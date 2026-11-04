#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行记录模块（追加式事件日志）

设计要点：
  1. 只记录状态、步骤、时间、必要汇总与产物路径。
     不记录业务明细、数据库密码、连接字符串、token/secret。
  2. 采用「追加事件、读取合并」模式：start() 追加一条 RUNNING，
     finish() 追加一条同 run_id 的终态记录，读取时按 run_id 合并。
     好处：不需要原地改写文件，多进程并行安全。
  3. 写入用 flock 独占锁，单行写入不会互相截断。

状态：RUNNING / SUCCESS / FAILED / MISSED / STALE
  - MISSED 与 STALE 由 build_runs.py 在读取侧判定，不写入文件。
"""

import datetime
import fcntl
import json
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(BASE, "data", "runs")

# 敏感信息过滤：命中关键字的值一律打码，避免误把凭据写进日志
_SENSITIVE_KEY = re.compile(
    r"(password|passwd|app_secret|secret|token|authorization|bearer|dsn|connect|"
    r"连接串|密码|令牌)", re.I)
_SENSITIVE_VAL = re.compile(
    r"((?:password|passwd|app_secret|secret|token)\s*[=:]\s*)(\S+)", re.I)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.I)
_MAX_ERROR = 200


def sanitize(text):
    """截断并打码，返回可安全写入日志的字符串"""
    if text is None:
        return None
    s = str(text)
    s = _BEARER.sub(r"\1***", s)
    s = _SENSITIVE_VAL.sub(r"\1***", s)
    s = re.sub(r"\b[A-Za-z0-9]{28,}\b", "***", s)
    if len(s) > _MAX_ERROR:
        s = s[:_MAX_ERROR] + "…(截断)"
    return s


def _now():
    return datetime.datetime.now()


def _path(dt=None):
    dt = dt or _now()
    os.makedirs(RUNS_DIR, exist_ok=True)
    return os.path.join(RUNS_DIR, dt.strftime("%Y-%m.jsonl"))


def _append(rec):
    line = json.dumps(rec, ensure_ascii=False, sort_keys=False)
    p = _path()
    with open(p, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return rec


def new_run_id(task_id):
    return "%s_%s_%d" % (
        str(task_id)[:8],
        _now().strftime("%Y%m%d%H%M%S"),
        os.getpid(),
    )


def start(task_id, task_name, period=None, trigger=None):
    """任务开始：追加一条 RUNNING 记录，返回 run_id"""
    rec = {
        "task_id": task_id,
        "task_name": task_name,
        "run_id": new_run_id(task_id),
        "started_at": _now().isoformat(timespec="seconds"),
        "ended_at": None,
        "duration_ms": None,
        "period": period,
        "trigger": trigger,
        "status": "RUNNING",
        "step": "start",
        "error": None,
        "rows": None,
        "output_files": [],
        "external_written": None,
        "retry_count": 0,
    }
    return _append(rec)


def finish(run_id, status, step=None, error=None, rows=None,
           output_files=None, external_written=None, retry_count=0,
           task_id=None, task_name=None):
    """任务结束：追加一条终态记录（SUCCESS / FAILED）

    ⚠️ 必须带上 task_id：终态行是独立一行，若缺失归属字段，
    一旦对应的 start 行丢失或被归档，这条记录就再也关联不到任务。
    """
    if status not in ("SUCCESS", "FAILED"):
        raise ValueError("finish() 只接受 SUCCESS 或 FAILED")
    rec = {
        "run_id": run_id,
        "task_id": task_id,
        "task_name": task_name,
        "ended_at": _now().isoformat(timespec="seconds"),
        "status": status,
        "step": step,
        "error": sanitize(error),
        "rows": rows,
        "output_files": output_files or [],
        "external_written": external_written,
        "retry_count": retry_count,
    }
    return _append(rec)


def read_all(days=None):
    """读取 runs 目录下所有记录（按文件时间由旧到新）"""
    if not os.path.isdir(RUNS_DIR):
        return []
    files = sorted(f for f in os.listdir(RUNS_DIR) if f.endswith(".jsonl"))
    out = []
    for name in files:
        with open(os.path.join(RUNS_DIR, name), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


if __name__ == "__main__":
    import sys
    print("runs 目录:", RUNS_DIR)
    print("记录条数:", len(read_all()))
    for r in read_all()[-10:]:
        print(" ", r.get("run_id"), r.get("status"), r.get("step"), r.get("rows"))
    sys.exit(0)
