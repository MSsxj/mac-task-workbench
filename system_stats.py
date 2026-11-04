#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mac 系统状态采集 system_stats.py  v1.1.0

设计原则：
  1. 只用 Python 标准库 + macOS 自带能力，不引入第三方依赖。
  2. 单次采集目标 < 300ms，不做全盘扫描、不持续抓包。
     这台是 8GB 内存的 Intel Mac，采集本身不能变成负担。
  3. 任何一项失败就返回 None，由页面显示「暂不可用」。
     **绝不兜底成 0% 或「正常」** —— 假数据比没数据更危险。

各项口径（写清楚，避免被误读）：

  CPU    ：读 mach 内核的 CPU 负载计数器（host_statistics /
           HOST_CPU_LOAD_INFO，user+system+nice+idle 的 tick 增量），
           算「非 idle 占比」。这是内核自己的口径，不依赖 ps/top。
           首次调用需要两次采样（间隔 0.25 秒）才有意义，之后每次
           都用「距上次调用的增量」计算，开销几乎为零。
           失败时退回 ps（部分受限环境禁 ps），再失败退回 top。
           与活动监视器允许百分之几的采样误差，不是瞬时精确值。

 内存   ：vm_stat 的 active + wired + compressor 页数 × 页大小，
           对应活动监视器「已使用内存」的大致口径，不含可清除缓存。

 磁盘   ：shutil.disk_usage("/")，即 statvfs 口径 = Finder / 活动监视器
           显示的「数据卷」容量。注意：`df /` 显示的是只读系统卷，
           两者数字不同是 APFS 的正常现象，不是采集错误。

 网络   ：取默认网关 → 对网关做 TCP 连接探测（443 / 80 / 53 依次尝试）。
           不用 ICMP：公司网络常把网关 ping 拦掉，会误判成断网。
           只连网关，不出公网，不抓包。

对外函数：
  get_all(use_cache=True) -> dict   一次拿全部；带 5 秒缓存，多标签页不会重复采集
  get_quick() -> dict               只拿 CPU/内存（更轻）

自测：
  python3 system_stats.py            打印一次采集结果与耗时
  python3 system_stats.py --compare  与 vm_stat / df / Finder 口径对一次数
"""

import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

CACHE_TTL = 5.0          # 秒；页面 15–30 秒刷新一次，5 秒缓存足够挡掉多标签重复采集
NET_TTL = 20.0           # 网络探测单独放宽：探不通时要等超时，别每次请求都付这个成本
_last = {"ts": 0.0, "data": None}
_net_cache = {"ts": 0.0, "data": None}

# CPU 增量采样状态
_cpu_lock = threading.Lock()
_cpu_prev = {"ticks": None, "ts": 0.0}

# 后台采样线程写入的「最新 CPU 值」，接口读它 → 请求几乎零等待
_shared = {"cpu": None, "ts": 0.0}
_sampler = {"thread": None, "stop": False}


def _run(cmd, timeout=6):
    """执行命令，返回 (stdout, ok)。失败不抛异常，返回 ("", False)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return (p.stdout or ""), False
        return (p.stdout or ""), True
    except Exception:
        return "", False


# ────────────────────────── CPU ──────────────────────────

def _mach_cpu_ticks():
    """读 mach CPU tick 计数器 [user, system, idle, nice]；失败返回 None

    走 libSystem 的 host_statistics，不 fork 任何进程，
    因此在限制 ps/top 的环境里仍然可用（沙箱、隐私限制都不影响）。
    """
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        libc.mach_host_self.restype = ctypes.c_uint32
        libc.host_statistics.restype = ctypes.c_int
        libc.host_statistics.argtypes = [
            ctypes.c_uint32, ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
        ]
        HOST_CPU_LOAD_INFO = 3
        count = ctypes.c_uint32(4)
        ticks = (ctypes.c_uint32 * 4)()
        kr = libc.host_statistics(libc.mach_host_self(), HOST_CPU_LOAD_INFO,
                                  ticks, ctypes.byref(count))
        if kr != 0:
            return None
        return [int(t) for t in ticks]
    except Exception:
        return None


def _cpu_from_ticks():
    """用 mach tick 增量算 CPU 占用。

    坑：这台机器在接近空闲时，0.25 秒内 4 个计数器可能一个都不动
    （实测约四成概率拿到全 0），直接算会得出「总量 0」而误判为失败。
    所以这里带重试：拿到全 0 就延长间隔再采一次，最多试 3 轮。
    """
    with _cpu_lock:
        base = _cpu_prev["ticks"]
        if base is None:
            base = _mach_cpu_ticks()
            if base is None:
                return None
            time.sleep(0.30)

        for _ in range(3):
            cur = _mach_cpu_ticks()
            if cur is None:
                return None
            d = [cur[i] - base[i] for i in range(4)]
            if any(x < 0 for x in d):
                # 计数器回绕 / 系统重启后变小：把当前值当新基线，重来
                base = cur
                time.sleep(0.30)
                continue
            total = sum(d)
            if total <= 0:
                base = cur
                time.sleep(0.30)
                continue
            _cpu_prev["ticks"] = cur
            _cpu_prev["ts"] = time.time()
            pct = (total - d[2]) * 100.0 / total
            return {
                "percent": round(max(0.0, min(pct, 100.0)), 1),
                "cores": os.cpu_count() or 1,
                "method": "mach 内核 CPU 计数器增量",
            }
        return None


def _cpu_from_ps():
    out, ok = _run(["ps", "-A", "-o", "%cpu="], timeout=5)
    if not ok or not out.strip():
        return None
    total = 0.0
    for tok in out.split():
        try:
            total += float(tok)
        except ValueError:
            continue
    ncpu = os.cpu_count() or 1
    return {
        "percent": round(min(max(total / ncpu, 0.0), 100.0), 1),
        "cores": ncpu,
        "method": "ps %CPU 之和 ÷ 逻辑核数（降级）",
    }


def _cpu_from_top():
    out, ok = _run(["top", "-l", "1", "-n", "0", "-s", "0"], timeout=12)
    if not ok or "CPU usage" not in out:
        return None
    m = re.search(r"CPU usage:\s*([\d.]+)%\s*user,\s*([\d.]+)%\s*sys,\s*([\d.]+)%\s*idle", out)
    if not m:
        return None
    busy = float(m.group(1)) + float(m.group(2))
    return {
        "percent": round(min(busy, 100.0), 1),
        "cores": os.cpu_count() or 1,
        "method": "top CPU usage 行（降级）",
    }


def cpu():
    """整机 CPU 占用百分比（0–100），失败返回 None

    优先用后台采样线程维护的最近值（十几秒内有效），这样接口调用不用等采样。
    后台线程没起（命令行直接跑）或值太旧，才现采。
    """
    if _shared["cpu"] and (time.time() - _shared["ts"]) < 15:
        d = dict(_shared["cpu"])
        d["cached"] = True
        return d
    for fn in (_cpu_from_ticks, _cpu_from_ps, _cpu_from_top):
        r = fn()
        if r:
            return r
    return None


def start_sampler(interval=2.0):
    """启动后台采样线程：每 2 秒读一次 mach 计数器，结果放 _shared。

    为什么要有它：算 CPU 占用必须比较两次采样，若在 HTTP 请求里现算，
    每次请求都要等 0.3–1 秒。放到常驻线程里，接口就能立刻返回。
    代价：每 2 秒一次 ctypes 调用，几乎不占 CPU。
    """
    if _sampler["thread"] is not None:
        return
    _sampler["stop"] = False

    def loop():
        while not _sampler["stop"]:
            try:
                r = _cpu_from_ticks()
                if r:
                    _shared["cpu"] = r
                    _shared["ts"] = time.time()
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=loop, name="cpu-sampler", daemon=True)
    _sampler["thread"] = t
    t.start()


def stop_sampler():
    _sampler["stop"] = True


# ────────────────────────── 内存 ──────────────────────────

def memory():
    """内存使用情况；失败返回 None"""
    out, ok = _run(["sysctl", "-n", "hw.memsize"], timeout=4)
    if not ok or not out.strip().isdigit():
        return None
    total = int(out.strip())

    vs, ok2 = _run(["vm_stat"], timeout=5)
    if not ok2 or not vs:
        return None

    page_size = 4096
    m = re.search(r"page size of (\d+) bytes", vs)
    if m:
        page_size = int(m.group(1))

    def pages(name):
        mm = re.search(r"^%s:\s+(\d+)\." % re.escape(name), vs, re.M)
        return int(mm.group(1)) if mm else 0

    active = pages("Pages active")
    wired = pages("Pages wired down")
    compressed = pages("Pages occupied by compressor")
    if active == 0 and wired == 0:
        return None

    used = min((active + wired + compressed) * page_size, total)
    return {
        "total": total,
        "used": used,
        "free": total - used,
        "percent": round(used * 100.0 / total, 1),
        "method": "vm_stat：active + wired + compressor",
    }


# ────────────────────────── 磁盘 ──────────────────────────

def disk():
    """数据卷容量（statvfs 口径，与 Finder 一致）；失败返回 None"""
    try:
        u = shutil.disk_usage("/")
    except OSError:
        return None
    if not u.total:
        return None
    return {
        "total": u.total,
        "used": u.used,
        "free": u.free,
        "percent": round(u.used * 100.0 / u.total, 1),
        "method": "根卷容量（= Finder 口径）",
    }


# ────────────────────────── 网络 ──────────────────────────

def _tcp_probe(host, ports=(443, 80, 53), timeout=0.35):
    """对网关做 TCP 连接探测，任一端口通则视为连通。不出公网。"""
    for p in ports:
        try:
            s = socket.create_connection((host, p), timeout=timeout)
            s.close()
            return True, p
        except OSError:
            continue
    return False, None


def _nc_probe(host, ports=(443,)):
    """nc 兜底：某些受限环境会禁掉 Python 自身的 socket 出站，
    但允许 nc 这类系统命令。只在 socket 全失败时用。"""
    for p in ports:
        try:
            r = subprocess.run(["nc", "-z", "-w", "1", host, str(p)],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                return True, p
        except Exception:
            continue
    return False, None


def _net_cached():
    """网络探测带 20 秒缓存：探不通要等超时，不能让每次请求都付这个代价"""
    if _net_cache["data"] and (time.time() - _net_cache["ts"]) < NET_TTL:
        d = dict(_net_cache["data"])
        d["cached"] = True
        return d
    d = network()
    if d:
        _net_cache["data"] = d
        _net_cache["ts"] = time.time()
    return d


def network():
    """默认网络的连通性。只探测默认网关，不访问互联网。"""
    out, ok = _run(["route", "-n", "get", "default"], timeout=4)
    if not ok or not out.strip():
        return {"state": "unknown", "text": "无默认路由（可能未连网）",
                "interface": None, "link_up": None, "port": None,
                "method": "route get default"}

    iface = gateway = None
    m = re.search(r"interface:\s*(\S+)", out)
    if m:
        iface = m.group(1)
    m = re.search(r"gateway:\s*(\S+)", out)
    if m:
        gateway = m.group(1)

    link_up = None
    if iface:
        io, iok = _run(["ifconfig", iface], timeout=4)
        if iok:
            link_up = "status: active" in io

    reachable, port, how = (None, None, "默认网关 TCP 探测")
    if gateway:
        reachable, port = _tcp_probe(gateway)
        if not reachable:
            ok_nc, p = _nc_probe(gateway)
            if ok_nc:
                reachable, port, how = True, p, "默认网关 TCP 探测（nc 兜底）"

    if reachable:
        state, text = "online", "网关可达"
    elif link_up:
        # 网卡连着但探测不通：公司网络常拦这类探测，不能直接判「断网」，
        # 但也不能说正常 —— 如实标为「受限」，让人自己判断。
        state, text = "limited", "网卡已连接，网关探测不通"
    elif reachable is False:
        state, text = "offline", "网卡未连接"
    else:
        state, text = "unknown", "无法判断"

    return {
        "state": state,
        "text": text,
        "interface": iface,
        "link_up": link_up,
        "port": port,
        "method": how,
        "scope": "仅局域网，不出公网",
    }


# ────────────────────────── 汇总 ──────────────────────────

def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def get_all(use_cache=True):
    """一次采集全部指标。单项失败为 None，整体不抛异常。"""
    now = time.time()
    if use_cache and _last["data"] and (now - _last["ts"]) < CACHE_TTL:
        d = dict(_last["data"])
        d["cached"] = True
        return d

    t0 = time.time()
    data = {
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cpu": _safe(cpu),
        "memory": _safe(memory),
        "disk": _safe(disk),
        "network": _safe(_net_cached),
        "host": os.uname().nodename,
        "elapsed_ms": None,
        "cached": False,
    }
    data["elapsed_ms"] = int((time.time() - t0) * 1000)
    _last["ts"] = time.time()
    _last["data"] = data
    return data


def get_quick():
    """只取 CPU 与内存（更轻）"""
    return {"collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cpu": _safe(cpu), "memory": _safe(memory)}


def main():
    compare = "--compare" in sys.argv
    d = get_all(use_cache=False)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    print("\n耗时：%d ms" % (d.get("elapsed_ms") or 0))

    if compare:
        print("\n── 与系统工具对照 ──")
        vs, _ = _run(["vm_stat"], timeout=6)
        for line in vs.splitlines():
            if re.match(r"^(Mach Virtual|Pages (active|wired|occupied))", line):
                print("  vm_stat:", line.strip())
        for path in ("/", "/System/Volumes/Data"):
            df, _ = _run(["df", "-k", path], timeout=6)
            ls = [l for l in df.splitlines() if l.strip()]
            if len(ls) >= 2:
                print("  df %-22s %s" % (path, ls[-1].strip()))
        sysctl, _ = _run(["sysctl", "-n", "hw.memsize"], timeout=4)
        print("  hw.memsize:", sysctl.strip(), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
