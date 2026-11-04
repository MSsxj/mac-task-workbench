#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 自动化运行记录 · 只读数据源（v1.8.0）

只读 ~/.workbuddy/workbuddy.db 的 automation_runs 表，把 WorkBuddy 自己
记下的每次运行转成与 data/runs/*.jsonl 同构的记录，交给 build_runs.py 汇总。

为什么存在：
  run_task.py 那层包装器只能包「跑 Python 脚本」的任务；像「AI 圈资讯简报」
  「AI 论文精读台」这类纯 Agent 任务没有 shell 命令可包，包装器覆盖不了。
  WorkBuddy 自己会把每次自动化运行写进 automation_runs，读它就能让
  **全部**自动任务都有运行历史，且不用改任何 automation 的 prompt。

安全约束（别改坏）：
  * 绝不 SELECT *。runs_json / thread_title / metadata_json / source_cwd 里
    含任务输出正文、工作区路径、会话 ID，一律不取、不写日志、不返回给页面。
  * 只取 automation_id / created_at / updated_at / result_success /
    failure_code / reason_code / status，以及 runs_json 内每条的
    startedAt / finishedAt（只取时间戳，不取其它字段）。
  * error 字段只放枚举码的中文直译 + 原始码，不放任何业务正文。
  * 只读连接（mode=ro），不写库、不新增 / 启停任何任务。
  * 库不可用 / 表结构变更 / 无有效行 → 返回 []，调用方自动退回仅包装器记录。
"""

import datetime
import json
import sqlite3
import wb_automations

DB_PATH = wb_automations.DB_PATH

_SELECT = (
    "SELECT thread_id, automation_id, status, result_success, "
    "failure_code, reason_code, created_at, updated_at, runs_json "
    "FROM automation_runs ORDER BY created_at"
)

MAX_RECORDS = 500   # 防御性上限：只保留最近 500 条，避免日志无限膨胀

# 失败原因码 → 中文直译（仅翻译码名字面含义，原始码一并保留，不臆造业务结论）
_REASON_TEXT = {
    "automation-request-refused": "请求被拒绝",
    "automation-run-failed": "运行失败",
    "automation-rate-limited": "触发限流",
    "automation-recovery-failed": "恢复失败",
    "interrupted": "运行被中断",
    "restart_interrupted": "重启导致中断",
    "session-stuck": "会话卡住",
}

_COLS = ("thread_id", "automation_id", "status", "result_success",
         "failure_code", "reason_code", "created_at", "updated_at", "runs_json")


def _iso(ms):
    """毫秒时间戳 → 本地时间 ISO 字符串（与 jsonl 日志格式一致）"""
    if not ms:
        return None
    try:
        return datetime.datetime.fromtimestamp(float(ms) / 1000).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError, TypeError):
        return None


def _reason(failure_code, reason_code, unfinished):
    """只拼枚举码，不碰任何输出正文"""
    codes = []
    for c in (failure_code, reason_code):
        if c and c not in codes:
            codes.append(_REASON_TEXT.get(c, c) + "（" + str(c) + "）")
    if unfinished:
        codes.append("运行未完成（无结束时间）")
    return " / ".join(codes) if codes else "未提供失败原因"


def _timings(row):
    """从 runs_json 只取起止时间戳；解析不了就回退 created_at / updated_at"""
    started, finished = None, None
    try:
        arr = json.loads(row["runs_json"] or "[]")
        if arr and isinstance(arr[-1], dict):
            started = arr[-1].get("startedAt")
            finished = arr[-1].get("finishedAt")
    except (ValueError, TypeError):
        pass
    return started or row["created_at"], finished


def read_records(valid_ids=None):
    """返回与 data/runs/*.jsonl 同构的记录列表；任何异常都返回 []（降级）"""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=3)
        try:
            rows = con.execute(_SELECT).fetchall()
        finally:
            con.close()
    except Exception:
        return []

    out = []
    for raw in rows:
        r = dict(zip(_COLS, raw))
        aid = r["automation_id"]
        if valid_ids is not None and aid not in valid_ids:
            continue          # 任务已删除的运行记录不进看板
        started_ms, finished_ms = _timings(r)
        started = _iso(started_ms)
        ended = _iso(finished_ms or r["updated_at"])
        success = bool(r["result_success"])
        unfinished = not finished_ms
        out.append({
            "run_id": "wbdb:" + str(r["thread_id"]),
            "task_id": aid,
            "task_name": None,
            "started_at": started,
            "ended_at": ended,
            "duration_ms": None,
            "status": "SUCCESS" if success else "FAILED",
            "step": None,
            "error": None if success else _reason(
                r["failure_code"], r["reason_code"], unfinished),
            "rows": None,
            "trigger": "automation",
            "source": "wb_db",
            "output_files": [],
            "external_written": None,
            "retry_count": 0,
        })
    return out[-MAX_RECORDS:]
