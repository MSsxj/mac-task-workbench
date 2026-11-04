#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务包装器：包一层再跑真实命令，自动写运行记录。

为什么不直接改生产脚本：
  用包装器可以在「不侵入你的业务脚本」的前提下拿到运行记录，
  回退时只需把 automation prompt 改回原命令即可，生产脚本一行不动。

用法：
  python run_task.py --task-id <id> --name <任务名> \
      [--workdir <目录>] [--timeout 900] [--profile <规则名>] [--dry-run] [--no-finish] \
      -- <真实命令> [参数...]

  --profile    从 task_profiles.json 取一套识别规则（步骤名、行数、是否写了外部系统）
  --dry-run    声明本次不写外部系统（仅记录用，不改变子进程行为）
  --no-finish  故意不写结束记录，仅用于验证看板的 STALE 判定

退出码：透传子进程退出码；子进程被超时杀掉时返回 124。
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runlog  # noqa: E402

# ── 进度 / 结果识别规则 ──
# 不同脚本打印的进度格式差别很大，写死规则换台机器就失效。
# 所以规则放在同目录的 task_profiles.json，用 --profile 指定。
# 不指定 profile 时只用一条通用规则：把输出里的 [1/3] 这种标记当作当前步骤。
BASE = os.path.dirname(os.path.abspath(__file__))
PROFILES_JSON = os.path.join(BASE, "task_profiles.json")
RE_STEP_GENERIC = re.compile(r"\[(\d+)/(\d+)\]")


def load_profile(name):
    """读 task_profiles.json 里的一套规则；读不到就返回空规则（只走通用兜底）"""
    if not name or not os.path.exists(PROFILES_JSON):
        return {}
    try:
        with open(PROFILES_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {}
    return (data.get("profiles") or {}).get(name) or {}


def detect_step(text, prof):
    for item in prof.get("step_marks") or []:
        try:
            if re.search(item.get("pattern", ""), text):
                return item.get("name") or "run"
        except re.error:
            continue
    m = RE_STEP_GENERIC.search(text)
    if m:
        return "%s/%s" % (m.group(1), m.group(2))
    return "run"


def detect_rows(text, prof):
    for pat in prof.get("rows_patterns") or []:
        try:
            m = re.search(pat, text)
        except re.error:
            continue
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                continue
    return None


def detect_external(text, prof, declared_dry_run):
    """判断这次有没有真的写到外部系统。拿不准就返回 None，不猜。"""
    if declared_dry_run:
        return False
    for mark in prof.get("no_write_marks") or []:
        if mark in text:
            return False
    for pat in prof.get("write_patterns") or []:
        try:
            if re.search(pat, text):
                return True
        except re.error:
            continue
    return None


def split_args(argv):
    """把 [--自身参数] -- [子命令] 切开"""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def main():
    argv, child = split_args(sys.argv[1:])
    p = argparse.ArgumentParser()
    p.add_argument("--task-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--workdir", default=None)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--period", default=None)
    p.add_argument("--trigger", default="manual")
    p.add_argument("--profile", default=None,
                   help="task_profiles.json 里的规则名，用于识别步骤/行数/外部写入")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-finish", action="store_true",
                   help="不写结束记录（仅用于验证 STALE）")
    args = p.parse_args(argv)

    if not child:
        print("[run_task] 缺少子命令，用法: ... -- <命令>", file=sys.stderr)
        return 2

    rec = runlog.start(args.task_id, args.name,
                       period=args.period, trigger=args.trigger)
    run_id = rec["run_id"]
    print("[run_task] run_id=%s  status=RUNNING" % run_id)

    t0 = time.time()
    buf = []
    code = None
    killed = False
    try:
        proc = subprocess.Popen(
            child,
            cwd=args.workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                buf.append(line)
            proc.wait(timeout=args.timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            killed = True
            code = 124
    except FileNotFoundError as e:
        buf.append(str(e))
        code = 127
    except Exception as e:  # noqa: BLE001
        buf.append(repr(e))
        code = 1

    out = "".join(buf)
    duration_ms = int((time.time() - t0) * 1000)
    prof = load_profile(args.profile)
    step = detect_step(out, prof)
    rows = detect_rows(out, prof)
    external = detect_external(out, prof, args.dry_run)

    if args.no_finish:
        print("[run_task] --no-finish：不写结束记录（用于验证 STALE）")
        return code or 0

    if killed:
        status, err = "FAILED", "执行超时 %ss，已终止" % args.timeout
    elif code == 0:
        status, err = "SUCCESS", None
    else:
        status = "FAILED"
        tail = [l.strip() for l in buf if l.strip()][-3:]
        err = "退出码 %s | %s" % (code, " / ".join(tail))

    runlog.finish(run_id, status, step=step, error=err, rows=rows,
                  output_files=[], external_written=external,
                  task_id=args.task_id, task_name=args.name)
    print("[run_task] run_id=%s  status=%s  step=%s  rows=%s  耗时=%sms"
          % (run_id, status, step, rows, duration_ms))
    return code or 0


if __name__ == "__main__":
    sys.exit(main())
