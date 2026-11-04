#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
汇总两类运行记录，叠加 MISSED / STALE 判定，生成看板可读的 data/runs.js

两类来源：
  1. data/runs/*.jsonl —— run_task.py 包装器为「跑 Python 脚本」的任务写下的日志；
     只对主动接入包装器的任务有记录，其余任务为空。
  2. ~/.workbuddy/workbuddy.db 的 automation_runs —— WorkBuddy 自己记录的每次
     自动化运行，由 wb_runlog.py 只读转换。**覆盖全部自动任务**，包括纯 Agent
     任务（无 shell 命令可包），且不需要改任何 automation 的 prompt。

关键约定：
  * 预期运行时间**只从 WorkBuddy 本地库（降级时用 data/automations.js）的 rrule 现算**，不另维护时间表，
    避免与实际计划不一致。
  * MISSED / STALE 是读取侧的衍生状态，不写回日志文件。
  * 日志只保留 90 天；加 --prune 才会真正删除过期文件（默认只报告）。
  * 本地库读不到时 wb_runlog 返回 []，看板自动退回「仅包装器记录」，不报错。

用法：
  python build_runs.py            # 生成 data/runs.js
  python build_runs.py --prune    # 顺带清理超过保留期的日志（保留现有记录，不删目录）
"""

import argparse
import datetime
import json
import os
import sys
import atomic
import wb_automations
import wb_runlog

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
RUNS_DIR = os.path.join(DATA, "runs")
ARCHIVE_DIR = os.path.join(RUNS_DIR, "_verification")   # 10-30 人工验证记录归档处
AUTOMATIONS_JS = os.path.join(DATA, "automations.js")
OUT_JS = os.path.join(DATA, "runs.js")

STALE_MINUTES = 30      # 有 RUNNING 但超过这个时长没有结束记录 → STALE
MISS_GRACE_MINUTES = 120  # 预期时间过了这么久还没有任何记录 → MISSED
RETAIN_DAYS = 90
HISTORY_KEEP = 10

WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def parse_rrule(s):
    d = {}
    for part in (s or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip().upper()] = v.strip()
    return d


def last_expected(rrule, now):
    """按 rrule 算出最近一次「应跑」的时间点（<= now）"""
    d = parse_rrule(rrule)
    freq = d.get("FREQ", "").upper()
    try:
        hh = int(d.get("BYHOUR", 0))
        mm = int(d.get("BYMINUTE", 0))
    except ValueError:
        return None
    if freq == "DAILY":
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand > now:
            cand -= datetime.timedelta(days=1)
        return cand
    if freq == "WEEKLY":
        days = [WD[x] for x in d.get("BYDAY", "MO").split(",") if x in WD]
        if not days:
            return None
        for i in range(0, 8):
            day = (now - datetime.timedelta(days=i)).date()
            if day.weekday() in days:
                cand = datetime.datetime.combine(day, datetime.time(hh, mm))
                if cand <= now:
                    return cand
        return None
    return None


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


# ── 生产记录 / 验证记录 的区分 ──
# 背景：手工验证留下的 trigger=dryrun-verify / manual-verify 记录，
# 如果混进「最近执行」，看上去就像任务真的跑过 —— 这是在骗人。
# 现在给每条记录打 is_production 标记：dry-run / 试跑不占「最近执行」主位，
# 也不能用它来抵消 MISSED 判定。
_NON_PROD_TRIGGERS = ("dryrun", "dry-run", "dry_run", "test", "verify", "demo")


def mark_production(run):
    trig = str(run.get("trigger") or "").lower()
    run["is_production"] = not any(t in trig for t in _NON_PROD_TRIGGERS)
    # 归档记录一律不算生产：它们只是「历史上发生过的人工运行」，可以看，不能顶替生产
    if run.get("archived"):
        run["is_production"] = False
    return run


def load_automations():
    # 优先读 WorkBuddy 本地库（实时反映启停），读不到才降级到静态快照。
    # 必须与 serve.py 用同一个源，否则会出现「页面显示已暂停、运行汇总仍按启用判 MISSED」。
    live = wb_automations.read_live()
    if live:
        return live["items"]
    if not os.path.exists(AUTOMATIONS_JS):
        return []
    txt = open(AUTOMATIONS_JS, encoding="utf-8").read()
    start = txt.find("[")
    end = txt.rfind("]")
    if start < 0 or end < 0:
        return []
    try:
        return json.loads(txt[start:end + 1])
    except ValueError:
        return []


def load_records(include_archive=True):
    """读包装器日志。

    10-30 的人工验证记录被归档到 data/runs/_verification/ 下（不污染「最近执行」），
    但它们**仍然属于真实发生过的运行**，看历史时应该看得到。
    所以这里一并读入，打 archived 标记——只进历史，不参与生产运行判定。
    """
    out = []

    def read_dir(d, archived=False):
        if not os.path.isdir(d):
            return
        for name in sorted(f for f in os.listdir(d) if f.endswith(".jsonl")):
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

    read_dir(RUNS_DIR)
    if include_archive:
        read_dir(ARCHIVE_DIR, archived=True)
    return out


def merge_runs(records, autos=None):
    """同一 run_id 的多条事件合并成一条（后写的覆盖先写的）

    兜底：终态行若缺失 task_id（早期版本 finish() 未写该字段，或 start 行已归档），
    从 run_id 前缀（task_id 前 8 位）反查归属，避免记录变成无主任务。
    """
    runs = {}
    order = []
    for r in records:
        rid = r.get("run_id")
        if not rid:
            continue
        if rid not in runs:
            runs[rid] = dict(r)
            order.append(rid)
        else:
            runs[rid].update({k: v for k, v in r.items() if v is not None})
    out = [runs[rid] for rid in order]
    if autos:
        prefix = {str(a.get("id"))[:8]: a for a in autos if a.get("id")}
        for r in out:
            if not r.get("task_id"):
                a = prefix.get(str(r.get("run_id")).split("_")[0])
                if a:
                    r["task_id"] = a["id"]
                    r["task_name"] = r.get("task_name") or a.get("name")
    return out


def decorate(run, now, stale_minutes=STALE_MINUTES):
    """补齐展示字段；RUNNING 超时转 STALE"""
    st = run.get("started_at")
    en = run.get("ended_at")
    d1, d2 = parse_dt(st), parse_dt(en)
    if d1 and d2:
        run["duration_ms"] = int((d2 - d1).total_seconds() * 1000)
    if run.get("status") == "RUNNING" and d1:
        age = (now - d1).total_seconds() / 60
        if age > stale_minutes:
            run["status"] = "STALE"
            run["stale_minutes"] = int(age)
    return run


def build(prune=False, stale_minutes=STALE_MINUTES, now=None):
    now = now or datetime.datetime.now()
    autos = load_automations()
    jsonl = load_records()
    valid_ids = {str(a.get("id")) for a in autos if a.get("id")}
    db_runs = wb_runlog.read_records(valid_ids=valid_ids)
    runs = [decorate(mark_production(r), now, stale_minutes)
            for r in merge_runs(jsonl + db_runs, autos)]

    by_task = {}
    for r in runs:
        by_task.setdefault(r.get("task_id"), []).append(r)

    tasks = {}
    summary = {"total": len(runs), "SUCCESS": 0, "FAILED": 0,
               "STALE": 0, "MISSED": 0, "RUNNING": 0, "NOT_INSTRUMENTED": 0,
               "VERIFICATION": 0}

    for r in runs:
        s = r.get("status")
        if s in summary:
            summary[s] += 1
    summary["VERIFICATION"] = sum(1 for r in runs if not r.get("is_production"))

    for a in autos:
        tid = a.get("id")
        lst = sorted(by_task.get(tid, []),
                     key=lambda x: parse_dt(x.get("started_at")) or now)
        # prod = 真实生产运行。「最近执行」只认它；只有验证记录时不占主位。
        prod = [r for r in lst if r.get("is_production")]
        last = prod[-1] if prod else None
        if last is None and lst:
            last = dict(lst[-1])
            note = "仅有验证/试跑记录，尚无生产运行"
            last["error"] = note + ("；" + str(last["error"]) if last.get("error") else "")
        expected = last_expected(a.get("rrule"), now) if a.get("rrule") else None

        if a.get("status") == "ACTIVE":
            if not lst:
                # 「从未接入运行记录」与宽限期无关——没接入就是不知道，任何时候都一样
                last = {
                    "run_id": None,
                    "task_id": tid,
                    "task_name": a.get("name"),
                    "status": "NOT_INSTRUMENTED",
                    "expected_at": expected.isoformat(timespec="minutes") if expected else None,
                    "started_at": None,
                    "ended_at": None,
                    "step": None,
                    "error": "该任务尚未接入运行记录，无法确认是否执行",
                    "rows": None,
                }
                summary["NOT_INSTRUMENTED"] += 1
            elif expected:
                # MISSED：已接入，但预期时间过了宽限期仍没有该次「生产」运行记录
                gap = (now - expected).total_seconds() / 60
                if gap > MISS_GRACE_MINUTES:
                    has_after = False
                    for r in prod:
                        d = parse_dt(r.get("started_at"))
                        if d and d >= expected - datetime.timedelta(hours=1):
                            has_after = True
                            break
                    if not has_after:
                        why = ("计划运行时间已过，但没有本次的生产运行记录"
                               if prod else
                               "计划运行时间已过，只有验证/试跑记录，没有生产运行记录")
                        last = {
                            "run_id": None,
                            "task_id": tid,
                            "task_name": a.get("name"),
                            "status": "MISSED",
                            "expected_at": expected.isoformat(timespec="minutes"),
                            "started_at": None,
                            "ended_at": None,
                            "step": None,
                            "error": why,
                            "rows": None,
                        }
                        summary["MISSED"] += 1

        tasks[tid] = {
            "name": a.get("name"),
            "last": last,
            "expected_at": expected.isoformat(timespec="minutes") if expected else None,
            "history": list(reversed(lst))[:HISTORY_KEEP],
        }

    pruned = []
    if prune and os.path.isdir(RUNS_DIR):
        cutoff = now - datetime.timedelta(days=RETAIN_DAYS)
        for name in os.listdir(RUNS_DIR):
            if not name.endswith(".jsonl"):
                continue
            p = os.path.join(RUNS_DIR, name)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(p))
            if mtime < cutoff:
                os.remove(p)
                pruned.append(name)

    payload = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "stale_minutes": stale_minutes,
        "miss_grace_minutes": MISS_GRACE_MINUTES,
        "retain_days": RETAIN_DAYS,
        "summary": summary,
        "sources": {"jsonl_events": len(jsonl), "wb_db": len(db_runs),
                    "merged": len(runs), "live": bool(db_runs)},
        "tasks": tasks,
        "pruned": pruned,
    }
    # 原子写：并发刷新时读的一方不会读到半截 JSON
    atomic.write_js(OUT_JS, "RUNS", payload,
                    comment="由 build_runs.py 生成：包装器日志 + WorkBuddy 本地库运行记录")
    return payload


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prune", action="store_true", help="清理超过保留期的日志")
    ap.add_argument("--stale-minutes", type=int, default=STALE_MINUTES,
                    help="STALE 判定阈值，默认 30；仅验证时调小")
    a = ap.parse_args()
    p = build(prune=a.prune, stale_minutes=a.stale_minutes)
    print("已生成:", OUT_JS)
    print("汇总:", json.dumps(p["summary"], ensure_ascii=False))
    print("来源:", json.dumps(p["sources"], ensure_ascii=False))
    for tid, t in p["tasks"].items():
        last = t.get("last")
        if last:
            print("  %-8s %-22s %s" % (str(tid)[:8], (t.get("name") or "")[:22],
                                       last.get("status")))
    sys.exit(0)
