#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时任务扫描器 v1.0.0 —— 只读

扫描 macOS 上「非系统自带」的 LaunchAgents / LaunchDaemons，输出看板所需数据。

⚠️ 安全边界（本脚本严格遵守，修改前请三思）：
  - 只读取 plist 文件，只调用 launchctl print 查询状态
  - 不加载(load)、不卸载(unload)、不启用、不禁用、不删除任何任务
  - 不写入系统目录，只在同目录 data/ 下产出数据文件

产出：
  data/scan-data.js   —— 供 dashboard.html 直接 <script> 引入（避开 file:// 的 CORS 限制）
  data/scan-data.json —— 标准 JSON，便于你拿去别处用
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import atomic

HOME = str(Path.home())
UID = os.getuid()
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ── 扫描范围：只扫非系统目录（/System/Library 下 846 个是 macOS 自带，不纳入）──
SCAN_TARGETS = [
    (f"{HOME}/Library/LaunchAgents", "用户级", "gui"),
    ("/Library/LaunchAgents",         "系统级-代理", "gui"),
    ("/Library/LaunchDaemons",        "系统级-守护", "system"),
]

# ── 厂商归属识别（按 label 关键字匹配，顺序敏感：先匹配先生效）──
VENDOR_MAP = [
    (("taskscheduler-workbench",), "自建·定时任务工作台"),
    (("sangfor", "atrust"),       "深信服 aTrust"),
    (("leagsoft", "lvmagent"),    "联软 UniAccess"),
    (("tencent", "lemon"),        "腾讯柠檬清理"),
    (("insolu", "cpms"),          "INSOLU 打印机驱动"),
    (("aldente", "apphousekitchen"), "AlDente Pro"),
    (("getlux", "xsus"),          "Lux"),
    (("microsoft", "edge"),       "Microsoft"),
    (("google",),                 "Google"),
]

# 公司管控软件：禁止任何操作（卸载/停用会影响办公网络准入）
MANAGED_VENDORS = {"深信服 aTrust", "联软 UniAccess"}

# 人工补充的已知备注。
#
# 这里是**示例**，照抄没有意义——它的作用是让同一个 label 在页面上多一行人话解释。
# 用法：跑一次 scan_tasks.py，看 data/scan-data.json 里哪些任务你认不出来，
#      把它的 label 加到这里，写一句「这是什么、动了会怎样」。
# 不填也完全不影响功能，只是页面上那一列会空着。
KNOWN_NOTES = {
    "com.taskworkbench.mac": (
        "工作台自己装的本地服务（serve.py），登录自启。"
        "作用：给看板的「刷新」按钮提供 /api/refresh 接口。"
        "停用或删除它，按钮会失效，但「刷新.command」仍可正常扫描。"
    ),
    # 下面三条是常见第三方任务的示例，仅供参考，可删
    "com.tencent.Lemon.trash": "腾讯柠檬清理的废纸篓自动清理，启动次数会持续增长，属正常。",
    "com.sangfor.limit.maxfiles": (
        "示例：某些企业管控软件会在开机时改写系统内核参数。"
        "这类任务通常是公司 IT 统一下发的，不建议改动。"
    ),
    "com.insolu.CpmsDeamon.plist": (
        "示例：打印机驱动的守护进程。停用会让对应打印机不可用。"
    ),
}

WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]

STATE_ZH = {
    "running": "运行中",
    "not running": "未运行",
    "waiting": "等待中",
    "initialized": "已初始化",
}


# ────────────────────────── 工具函数 ──────────────────────────

def classify_vendor(label: str, filename: str = "") -> str:
    """
    按 label + 文件名共同匹配。
    有些 plist 的 Label 与文件名不一致（如 com.leagsoft.uniaccess.plist 的 Label 就叫 "UniAccess"），
    只看 Label 会漏判，所以两个都参与匹配。
    """
    low = f"{label} {filename}".lower()
    for keys, name in VENDOR_MAP:
        for k in keys:
            if k in low:
                return name
    return "其他/待确认"


def domain_of(kind: str, label: str) -> str:
    """gui 域必须带 UID，写成 gui/<label> 会报 Bad request"""
    return f"gui/{UID}/{label}" if kind == "gui" else f"system/{label}"


def interval_to_text(sec: int) -> str:
    if sec >= 3600 and sec % 3600 == 0:
        return f"每 {sec // 3600} 小时"
    if sec >= 60 and sec % 60 == 0:
        return f"每 {sec // 60} 分钟"
    return f"每 {sec} 秒"


def cal_to_text(it: dict) -> str:
    h, m = it.get("Hour"), it.get("Minute")
    time_str = f"{int(h):02d}:{int(m):02d}" if h is not None and m is not None else "时刻未指定"
    wd, d, mo = it.get("Weekday"), it.get("Day"), it.get("Month")
    if wd is not None:
        base = f"每{WEEK[int(wd) % 7]}"
    elif d is not None:
        base = f"每月 {int(d)} 日"
    elif mo is not None:
        base = f"每年 {int(mo)} 月"
    else:
        base = "每天"
    return f"{base} {time_str}"


def describe_schedule(p: dict) -> str:
    """把 plist 里的定时字段翻译成人话"""
    parts = []

    sci = p.get("StartCalendarInterval")
    if sci is not None:
        items = sci if isinstance(sci, list) else [sci]
        for it in items:
            if isinstance(it, dict):
                parts.append(cal_to_text(it))

    si = p.get("StartInterval")
    if si is not None:
        try:
            parts.append(interval_to_text(int(si)))
        except (TypeError, ValueError):
            parts.append(f"每 {si} 秒")

    if p.get("RunAtLoad"):
        parts.append("登录/开机时触发一次")
    if p.get("KeepAlive"):
        parts.append("保持常驻（退出后自动重启）")
    if p.get("WatchPaths"):
        parts.append(f"监听 {len(p['WatchPaths'])} 个路径变化触发")

    if not parts:
        parts.append("无常规定时（按需触发 / 由主程序调用）")
    return "；".join(parts)


def read_plist(path: str):
    """用 plutil 读 plist（兼容 binary 格式）。失败返回 None。"""
    try:
        r = subprocess.run(
            ["plutil", "-convert", "json", "-o", "-", path],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def get_runtime(kind: str, label: str) -> dict:
    """
    只读查询运行状态。
    注意：本机的 `launchctl list` 恒返回空（受限），
    因此改用 `launchctl print <domain>/<label>` 逐条查询。
    """
    info = {"state": "未知", "raw_state": "", "runs": None, "last_exit": None, "pid": None}
    try:
        r = subprocess.run(
            ["launchctl", "print", domain_of(kind, label)],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        info["state"] = "查询超时"
        return info
    except Exception:
        info["state"] = "查询失败"
        return info

    out = (r.stdout or "") + (r.stderr or "")
    if "Could not find service" in out or "Bad request" in out or out.strip() == "":
        info["state"] = "未加载"
        return info

    for line in out.splitlines():
        s = line.strip()
        for key, field in (
            ("state =", "raw_state"), ("pid =", "pid"),
            ("runs =", "runs"), ("last exit code =", "last_exit"),
        ):
            if s.startswith(key):
                info[field] = s.split("=", 1)[1].strip()

    # launchctl 对常驻/从未启动的进程会输出 "(never exited)"，这不是失败，
    # 归零处理，否则会被误判成「有失败记录」。
    if info["last_exit"] and "never exited" in info["last_exit"]:
        info["raw_last_exit"] = info["last_exit"]
        info["last_exit"] = None

    info["state"] = STATE_ZH.get(info["raw_state"], info["raw_state"] or "未知")
    return info


def extract_program(p: dict) -> str:
    pa = p.get("ProgramArguments")
    if isinstance(pa, list) and pa:
        return str(pa[0])
    if p.get("Program"):
        return str(p["Program"])
    return "（未指定，可能通过 MachService 触发）"


def risk_of(vendor: str, state: str, last_exit) -> dict:
    """给出处置态度标签，而非任意评级"""
    if vendor in MANAGED_VENDORS:
        return {"level": "locked", "text": "禁操作", "hint": "公司管控软件，停用会影响办公网络准入"}
    if vendor == "待确认来源" or vendor == "其他/待确认":
        return {"level": "unknown", "text": "待确认", "hint": "来源不明，先查清是哪个软件装的再决定"}
    if last_exit not in (None, "", "0"):
        return {"level": "warn", "text": "有失败记录", "hint": f"上次退出码 {last_exit}，可留意是否反复失败"}
    return {"level": "normal", "text": "可评估", "hint": "第三方组件，停用前确认对应主程序是否依赖它"}


# ────────────────────────── 主流程 ──────────────────────────

def scan() -> dict:
    tasks, errors = [], []

    for directory, level, kind in SCAN_TARGETS:
        if not os.path.isdir(directory):
            errors.append({"dir": directory, "error": "目录不存在或无权限"})
            continue
        try:
            files = sorted(f for f in os.listdir(directory) if f.endswith(".plist"))
        except PermissionError:
            errors.append({"dir": directory, "error": "权限不足，无法列出"})
            continue

        for fn in files:
            path = os.path.join(directory, fn)
            raw = read_plist(path)
            if raw is None:
                errors.append({"dir": directory, "error": f"{fn} 解析失败"})
                continue

            label = raw.get("Label") or fn[:-6]
            vendor = classify_vendor(label, fn)
            runtime = get_runtime(kind, label)

            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
            except OSError:
                mtime = "未知"

            tasks.append({
                "label": label,
                "file": fn,
                "path": path,
                "level": level,
                "domain": domain_of(kind, label),
                "vendor": vendor,
                "program": extract_program(raw),
                "schedule": describe_schedule(raw),
                "raw_schedule": {
                    "StartInterval": raw.get("StartInterval"),
                    "StartCalendarInterval": raw.get("StartCalendarInterval"),
                    "RunAtLoad": bool(raw.get("RunAtLoad")),
                    "KeepAlive": bool(raw.get("KeepAlive")) if not isinstance(raw.get("KeepAlive"), dict) else True,
                },
                "state": runtime["state"],
                "pid": runtime["pid"],
                "runs": runtime["runs"],
                "last_exit": runtime["last_exit"],
                "disabled": bool(raw.get("Disabled")),
                "log_path": raw.get("StandardOutPath") or raw.get("StandardErrorPath") or "",
                "file_mtime": mtime,
                "note": KNOWN_NOTES.get(label, ""),
                "risk": risk_of(vendor, runtime["state"], runtime["last_exit"]),
            })

    tasks.sort(key=lambda t: (t["level"], t["vendor"], t["label"]))

    counts = {
        "total": len(tasks),
        "running": sum(1 for t in tasks if t["state"] == "运行中"),
        "not_running": sum(1 for t in tasks if t["state"] == "未运行"),
        "unknown_state": sum(1 for t in tasks if t["state"] in ("未知", "查询超时", "查询失败")),
        "locked": sum(1 for t in tasks if t["risk"]["level"] == "locked"),
        "unknown_src": sum(1 for t in tasks if t["risk"]["level"] == "unknown"),
        "failed": sum(1 for t in tasks if t["risk"]["level"] == "warn"),
    }

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": os.uname().nodename,
        "user": os.environ.get("USER", "unknown"),
        "macos": get_macos_version(),
        "counts": counts,
        "tasks": tasks,
        "errors": errors,
        "scan_scope": [d for d, _, _ in SCAN_TARGETS],
    }


def get_macos_version() -> str:
    try:
        r = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return "未知"


def write_outputs(data):
    """把扫描结果落盘（原子写，避免并发读到半截 JSON）

    单独抽成函数，是因为 collect.py 的常驻采集器也要复用：
    它自己调用 scan() 拿数据，再调这里写文件，避免重复实现写盘逻辑。
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    jpath = os.path.join(OUT_DIR, "scan-data.json")
    jspath = os.path.join(OUT_DIR, "scan-data.js")
    # 1) JSON（标准格式，便于二次使用）
    atomic.write_json(jpath, data)
    # 2) JS（供 file:// 直接打开看板，绕开浏览器对本地文件的 fetch 限制）
    atomic.write_js(jspath, "SCAN_DATA", data)
    return jpath, jspath


def main():
    data = scan()
    jpath, jspath = write_outputs(data)

    c = data["counts"]
    print("─" * 52)
    print("  定时任务扫描完成（只读，未做任何改动）")
    print("─" * 52)
    print(f"  扫描范围  ：{len(data['scan_scope'])} 个目录（已排除 /System 下系统自带任务）")
    print(f"  任务总数  ：{c['total']}")
    print(f"  运行中    ：{c['running']}")
    print(f"  未运行    ：{c['not_running']}")
    print(f"  状态未知  ：{c['unknown_state']}")
    print(f"  禁操作    ：{c['locked']} 条（公司管控软件）")
    print(f"  来源待确认：{c['unknown_src']} 条")
    print(f"  有失败记录：{c['failed']} 条")
    if data["errors"]:
        print(f"  ⚠️ 解析异常：{len(data['errors'])} 条")
        for e in data["errors"][:5]:
            print(f"     - {e['dir']}: {e['error']}")
    print("─" * 52)
    print(f"  数据已写入：{jspath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
