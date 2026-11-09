#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
常驻采集器 collect.py  v1.9.0

为什么要有它（这是 2026-11-03 架构审查报告里 E-1 / F-1 的落地）：
  改之前，所有「漏跑 / 卡住」的判定都在浏览器打开页面、请求 /api/refresh 的
  那一刻才算。也就是说：周五 09:00 的任务失败了，只要没人打开看板，
  系统就一声不响 —— 这是「看板」和「监控」的分界线。
  有了这个采集器，判定改为由 launchd 每 5 分钟跑一轮，页面只是读结果。

做三件事：
  1. 采一轮：scan_tasks.scan() + build_runs.build()，全部原子写；
  2. 写心跳：data/state.json / state.js（采集时刻、耗时、摘要、健康度）；
  3. 判告警：连续失败 2 次 / MISSED / STALE / 采集自身异常 →
     追加 data/alerts/ALERT.jsonl + 弹一条 macOS 本地通知（可关）。

刻意不做的事（别加）：
  * 不外发：不发邮件、不推任何外部消息、不上云。数据全部留在本机。
  * 不改业务：不跑任何业务脚本、不改 automation、不碰生产数据。
  * 不接管调度：任务仍由 WorkBuddy / launchd 跑，这里只观测。

用法：
  python3 collect.py              # 采一轮（launchd 每 5 分钟调这个）
  python3 collect.py --quiet      # 静默（launchd 用）
  python3 collect.py --no-notify  # 采一轮但不弹通知（测试用）
  python3 collect.py --selftest   # 只自检数据源，不采集、不写文件

退出码：0 正常；1 采集失败（心跳仍会写下 ok=false，便于看板发现）
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

import atomic
import build_runs
import scan_tasks
import state_history
import wb_automations
import wb_runlog

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
ALERT_DIR = os.path.join(DATA, "alerts")
OUT_JSON = os.path.join(DATA, "state.json")
OUT_JS = os.path.join(DATA, "state.js")
ALERT_LOG = os.path.join(ALERT_DIR, "ALERT.jsonl")
ALERT_STATE = os.path.join(DATA, "alert_state.json")

VERSION = "1.9.0"

# ── 告警参数（克制：只留四条，避免噪音）──
COOLDOWN_HOURS = 6      # 同一条告警 6 小时内不重复弹
ALERT_KEEP_DAYS = 7     # 看板上只展示最近 7 天的告警
ALERT_SHOW_MAX = 20


# ────────────────────────── 告警判定 ──────────────────────────

def _now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse(s):
    try:
        return datetime.datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


def consecutive_failures(task, limit=2):
    """最近连续失败次数（只数真实生产运行；验证记录不算）"""
    n = 0
    for h in task.get("history") or []:
        if not h.get("is_production", True):
            continue
        if h.get("status") == "FAILED":
            n += 1
        else:
            break
    return n


def evaluate_alerts(runs, health):
    """返回本轮命中的告警列表（未去重、未落盘）

    runs = build_runs.build() 的完整返回值（含 tasks 与 history）；
    心跳文件里只放摘要，不放 tasks，避免和 runs.js 重复占空间。
    """
    out = []
    summary = (runs or {}).get("summary") or {}
    tasks = (runs or {}).get("tasks") or {}

    # A1 · 连续失败 2 次
    for tid, t in tasks.items():
        n = consecutive_failures(t)
        if n >= 2:
            last = t.get("last") or {}
            out.append({
                "rule": "failed_twice",
                "level": "error",
                "task_id": tid,
                "task_name": t.get("name"),
                "title": "任务连续失败",
                "detail": "%s 已连续失败 %d 次，最近一次 %s"
                          % (t.get("name"), n, last.get("started_at") or "时间未知"),
            })

    # A2 · 漏跑
    if summary.get("MISSED", 0):
        names = [t.get("name") for t in tasks.values()
                 if (t.get("last") or {}).get("status") == "MISSED"]
        out.append({
            "rule": "missed",
            "level": "warn",
            "task_id": None,
            "task_name": None,
            "title": "任务漏跑",
            "detail": "%d 个任务过了计划时间仍无生产运行记录：%s"
                      % (len(names), "、".join(str(x) for x in names[:5])),
        })

    # A3 · 卡住
    if summary.get("STALE", 0):
        names = [t.get("name") for t in tasks.values()
                 if (t.get("last") or {}).get("status") == "STALE"]
        out.append({
            "rule": "stale",
            "level": "warn",
            "task_id": None,
            "task_name": None,
            "title": "任务疑似卡住",
            "detail": "%d 个任务运行超过阈值仍未结束：%s"
                      % (len(names), "、".join(str(x) for x in names[:5])),
        })

    # A4 · 采集自身异常（数据源读不到 / 扫描有错）
    if not health.get("db_ok"):
        out.append({
            "rule": "db_unavailable",
            "level": "error",
            "task_id": None,
            "task_name": None,
            "title": "运行记录数据源不可用",
            "detail": "读不到 WorkBuddy 本地库运行记录，历史可能不完整（看板已退回包装器日志）",
        })
    if health.get("scan_errors"):
        out.append({
            "rule": "scan_error",
            "level": "warn",
            "task_id": None,
            "task_name": None,
            "title": "本机任务扫描有异常",
            "detail": "%d 个 plist 解析失败或目录不可读" % health["scan_errors"],
        })
    return out


def _load_alert_state():
    return atomic.read_json(ALERT_STATE, default={}) or {}


def _save_alert_state(st):
    try:
        atomic.write_json(ALERT_STATE, st)
    except OSError:
        pass


def _append_alerts(items):
    if not items:
        return
    os.makedirs(ALERT_DIR, exist_ok=True)
    try:
        import fcntl
        with open(ALERT_LOG, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                for a in items:
                    f.write(json.dumps(a, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass


def _notify(title, detail):
    """macOS 本地通知。失败不影响采集（可能没给通知权限）。"""
    text = "%s：%s" % (title, detail)
    text = text.replace("\\", " ").replace('"', "'").replace("\n", " ")
    if len(text) > 180:
        text = text[:177] + "…"
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification "%s" with title "定时任务告警" sound name "Basso"' % text],
            capture_output=True, text=True, timeout=10,
        )
        return True
    except Exception:
        return False


def dispatch_alerts(new_alerts, notify=True):
    """去重 → 落盘 → 通知。返回真正「新」的告警（供看板显示）。"""
    now = datetime.datetime.now()
    st = _load_alert_state()
    fresh = []
    for a in new_alerts:
        key = a["rule"] + ":" + str(a.get("task_id") or a.get("task_name") or "-")
        last = _parse(st.get(key))
        if last and (now - last).total_seconds() < COOLDOWN_HOURS * 3600:
            continue                      # 冷却期内，不重复弹
        a["ts"] = now.strftime("%Y-%m-%d %H:%M:%S")
        a["key"] = key
        st[key] = a["ts"]
        fresh.append(a)

    _append_alerts(fresh)
    _save_alert_state(st)
    if fresh and notify:
        first = fresh[0]
        more = "" if len(fresh) == 1 else "（另有 %d 条）" % (len(fresh) - 1)
        _notify(first["title"], first["detail"] + more)
    return fresh


def read_recent_alerts(days=ALERT_KEEP_DAYS, limit=ALERT_SHOW_MAX):
    """读最近告警供看板展示（只读，不写）"""
    if not os.path.exists(ALERT_LOG):
        return []
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    out = []
    try:
        with open(ALERT_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    a = json.loads(line)
                except ValueError:
                    continue
                t = _parse(a.get("ts"))
                if t and t >= cutoff:
                    out.append(a)
    except OSError:
        return []
    return out[-limit:]


# ────────────────────────── 采集主流程 ──────────────────────────

def collect(notify=True, quiet=True):
    t0 = datetime.datetime.now()
    state = {
        "collected_at": None,
        "version": VERSION,
        "ok": False,
        "duration_ms": None,
        "system": {"total": 0, "running": 0, "failed": 0, "errors": 0},
        "wb": {"total": 0, "active": 0, "source": "none"},
        "runs": {},
        "health": {"db_ok": False, "scan_ok": False, "scan_errors": 0},
        "alerts": [],
        "alert_total": 0,
        "acked_until": _load_alert_state().get("__acked_until__"),
        "error": None,
    }

    # 1) 本机任务
    try:
        data = scan_tasks.scan()
        scan_tasks.write_outputs(data)
        c = data.get("counts") or {}
        state["system"] = {
            "total": c.get("total", 0),
            "running": c.get("running", 0),
            "failed": c.get("failed", 0),
            "errors": len(data.get("errors") or []),
        }
        state["health"]["scan_ok"] = True
        state["health"]["scan_errors"] = len(data.get("errors") or [])

        # 常驻守护进程的存活变动：只在 pid/状态变化时记一条。
        # 对 KeepAlive 的任务，launchd 的 runs/last_exit 永远没有信息量，
        # 「中间有没有掉过」才是这类任务真正可观测的事实。
        try:
            state["state_changes"] = state_history.observe(data.get("tasks") or [])
        except Exception as e:      # 记不下存活变动不能拖垮整轮采集
            state["state_changes"] = []
            state["health"]["state_history_error"] = str(e)
    except Exception as e:      # 扫描失败也要写心跳，否则看板分不清「没跑」和「跑挂了」
        state["error"] = "本机任务扫描失败：%s" % e
        state["health"]["scan_ok"] = False

    # 2) 运行记录汇总 + 3) WB 任务概况
    runs = {}
    try:
        autos = wb_automations.read_live()
        if autos and autos.get("items") is not None:
            items = autos["items"]
            state["wb"] = {
                "total": len(items),
                "active": sum(1 for x in items if x.get("status") == "ACTIVE"),
                "source": "db",
            }
        else:
            state["wb"]["source"] = "snapshot"
        runs = build_runs.build()
        # 心跳里只放摘要（任务明细在 runs.js，不重复存一份）
        state["runs"] = {
            "generated_at": runs.get("generated_at"),
            "summary": runs.get("summary"),
            "sources": runs.get("sources"),
        }
        state["health"]["db_ok"] = bool((runs.get("sources") or {}).get("live"))
    except Exception as e:
        state["error"] = (state["error"] + "；" if state["error"] else "") + \
                         "运行记录汇总失败：%s" % e

    # 4) 告警
    try:
        fresh = dispatch_alerts(evaluate_alerts(runs, state["health"]), notify=notify)
    except Exception:
        fresh = []
    state["alerts"] = read_recent_alerts()
    state["alert_total"] = len(state["alerts"])
    state["new_alerts"] = len(fresh)

    state["ok"] = bool(state["health"]["scan_ok"])
    state["collected_at"] = _now_str()
    state["duration_ms"] = int((datetime.datetime.now() - t0).total_seconds() * 1000)

    # 5) 原子写心跳
    try:
        atomic.write_json(OUT_JSON, state)
        atomic.write_js(OUT_JS, "STATE", state,
                        comment="由 collect.py 生成：采集器心跳 + 摘要 + 告警")
    except OSError as e:
        if not quiet:
            print("写心跳失败：%s" % e)
        return 1

    if not quiet:
        s = state["runs"].get("summary") or {}
        print("[%s] 采集完成 %.1fs · 本机 %d 条 · WB %d 条(%d 启用) · "
              "SUCCESS %s / FAILED %s / MISSED %s"
              % (state["collected_at"], state["duration_ms"] / 1000,
                 state["system"]["total"], state["wb"]["total"], state["wb"]["active"],
                 s.get("SUCCESS", "-"), s.get("FAILED", "-"), s.get("MISSED", "-")))
        for a in fresh:
            print("  ⚠ %s：%s" % (a["title"], a["detail"]))
    return 0 if state["ok"] else 1


# ────────────────────────── 数据源自检 ──────────────────────────

def selftest():
    """WorkBuddy 升级 / 换机器后跑一次，确认数据源还认不认

    这是架构审查报告 F-2 的轻量替代：不做完整的适配层，
    但把「表在不在、字段全不全、能不能映射出状态」检查出来。
    """
    ok = True
    print("─" * 56)
    print("  数据源自检（只读，不采数据、不写文件）")
    print("─" * 56)

    # 1) 库文件
    exists = os.path.exists(wb_automations.DB_PATH)
    print("  %s WorkBuddy 本地库存在" % ("✅" if exists else "❌"))
    ok = ok and exists

    if exists:
        import sqlite3
        try:
            con = sqlite3.connect("file:%s?mode=ro" % wb_automations.DB_PATH,
                                  uri=True, timeout=5)
            try:
                # 2) 表结构
                for table, need in (
                    ("automations", {"id", "name", "status", "rrule", "deleted_at"}),
                    ("automation_runs", {"automation_id", "result_success",
                                         "failure_code", "reason_code",
                                         "created_at", "updated_at"}),
                    ("automation_runtime_state", {"automation_id", "running"}),
                ):
                    try:
                        cols = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
                    except sqlite3.Error:
                        cols = set()
                    if not cols:
                        print("  ❌ 表 %s 不存在" % table)
                        ok = False
                        continue
                    miss = need - cols
                    if miss:
                        print("  ⚠ 表 %s 缺字段：%s" % (table, ", ".join(sorted(miss))))
                        ok = False
                    else:
                        print("  ✅ 表 %s 字段齐全（%d 列，关键字段都在）" % (table, len(cols)))

                # 3) 数据映射
                n_auto = con.execute(
                    "SELECT COUNT(*) FROM automations WHERE deleted_at IS NULL").fetchone()[0]
                n_active = con.execute(
                    "SELECT COUNT(*) FROM automations "
                    "WHERE deleted_at IS NULL AND status='ACTIVE'").fetchone()[0]
                n_runs = con.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0]
                print("  ✅ 有效任务 %d 条（ACTIVE %d），运行记录 %d 行" % (n_auto, n_active, n_runs))

                items = wb_automations.read_live()
                mapped = (items or {}).get("items") or []
                if len(mapped) == n_auto:
                    print("  ✅ wb_automations 映射正常（%d 条）" % len(mapped))
                else:
                    print("  ❌ wb_automations 映射异常：库 %d 条 / 映射出 %d 条"
                          % (n_auto, len(mapped)))
                    ok = False

                recs = wb_runlog.read_records()
                if n_runs and not recs:
                    print("  ❌ wb_runlog 读不出记录（库有 %d 行）" % n_runs)
                    ok = False
                else:
                    print("  ✅ wb_runlog 读出 %d 条记录" % len(recs))
            finally:
                con.close()
        except Exception as e:
            print("  ❌ 打开库失败：%s" % e)
            ok = False

    # 4) 输出目录可写
    try:
        os.makedirs(DATA, exist_ok=True)
        probe = os.path.join(DATA, ".selftest-probe")
        atomic.write_text(probe, "ok")
        os.unlink(probe)
        print("  ✅ data/ 可写（原子写可用）")
    except OSError as e:
        print("  ❌ data/ 不可写：%s" % e)
        ok = False

    print("─" * 56)
    print("  结论：%s" % ("全部通过，可以采集" if ok else "存在问题，先修再采集"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="定时任务工作台 · 常驻采集器")
    ap.add_argument("--quiet", action="store_true", help="静默输出（launchd 用）")
    ap.add_argument("--notify", action="store_true",
                    help="命中告警时额外弹 macOS 本地通知（默认关闭）")
    ap.add_argument("--selftest", action="store_true", help="只自检数据源")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    # 默认不弹通知：需求明确「暂时不做独立告警系统」，异常只在网页上提示。
    # 想开桌面通知再手动加 --notify。
    return collect(notify=a.notify, quiet=a.quiet)


if __name__ == "__main__":
    sys.exit(main())
