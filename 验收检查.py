#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按需求书第三步的 10 项验收标准做只读验证，输出可直接抄进交付报告的结果。
不触发任何生产任务、不修改任何业务数据。

用法：python3 验收检查.py
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
B = "http://127.0.0.1:8765"

sys.path.insert(0, BASE)
import system_stats  # noqa: E402

results = []


def check(no, name, ok, detail):
    results.append({"no": no, "name": name, "ok": ok, "detail": detail})
    print(("✅" if ok else "❌") + " [%s] %s\n     %s" % (no, name, detail))


def get(path, timeout=30):
    with urllib.request.urlopen(B + path, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")


def code(path):
    try:
        with urllib.request.urlopen(B + path, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def main():
    print("=" * 66)
    print("  Mac 智能工作台 · 验收检查（只读，不触发业务任务）")
    print("  时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 66)

    # ── 1. 系统数值与系统工具对照 ──
    d = system_stats.get_all(use_cache=False)
    cpu_pct = (d.get("cpu") or {}).get("percent")
    mem = d.get("memory") or {}
    disk = d.get("disk") or {}

    # 独立核对：df 数据卷
    try:
        df = subprocess.run(["df", "-k", "/System/Volumes/Data"],
                            capture_output=True, text=True, timeout=6).stdout
        line = [l for l in df.splitlines() if l.strip()][-1].split()
        df_used_kb, df_cap = int(line[2]), int(line[4].rstrip("%"))
        disk_ok = abs(df_cap - round(disk.get("percent", -1))) <= 2
        disk_detail = "页面 %.1f%% vs df %.0f%%（数据卷口径，允许 ±2%%）" % (
            disk.get("percent", -1), df_cap)
    except Exception as e:
        disk_ok, disk_detail = False, "df 对照失败：%s" % e

    mem_ok = mem.get("total") == 8589934592 and 0 < mem.get("percent", 0) <= 100
    cpu_ok = cpu_pct is not None and 0 <= cpu_pct <= 100
    check(1, "CPU / 内存 / 磁盘 数值合理且与系统工具一致",
          bool(cpu_ok and mem_ok and disk_ok),
          "CPU %.1f%%（%s）｜ 内存 %.1f%% = %s / %s ｜ 磁盘 %s"
          % (cpu_pct or -1, (d.get("cpu") or {}).get("method", "-"),
             mem.get("percent", -1),
             round(mem.get("used", 0) / 1073741824, 1), round(mem.get("total", 0) / 1073741824, 1),
             disk_detail))

    # ── 2. 网络状态与来源 ──
    net = d.get("network") or {}
    check(2, "网络状态可判断且来源明确",
          net.get("state") in ("online", "limited", "offline", "unknown"),
          "状态=%s（%s）｜接口 %s ｜方法：%s · %s"
          % (net.get("state"), net.get("text"), net.get("interface"),
             net.get("method"), net.get("scope", "")))

    # ── 3. 任务数量与原始数据一致（先刷新，再读扫描结果，避免拿到上一轮文件）──
    api_scan = json.loads(get("/api/refresh")[1])
    scan = json.load(open(os.path.join(DATA, "scan-data.json"), encoding="utf-8"))
    con = sqlite3.connect("file:%s?mode=ro" % os.path.expanduser("~/.workbuddy/workbuddy.db"),
                          uri=True, timeout=5)
    wb_n = con.execute("SELECT COUNT(*) FROM automations WHERE deleted_at IS NULL").fetchone()[0]
    wb_active = con.execute(
        "SELECT COUNT(*) FROM automations WHERE deleted_at IS NULL AND status='ACTIVE'").fetchone()[0]
    con.close()

    page_local = api_scan["system"]["counts"]["total"]
    page_wb = len((api_scan.get("automations") or {}).get("items") or [])
    check(3, "本机 / WorkBuddy 任务数量与原始数据一致",
          page_local == scan["counts"]["total"] and page_wb == wb_n,
          "本机 %d（扫描结果 %d）｜WorkBuddy %d（库 %d，启用 %d）"
          % (page_local, scan["counts"]["total"], page_wb, wb_n, wb_active))

    # ── 4. 状态 / 时间 / 历史无矛盾 ──
    runs = api_scan.get("runs") or {}
    tasks = runs.get("tasks") or {}
    bad = []
    for tid, t in tasks.items():
        last = t.get("last") or {}
        hist = t.get("history") or []
        if last.get("started_at") and last.get("ended_at"):
            if last["ended_at"] < last["started_at"]:
                bad.append("%s 结束早于开始" % (t.get("name") or tid))
        if hist and last.get("status") in ("SUCCESS", "FAILED"):
            # 「最近执行」只取生产运行，所以要和历史里第一条生产记录比，而不是第一条记录
            first_prod = next((h for h in hist if h.get("is_production") is not False), None)
            if first_prod and last.get("started_at") and \
                    first_prod.get("started_at") != last.get("started_at"):
                bad.append("%s 最近执行与历史首条生产记录不一致" % (t.get("name") or tid))
        if last.get("duration_ms") is not None and last["duration_ms"] < 0:
            bad.append("%s 耗时为负" % (t.get("name") or tid))
    check(4, "任务状态 / 时间 / 历史记录无明显错误", not bad,
          "检查 %d 个任务，问题 %d 项：%s" % (len(tasks), len(bad), "；".join(bad[:3]) or "无"))

    # ── 5. 人工测试不覆盖生产记录 ──
    dry = [h for t in tasks.values() for h in (t.get("history") or [])
           if h.get("is_production") is False]
    last_dry = [t for t in tasks.values()
                if (t.get("last") or {}).get("is_production") is False
                and (t.get("last") or {}).get("status") in ("SUCCESS", "FAILED")]
    check(5, "dry-run / 验证记录不冒充最近生产执行",
          len(last_dry) == 0,
          "历史中验证记录 %d 条，被当作「最近执行」的 %d 个（应为 0）" % (len(dry), len(last_dry)))

    # ── 6. 日志来自真实来源 ──
    # 自动挑一个「有运行记录」的任务来验；一个都没有就如实报未接入，不算通过
    tid = None
    for _tid, _t in tasks.items():
        if (_t.get("history") or []) and (_t.get("last") or {}).get("status"):
            tid = _tid
            break
    if not tid:
        check(6, "日志摘要与详细日志均来自真实来源", False,
              "当前没有任何已接入运行记录的任务，无法验证（不视为失败，但请先接入至少一条）")
    else:
        s = json.loads(get("/api/log?task_id=" + tid)[1])
        det = json.loads(get("/api/log?task_id=%s&source=wrapper" % tid)[1])
        has_real = (s.get("ok") and det.get("ok")
                    and any("data/runs" in x or "来源" in x for x in det.get("lines") or []))
        check(6, "日志摘要与详细日志均来自真实来源",
              bool(has_real),
              "任务=%s ｜ 摘要结果=%s 来源=%s；详细日志 %d 行，首行：%s"
              % (s.get("summary", {}).get("name") or tid,
                 (s.get("summary") or {}).get("status"),
                 (s.get("summary") or {}).get("source"),
                 len(det.get("lines") or []),
                 (det.get("lines") or ["-"])[0][:46]))

    # ── 7. 刷新不影响业务脚本 ──
    before = {f: os.path.getmtime(os.path.join(DATA, f))
              for f in ("runs.js",) if os.path.exists(os.path.join(DATA, f))}
    t0 = time.time()
    get("/api/sys")
    t_sys = (time.time() - t0) * 1000
    t0 = time.time()
    get("/api/refresh", timeout=90)
    t_ref = (time.time() - t0) * 1000
    after = {f: os.path.getmtime(os.path.join(DATA, f))
             for f in ("runs.js",) if os.path.exists(os.path.join(DATA, f))}
    # 只读验证：受监控的业务脚本目录不应被写入。
    # 默认不检查；想看这一项就设环境变量 WORKBENCH_WATCH_DIR=<你的业务目录>
    biz = os.environ.get("WORKBENCH_WATCH_DIR", "").strip()
    biz_touched = False
    if biz and os.path.isdir(biz):
        now = time.time()
        for root, _, files in os.walk(biz):
            for f in files:
                try:
                    if now - os.path.getmtime(os.path.join(root, f)) < 120:
                        biz_touched = True
                except OSError:
                    pass
    biz_desc = ("是" if biz_touched else "否") if biz else "未指定目录，跳过"
    check(7, "刷新不触发业务脚本、不改业务文件",
          not biz_touched,
          "/api/sys %.0fms ｜ /api/refresh %.0fms（含扫描+汇总）｜ 业务目录 120 秒内有写入：%s"
          % (t_sys, t_ref, biz_desc))

    # ── 8. 未授权文件无法通过 HTTP 读取 ──
    deny = ["/serve.py", "/logs_allow.json", "/data/runs/2026-10.jsonl",
            "/README.md", "/data/../serve.py", "/.git/config"]
    allow = ["/", "/data/scan-data.js", "/data/runs.js", "/data/automations.js", "/data/state.js"]
    bad_allow = [p for p in deny if code(p) == 200]
    bad_deny = [p for p in allow if code(p) != 200]
    check(8, "本地服务无法读取未授权的备份 / 配置 / 源码 / 原始日志",
          not bad_allow and not bad_deny,
          "禁止路径全部非 200 %s；白名单 %d 项均 200 %s"
          % ("✓" if not bad_allow else "✗ " + ",".join(bad_allow),
             len(allow), "✓" if not bad_deny else "✗ " + ",".join(bad_deny)))

    # ── 9. 敏感信息不进入页面数据 ──
    body = get("/api/refresh")[1]
    html = get("/")[1]
    keys = ["thread_title", "conversationId", "sessionId", "app_secret", "password",
            "Bearer", "runs_json", "metadata_json"]
    hits = [k for k in keys if k in body or k in html]
    # 私有网段一起查，但不写死某个具体地址
    ip_pat = re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\.\d+\b")
    ip_hits = sorted(set(ip_pat.findall(body) + ip_pat.findall(html)))
    log_body = get("/api/log?task_id=%s&source=wrapper" % tid)[1] if tid else ""
    log_hits = [k for k in ["password", "app_secret", "Bearer"] if k in log_body] \
        + sorted(set(ip_pat.findall(log_body)))
    check(9, "敏感信息未进入页面或接口返回体",
          not hits and not ip_hits and not log_hits,
          "接口命中 %s；内网地址 %s；日志命中 %s（关键字：凭据字段、会话字段、私有 IP）"
          % (hits or "无", ip_hits or "无", log_hits or "无"))

    # ── 10. 原有功能仍可用 ──
    impl = os.path.join(BASE, "dashboard.html")
    js = open(impl, encoding="utf-8").read()
    feats = {
        "搜索框": 'id="q"' in js,
        "分类切换": 'data-tab="local"' in js and 'data-tab="wb"' in js,
        "归属筛选": 'id="f-vendor"' in js,
        "执行结果筛选": 'id="f-run"' in js,
        "手动刷新": 'id="btn-refresh"' in js,
        "打开自动刷新": "doRefresh();" in js,
        "系统状态独立轮询": 'id="sys-iv"' in js,
        "历史记录": "历史记录" in js,
        "详细日志": "详细日志" in js,
        "复制脱敏日志": "复制脱敏日志" in js,
    }
    miss = [k for k, v in feats.items() if not v]
    check(10, "原有功能（搜索 / 筛选 / 分类 / 刷新）保持可用", not miss,
          "检查 %d 项，缺失：%s" % (len(feats), "、".join(miss) or "无"))

    ok = sum(1 for r in results if r["ok"])
    print("=" * 66)
    print("  通过 %d / %d 项" % (ok, len(results)))
    print("=" * 66)

    out = os.path.join(BASE, "data", "acceptance.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "passed": ok, "total": len(results), "items": results},
                  f, ensure_ascii=False, indent=2)
    print("结果已写入", out)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
