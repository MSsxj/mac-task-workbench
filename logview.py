#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务日志读取 logview.py  v1.0.0

安全前提（这一条是整个模块存在的理由）：
  网页只允许读「白名单里明确列出的文件」，绝不允许前端传路径。
  前端只能传 task_id + source_id，真实路径全部在服务端解析。
  这样即使有人手动拼 URL，也读不到 /etc/passwd、备份目录、项目源码。

白名单三层来源（任一命中才可读）：
  1. 本项目自己产生的 data/runs/*.jsonl（包装器结构化日志）
  2. 内置固定路径（本项目服务的 stdout 日志、采集器日志）
  3. 用户手工放进 logs_allow.json 的显式授权路径（默认不存在 = 不额外授权）

不做的事：
  * 不读 WorkBuddy 的 runs_json / thread_title（会话正文），那边根本没有
    可展示的明细日志，如实返回「平台只提供结果码」。
  * 不做关键词替换就宣称安全：除了打码，还限制来源、行数、单行长度、
    总字节数，并且只按结构化字段回显。

对外：
  summary_for(runs, task_id)            运行摘要 + 可用日志来源
  read_source(runs, task_id, source_id) 读某个来源的内容（已脱敏、已截断）
"""

import datetime
import json
import os
import hashlib
import re
import time

import state_history

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
RUNS_DIR = os.path.join(DATA, "runs")
SCAN_JSON = os.path.join(DATA, "scan-data.json")
ALLOW_JSON = os.path.join(BASE, "logs_allow.json")

MAX_LINES = 300          # 最多返回多少行
MAX_LINE_CHARS = 400     # 单行最长
MAX_BYTES = 96 * 1024    # 单文件最多读尾部这么多字节

# 内置固定白名单（本项目自己的日志）
BUILTIN_FILES = [
    ("svc-log", "本地服务日志", "/tmp/taskscheduler-workbench.log"),
    ("collector-log", "采集器日志", "/tmp/taskscheduler-collector.log"),
]

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_LONGNUM = re.compile(r"\b\d{8,}\b")
_SECRET = re.compile(
    r"((?:password|passwd|pwd|app_secret|secret|token|authorization|bearer|dsn)\s*[=:]\s*)(\S+)",
    re.I)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.I)
_LONGKEY = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_HOME_USER = os.path.basename(os.path.expanduser("~"))


def sanitize_line(s):
    """单行脱敏。原则：宁可多打码，也不要把内部信息带出去。

    除常见凭据外，还打掉 IPv4（本机回环除外）、邮箱、8 位以上纯数字
    （可能是手机号 / 工号 / 单号）和超长随机串（疑似密钥）。
    """
    if s is None:
        return ""
    out = str(s).replace("\r", " ").replace("\t", "    ")
    out = _BEARER.sub(r"\1***", out)
    out = _SECRET.sub(r"\1***", out)
    out = _LONGKEY.sub("***", out)
    out = _EMAIL.sub("***@***", out)

    def _ip(m):
        ip = m.group(0)
        return ip if ip.startswith("127.") else "<IP>"

    out = _IPV4.sub(_ip, out)
    out = _LONGNUM.sub("<NUM>", out)
    # 厂商日志正文常带本机登录名（如 strOsLogon = xxx），一并打码
    if _HOME_USER and len(_HOME_USER) >= 3:
        out = re.sub(re.escape(_HOME_USER), "<用户>", out, flags=re.I)
    if len(out) > MAX_LINE_CHARS:
        out = out[:MAX_LINE_CHARS] + "…(截断)"
    return out


# ────────────────────────── 白名单 ──────────────────────────

def _load_allow():
    """读用户显式授权清单；格式 {"files":[路径...],"dirs":[目录...]}"""
    if not os.path.exists(ALLOW_JSON):
        return {"files": [], "dirs": []}
    try:
        with open(ALLOW_JSON, encoding="utf-8") as f:
            d = json.load(f)
        return {"files": list(d.get("files") or []), "dirs": list(d.get("dirs") or [])}
    except (OSError, ValueError):
        return {"files": [], "dirs": []}


def _allowed_paths():
    """返回 {绝对真实路径: (source_id, 显示名)}"""
    allow = _load_allow()
    m = {}
    for sid, label, p in BUILTIN_FILES:
        rp = os.path.realpath(os.path.expanduser(p))
        m[rp] = (sid, label)
    for p in allow["files"]:
        try:
            rp = os.path.realpath(os.path.expanduser(str(p)))
        except (OSError, ValueError):
            continue
        m.setdefault(rp, ("allow-" + os.path.basename(rp), "授权日志：" + os.path.basename(rp)))
    return m, [os.path.realpath(os.path.expanduser(d)) for d in allow["dirs"]]


def is_allowed(path):
    """路径是否可读。用 realpath 比对，挡掉符号链接跳出去。"""
    try:
        rp = os.path.realpath(path)
    except (OSError, ValueError):
        return False, None
    # 1) 本项目 runs 目录（只允许 .jsonl）
    if rp.startswith(os.path.realpath(RUNS_DIR) + os.sep) and rp.endswith(".jsonl"):
        if os.path.isfile(rp):
            return True, "wrapper"
        return False, None
    # 2) 固定 + 用户授权文件
    files, dirs = _allowed_paths()
    if rp in files and os.path.isfile(rp):
        return True, files[rp][0]
    # 3) 用户授权目录（限 .log / .txt / .jsonl）
    for d in dirs:
        if rp.startswith(d + os.sep) and rp.endswith((".log", ".txt", ".jsonl")):
            if os.path.isfile(rp):
                return True, "allow-dir"
    return False, None


# ────────────────── 本机授权目录：按任务发现日志文件 ──────────────────
# 目的：让每个本机任务能看到「自己那摊程序写下的日志」，而不是只剩两项
# 「不可用」。匹配词全部由任务自身的 label / 可执行文件路径 / 归属推导，
# 不硬编码任何厂商名；换一台机器只要改 logs_allow.json 的 dirs 即可。

_EXT_TTL = 20                  # 目录扫描结果缓存秒数，避免每次点击都遍历
_EXT_LIMIT = 120               # 单个任务最多列出多少个日志文件
_EXT_CACHE = {}
_EXT_STOP = {
    "com", "plist", "app", "exe", "dvc", "agent", "service", "control",
    "daemon", "deamon", "library", "application", "support", "contents",
    "macos", "resources", "bin", "opt", "users", "log", "logs", "root",
    "instbase", "other", "system", "tmp", "launchagents", "launchdaemons",
    "待确认", "其他", "本机", "任务", "归属",
}


def _ext_dirs():
    """返回授权目录的真实路径列表，只取 logs_allow.json 的 dirs。"""
    out = []
    for raw in _load_allow()["dirs"]:
        try:
            rp = os.path.realpath(os.path.expanduser(str(raw)))
        except (OSError, ValueError):
            continue
        if os.path.isdir(rp):
            out.append(rp)
    return out


def _clean_bytes(data):
    """部分厂商用预分配环形文件，尾部是 NUL 填充，去掉后再切行。"""
    return data.decode("utf-8", errors="replace").replace("\x00", "")


# 可读性检查推迟到 read_source：扫描期逐个 open 厂商日志会被管控软件
# 实时拦截，实测 81 个文件耗时 38 秒，页面完全不能接受。


def _task_keys(ctx):
    """从任务自身字段推导匹配关键词，全部小写。"""
    loc = (ctx or {}).get("local") or {}
    raw = " ".join(str(loc.get(k) or "") for k in
                   ("label", "program", "vendor", "log_path", "file", "path"))
    parts = re.split(r"[^a-z0-9\u4e00-\u9fff]+", raw.lower())
    home_user = os.path.basename(os.path.expanduser("~")).lower()
    # 路径里的用户名、年份这类词会误命中同目录日志（例如把 gui-<用户>_0.log
    # 判给自建任务），必须排除；匹配只用能标识程序本身的词。
    return {p for p in parts if len(p) >= 3 and p not in _EXT_STOP
            and p != home_user and not p.isdigit()}


def _human_size(n):
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1024.0 / 1024.0)


_AUTH_TTL = 60                 # 授权目录全量扫描的共享缓存秒数
_AUTH_CACHE = None            # (ts, [文件信息...])，所有任务共用一份


def _authorized_files():
    """全局扫一次授权目录，缓存给全部任务复用（只 stat，不打开文件）。

    规则与 is_allowed 的「授权目录」分支一致：真实路径在目录内 + 后缀
    为 .log/.txt/.jsonl。扫描期不读文件内容，避免管控软件 hook 拖慢首屏。
    """
    global _AUTH_CACHE
    now = time.time()
    if _AUTH_CACHE and now - _AUTH_CACHE[0] < _AUTH_TTL:
        return _AUTH_CACHE[1]
    home_user = os.path.basename(os.path.expanduser("~"))
    dirs = _ext_dirs()
    out = []
    for root in dirs:
        root_name = os.path.basename(root)
        for dp, dns, fns in os.walk(root):
            if dp[len(root):].count(os.sep) > 2:
                dns[:] = []
                continue
            dns[:] = [x for x in dns if not x.startswith(".")]
            for fn in fns:
                if not fn.endswith((".log", ".txt", ".jsonl")):
                    continue
                p = os.path.join(dp, fn)
                try:
                    rp = os.path.realpath(p)
                    st = os.stat(rp)
                except OSError:
                    continue
                if st.st_size < 512:
                    continue
                if not any(rp.startswith(d + os.sep) for d in dirs):
                    continue
                rel = os.path.relpath(rp, root)
                out.append({
                    "sid": "ext-" + hashlib.md5(
                        (root_name + "/" + rel).encode("utf-8")).hexdigest()[:12],
                    "path": rp, "rel": root_name + "/" + rel,
                    "name": fn.replace(home_user, "<用户>") if home_user else fn,
                    "size": st.st_size, "mtime": st.st_mtime,
                    "size_txt": _human_size(st.st_size),
                    "mtime_txt": time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime)),
                })
    _AUTH_CACHE = (time.time(), out)   # 用结束时间，别把扫描耗时算进 TTL
    return out


def discover_logs_for(task_id):
    """按任务关键词挑出相关日志，并附同目录其他日志（折叠展示）。

    一个相关文件都没有时返回空表，如实显示「未找到」，不张冠李戴。
    返回稳定 source_id，前端始终不接触文件路径。
    """
    now = time.time()
    hit = _EXT_CACHE.get(task_id)
    if hit and now - hit[0] < _EXT_TTL:
        return hit[1]

    keys = _task_keys(_task_ctx(task_id))
    related, others = [], []
    for f in _authorized_files():
        matched = [k for k in keys if k in f["rel"].lower()]
        item = dict(f)
        item["related"] = bool(matched)
        item["score"] = sum(10 for k in matched if k in f["name"].lower()) + len(matched)
        (related if matched else others).append(item)

    if not related:                       # 无相关文件时不展示同目录杂项
        _EXT_CACHE[task_id] = (now, [])
        return []
    related.sort(key=lambda x: (-x["score"], -x["mtime"], x["name"]))
    others.sort(key=lambda x: (-x["mtime"], x["name"]))
    out = (related + others)[:_EXT_LIMIT]
    _EXT_CACHE[task_id] = (now, out)
    return out


def _ext_lookup(task_id, sid):
    """read_source 用：确认 sid 确实属于这个任务可见集合。"""
    for e in discover_logs_for(task_id):
        if e["sid"] == sid:
            return e
    return None


# ────────────────────────── 数据源读取 ──────────────────────────

def _load_runs_file():
    try:
        with open(os.path.join(DATA, "runs.js"), encoding="utf-8") as f:
            txt = f.read()
        i = txt.index("=")
        obj, _ = json.JSONDecoder().raw_decode(txt[i + 1:].strip())
        return obj
    except Exception:
        return {}


def _load_scan():
    try:
        with open(SCAN_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _load_jsonl_events():
    """读本项目包装器日志（结构化，不含业务正文）

    包含 data/runs/_verification/ 下归档的早期人工验证记录——
    它们确实发生过，展示时标「归档」，但不参与生产判定。
    """
    out = []

    def read_dir(d, archived=False):
        if not os.path.isdir(d):
            return
        for name in sorted(f for f in os.listdir(d) if f.endswith(".jsonl")):
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if archived:
                            rec["archived"] = True
                        out.append(rec)
            except OSError:
                continue

    read_dir(RUNS_DIR)
    read_dir(os.path.join(RUNS_DIR, "_verification"), archived=True)
    return out


def _task_ctx(task_id):
    """判断这个 id 是 WorkBuddy 任务还是本机 plist 任务"""
    runs = _load_runs_file()
    t = (runs.get("tasks") or {}).get(task_id)
    if t:
        return {"kind": "wb", "id": task_id, "name": t.get("name"), "run": t}
    for it in (_load_scan().get("tasks") or []):
        if it.get("label") == task_id:
            return {"kind": "local", "id": task_id, "name": it.get("label"),
                    "local": it}
    return None


def _merge_events(task_id):
    """把这个任务的本项目日志按 run_id 合并成一条条运行

    兜底：早期 finish() 没写 task_id，终态行只有 run_id；
    用 run_id 前缀（task_id 前 8 位）反查归属，否则这些终态会被丢掉、
    只剩一条永远 RUNNING 的 start 行。
    """
    prefix = str(task_id or "")[:8]
    evs = []
    for e in _load_jsonl_events():
        tid = e.get("task_id")
        if tid == task_id:
            evs.append(e)
        elif not tid and str(e.get("run_id") or "").split("_")[0] == prefix:
            evs.append(e)
    runs = {}
    order = []
    for e in evs:
        rid = e.get("run_id")
        if not rid:
            continue
        if rid not in runs:
            runs[rid] = dict(e)
            order.append(rid)
        else:
            runs[rid].update({k: v for k, v in e.items() if v is not None})
    return [runs[r] for r in order]


def summary_for(task_id):
    """第一级：运行摘要 + 有哪些日志可看"""
    ctx = _task_ctx(task_id)
    if not ctx:
        return {"ok": False, "error": "没有这个任务"}

    if ctx["kind"] == "wb":
        t = ctx["run"]
        last = t.get("last") or {}
        hist = t.get("history") or []
        summary = {
            "name": t.get("name"),
            "last_at": last.get("started_at"),
            "ended_at": last.get("ended_at"),
            "status": last.get("status"),
            "duration_ms": last.get("duration_ms"),
            "error": last.get("error"),
            "source": last.get("source"),
            "trigger": last.get("trigger"),
            "is_production": last.get("is_production", True),
            "step": last.get("step"),
            "rows": last.get("rows"),
            "expected_at": t.get("expected_at"),
            "history_count": len(hist),
        }
    else:
        evs = _merge_events(task_id)
        evs.sort(key=lambda x: x.get("started_at") or "")
        last = evs[-1] if evs else {}
        summary = {
            "name": ctx["local"].get("label"),
            "last_at": last.get("started_at"),
            "ended_at": last.get("ended_at"),
            "status": last.get("status"),
            "duration_ms": last.get("duration_ms"),
            "error": last.get("error"),
            "source": "wrapper" if last else None,
            "trigger": last.get("trigger"),
            "is_production": (str(last.get("trigger") or "").lower().find("dryrun") < 0) if last else True,
            "step": last.get("step"),
            "rows": last.get("rows"),
            "expected_at": None,
            "history_count": len(evs),
        }
        # launchd 自己的记录：累计启动次数 + 上次退出码。
        # 这不是「逐次运行历史」，但是真实可得的执行信号，如实附上。
        summary["launchd"] = {
            "state": ctx["local"].get("state"),
            "runs": ctx["local"].get("runs"),
            "last_exit": ctx["local"].get("last_exit"),
            "schedule": ctx["local"].get("schedule"),
            "vendor": ctx["local"].get("vendor"),
        }

    sources = []
    # 1) 本项目结构化日志
    if ctx["kind"] == "local":
        has_ev = bool(_merge_events(task_id))
    else:
        has_ev = bool(_merge_events(task_id))
    if has_ev:
        sources.append({"id": "wrapper", "label": "本项目运行日志（包装器）",
                        "available": True, "reason": None})
    else:
        sources.append({"id": "wrapper", "label": "本项目运行日志（包装器）",
                        "available": False,
                        "reason": "该任务未接入包装器，没有逐步骤日志"})

    # 2) plist 指定的日志文件（必须命中白名单）
    if ctx["kind"] == "local":
        lp = ctx["local"].get("log_path") or ""
        if lp:
            ok, _sid = is_allowed(lp)
            sources.append({
                "id": "plist-log",
                "label": "任务自身输出日志（plist 指定）",
                "available": bool(ok),
                "reason": None if ok else "该路径不在日志白名单内，未授权读取",
            })
        else:
            sources.append({"id": "plist-log", "label": "任务自身输出日志（plist 指定）",
                            "available": False, "reason": "plist 未配置输出日志路径"})

    # 3) 服务/采集器日志（只对本工作台自己的任务开放）
    if ctx["kind"] == "local" and "taskscheduler-workbench" in (task_id or ""):
        files, _ = _allowed_paths()
        for rp, (sid, label) in files.items():
            sources.append({"id": sid, "label": label,
                            "available": os.path.isfile(rp),
                            "reason": None if os.path.isfile(rp) else "文件不存在"})

    # 4) WorkBuddy 平台
    if ctx["kind"] == "wb":
        sources.append({"id": "wb-platform", "label": "WorkBuddy 平台运行记录",
                        "available": True,
                        "reason": "平台只提供结果码与起止时间，无运行正文"})

    # 5) 本机授权目录里与该任务相关的日志文件（v2.3.0）
    #    命中关键词的排在前面；同目录其余日志折叠展示，仍受白名单与脱敏约束。
    if ctx["kind"] == "local":
        try:
            ext = discover_logs_for(task_id)
        except Exception:
            ext = []
        for e in ext:
            sources.append({
                "id": e["sid"], "label": "本机日志 · " + e["name"],
                "group": "ext", "related": e["related"],
                "meta": e["size_txt"] + " ｜ 最后写入 " + e["mtime_txt"],
                "available": True, "reason": None,
            })
        if not ext:
            sources.append({
                "id": "ext-none", "label": "本机授权目录中的相关日志",
                "group": "ext", "related": False, "available": False,
                "reason": "未找到与该任务相关的日志文件；该程序可能根本没写文件日志",
            })

    # 常驻守护进程的存活变动时间线（由 collect.py 每轮对比 pid 生成）
    survival = {"resident": False, "since": None, "events": [],
                "last_event": None, "uptime_seconds": None, "changes": 0}
    if ctx["kind"] == "local":
        try:
            survival = state_history.survival_for(task_id)
        except Exception:
            pass

    return {"ok": True, "kind": ctx["kind"], "task_id": task_id,
            "summary": summary, "sources": sources, "survival": survival}


# ────────────────────────── 第二级：详细日志 ──────────────────────────

def _fmt_event(e):
    ts = (e.get("started_at") or e.get("ended_at") or "").replace("T", " ")
    parts = ["%s" % ts, "%-7s" % (e.get("status") or "-")]
    if e.get("archived"):
        parts.append("[归档验证]")
    if e.get("step"):
        parts.append("步骤=%s" % e["step"])
    if e.get("rows") is not None:
        parts.append("行数=%s" % e["rows"])
    if e.get("duration_ms") is not None:
        parts.append("耗时=%.1fs" % (e["duration_ms"] / 1000.0))
    if e.get("trigger"):
        parts.append("触发=%s" % e["trigger"])
    if e.get("error"):
        parts.append("错误=%s" % e["error"])
    if e.get("external_written") is not None:
        parts.append("外部写入=%s" % e["external_written"])
    return "  ".join(parts)


def _tail_file(path, max_lines=MAX_LINES, max_bytes=MAX_BYTES):
    """读文件尾部，避免把大文件整个读进内存"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                data = f.read()
                cut = True
            else:
                data = f.read()
                cut = False
    except OSError as e:
        return None, "读取失败：%s" % e, False
    # 部分厂商用预分配环形文件，尾部是 NUL；不清掉会读出满屏空白
    txt = _clean_bytes(data)
    lines = [x for x in txt.splitlines() if x.strip()]
    truncated = cut
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return lines, None, truncated


def read_source(task_id, source_id):
    """第二级：按来源读内容。只接受服务端定义的 source_id。"""
    ctx = _task_ctx(task_id)
    if not ctx:
        return {"ok": False, "error": "没有这个任务"}

    if source_id == "wrapper":
        evs = _merge_events(task_id)
        if not evs:
            return {"ok": True, "lines": [], "truncated": False,
                    "reason": "该任务没有本项目运行日志（未接入包装器）"}
        evs.sort(key=lambda x: x.get("started_at") or "")
        lines = [_fmt_event(e) for e in evs[-MAX_LINES:]]
        head = [
            "来源：本项目 data/runs/*.jsonl（run_task.py 包装器写入）",
            "说明：只记录状态、步骤、行数、耗时与错误摘要，不含业务明细",
            "",
        ]
        return {"ok": True, "lines": head + [sanitize_line(x) for x in lines],
                "truncated": len(evs) > MAX_LINES}

    if source_id == "plist-log":
        lp = (ctx.get("local") or {}).get("log_path") or ""
        if not lp:
            return {"ok": False, "error": "plist 未配置输出日志路径"}
        ok, _ = is_allowed(lp)
        if not ok:
            return {"ok": False, "error": "该路径不在日志白名单内，拒绝读取"}
        lines, err, trunc = _tail_file(lp)
        if err:
            return {"ok": False, "error": err}
        head = ["来源：plist 指定的 StandardOutPath / StandardErrorPath",
                "已脱敏：凭据、IP、邮箱、长数字串", ""]
        return {"ok": True, "lines": head + [sanitize_line(x) for x in lines],
                "truncated": trunc}

    if source_id == "wb-platform":
        t = ctx.get("run") or {}
        hist = t.get("history") or []
        lines = ["来源：WorkBuddy 本地库 automation_runs（只读结果码）",
                 "说明：平台不提供运行正文/会话内容，本项目也不读取。", ""]
        for h in reversed(hist):
            lines.append(_fmt_event({
                "started_at": h.get("started_at"),
                "ended_at": h.get("ended_at"),
                "status": h.get("status"),
                "duration_ms": h.get("duration_ms"),
                "error": h.get("error"),
                "trigger": h.get("trigger"),
            }))
        if not hist:
            lines.append("（该任务暂无平台运行记录）")
        return {"ok": True, "lines": [sanitize_line(x) for x in lines],
                "truncated": False}

    # 本机授权目录日志（v2.3.0）：sid 必须属于该任务可见集合
    if source_id.startswith("ext-"):
        e = _ext_lookup(task_id, source_id)
        if not e:
            return {"ok": False, "error": "未授权或已失效的日志来源"}
        ok, _ = is_allowed(e["path"])
        if not ok:
            return {"ok": False, "error": "不在白名单内"}
        lines, err, trunc = _tail_file(e["path"])
        if err:
            return {"ok": False, "error": err}
        if not lines:
            lines = ["（该文件尾部没有可读正文，可能是预分配的环形日志；"
                     "程序写入后即可查看）"]
        head = ["来源：本机授权日志 · " + e["name"],
                "最后写入：" + e["mtime_txt"] + " ｜ 文件 " + e["size_txt"],
                "已脱敏：凭据、IP、邮箱、长数字串、本机用户名；仅显示文件尾部", ""]
        return {"ok": True, "lines": head + [sanitize_line(x) for x in lines],
                "truncated": trunc}

    # 固定/授权文件来源
    files, _ = _allowed_paths()
    for rp, (sid, label) in files.items():
        if sid == source_id:
            ok, _ = is_allowed(rp)
            if not ok:
                return {"ok": False, "error": "不在白名单内"}
            lines, err, trunc = _tail_file(rp)
            if err:
                return {"ok": False, "error": err}
            return {"ok": True, "lines": [sanitize_line(x) for x in lines],
                    "truncated": trunc}

    return {"ok": False, "error": "未知的日志来源"}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法：python3 logview.py <task_id> [source_id]")
        sys.exit(2)
    tid = sys.argv[1]
    if len(sys.argv) > 2:
        print(json.dumps(read_source(tid, sys.argv[2]), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summary_for(tid), ensure_ascii=False, indent=2))
