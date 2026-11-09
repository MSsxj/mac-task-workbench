#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
常驻守护进程的存活变动记录 v1.0.0

为什么需要它：
  launchd 只给两个数字——累计启动次数（runs）和上次退出码（last exit code）。
  对 KeepAlive 的常驻守护进程，进程从来不「退出」，这两个数字永远是
  runs=1 / never exited，等于没有信息。这类任务真正值得记的是「中间有没有掉过」，
  而不是「最后一次跑成功了没」。

做法：
  每轮采集把常驻任务的 (pid, state) 与上一次快照比，只在**变化时**追加一条事件。
  不记每次快照，所以数据量随时间线性增长，不会按采集轮次（默认每 5 分钟）爆炸。

事件类型：
  baseline  首次观测，建立基线（不算变动，只为了下次能对比）
  start     从无进程变为有进程
  stop      从有进程变为无进程
  restart   pid 变了（进程重启过，比如崩溃后被 KeepAlive 拉起、或软件升级）

不做的事：
  * 不判断「这算不算故障」。掉线可能是正常的——用户手动停用、软件升级、
    系统休眠恢复。只客观记录发生了什么，由人判断。
  * 不记录非常驻任务。一次性任务看 runs / last_exit / 包装器日志就够了。

对外：
  observe(tasks)               对比本轮扫描结果，追加变动事件，返回本次新增
  survival_for(task_id)        给页面用：基线时间、事件列表、当前持续时间
  prune()                      按天数和条数裁剪历史
"""

import datetime
import json
import os

import atomic

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SNAP_JSON = os.path.join(DATA, "state-snapshot.json")
HIST_JSONL = os.path.join(DATA, "state-history.jsonl")

KEEP_DAYS = 30          # 事件保留天数
MAX_EVENTS = 2000       # 最多保留多少条，超出按时间裁掉最老的

EVENT_LABEL = {
    "baseline": "开始观测",
    "start": "启动",
    "stop": "停止",
    "restart": "重启",
}


def _now():
    return datetime.datetime.now()


def _now_str():
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(str(s)[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


def _is_resident(task):
    """是否常驻：plist 带 KeepAlive，或当前有 pid（有 pid 说明是长跑进程）"""
    raw = task.get("raw_schedule") or {}
    if raw.get("KeepAlive"):
        return True
    return bool(task.get("pid"))


def _pid(task):
    p = task.get("pid")
    if p in (None, "", "—", "-"):
        return None
    return str(p)


def _load_snapshot():
    d = atomic.read_json(SNAP_JSON, None)
    return d if isinstance(d, dict) else {}


def _append_events(events):
    """追加写 jsonl。一条事件一行，避免整表重写。"""
    if not events:
        return
    os.makedirs(DATA, exist_ok=True)
    with open(HIST_JSONL, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _read_all():
    out = []
    try:
        with open(HIST_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def observe(tasks):
    """对比本轮扫描结果与上次快照，追加变动事件。返回本次新增的事件列表。

    tasks 是 scan_tasks.scan() 里的 tasks 数组（只处理本机任务）。
    第一轮跑时对每个常驻任务只写一条 baseline，不制造假的「启动」记录。
    """
    prev = _load_snapshot()
    now_str = _now_str()
    fresh = []
    snap = {}

    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        tid = t.get("label") or t.get("file") or ""
        if not tid or not _is_resident(t):
            continue

        pid = _pid(t)
        state = t.get("state") or ""
        snap[tid] = {"pid": pid, "state": state}

        old = prev.get(tid)
        if old is None:
            # 首次见到这个任务：只建基线
            fresh.append({"ts": now_str, "task_id": tid, "event": "baseline",
                          "pid": pid, "prev_pid": None, "state": state,
                          "vendor": t.get("vendor") or ""})
            continue

        op, ost = old.get("pid"), old.get("state") or ""
        if op == pid:
            continue                      # 没变化，不记

        if op and not pid:
            ev = "stop"
        elif not op and pid:
            ev = "start"
        elif op and pid:
            ev = "restart"
        else:
            continue                      # 两边都没有 pid，只是 state 文案变了，不记

        fresh.append({"ts": now_str, "task_id": tid, "event": ev,
                      "pid": pid, "prev_pid": op, "state": state,
                      "vendor": t.get("vendor") or ""})

    try:
        atomic.write_json(SNAP_JSON, snap)
    except OSError:
        pass
    try:
        _append_events(fresh)
        prune()
    except OSError:
        pass
    return [e for e in fresh if e["event"] != "baseline"]


def survival_for(task_id, limit=12):
    """给页面用的存活概要。

    返回：
      {"resident": bool, "since": 基线时间, "events": [...最近 limit 条, 新的在前],
       "last_event": 最近一条, "uptime_seconds": 当前状态持续了多少秒,
       "changes": 除 baseline 外的变动次数}
    """
    tid = str(task_id or "")
    evs = [e for e in _read_all() if e.get("task_id") == tid]
    if not evs:
        return {"resident": False, "since": None, "events": [],
                "last_event": None, "uptime_seconds": None, "changes": 0}

    evs.sort(key=lambda e: e.get("ts") or "")
    baseline = next((e for e in evs if e.get("event") == "baseline"), evs[0])
    changes = [e for e in evs if e.get("event") != "baseline"]
    last = evs[-1]

    uptime = None
    lt = _parse_ts(last.get("ts"))
    if lt:
        uptime = max(0, int((_now() - lt).total_seconds()))

    out_events = []
    for e in reversed(evs[-limit:]):
        out_events.append({
            "ts": e.get("ts"),
            "event": e.get("event"),
            "label": EVENT_LABEL.get(e.get("event"), e.get("event")),
            "pid": e.get("pid"),
            "prev_pid": e.get("prev_pid"),
            "state": e.get("state"),
        })

    return {
        "resident": True,
        "since": baseline.get("ts"),
        "events": out_events,
        "last_event": {"ts": last.get("ts"),
                       "label": EVENT_LABEL.get(last.get("event"), last.get("event")),
                       "pid": last.get("pid"),
                       "state": last.get("state")},
        "uptime_seconds": uptime,
        "changes": len(changes),
    }


def prune():
    """按天数与条数裁剪历史，防止无限增长"""
    evs = _read_all()
    if not evs:
        return 0
    cutoff = _now() - datetime.timedelta(days=KEEP_DAYS)
    kept = []
    for e in evs:
        ts = _parse_ts(e.get("ts"))
        if ts and ts < cutoff:
            continue
        kept.append(e)
    if len(kept) > MAX_EVENTS:
        kept = kept[-MAX_EVENTS:]
    if len(kept) == len(evs):
        return 0
    os.makedirs(DATA, exist_ok=True)
    atomic.write_text(HIST_JSONL, "".join(
        json.dumps(e, ensure_ascii=False) + "\n" for e in kept))
    return len(evs) - len(kept)


if __name__ == "__main__":
    import scan_tasks
    data = scan_tasks.scan()
    new = observe(data.get("tasks") or [])
    print("本轮新增变动事件：%d 条" % len(new))
    for e in new:
        print("  %s  %s  %s  pid %s → %s" % (
            e["ts"], e["task_id"], e["event"], e["prev_pid"], e["pid"]))
    print()
    for t in (data.get("tasks") or []):
        tid = t.get("label") or ""
        s = survival_for(tid)
        if s["resident"] and s["changes"]:
            print("  %-40s 变动 %d 次，最近：%s" % (
                tid, s["changes"], s["last_event"]["label"]))
