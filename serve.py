#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mac 智能工作台 · 本地服务 serve.py  v2.0.0

存在原因：浏览器不允许 file:// 页面调用本机脚本，所以需要一个本地桥接服务。

安全边界（写死在这里，别改坏）：
  1. 只绑定 127.0.0.1 —— 局域网、公网都访问不到
  2. 静态文件走**白名单**：只放行看板页与 4 个生成的 JS 数据文件。
     备份目录、源码、配置、原始日志、shell 脚本一律 404 ——
     以前是「工作台目录内任意文件都能下」，那是真实漏洞，v2.0.0 已堵。
  3. 不提供任何启停 / 删除 / 新建定时任务的接口
  4. 读 WorkBuddy 本地库只取 id/name/status/rrule 与运行结果枚举码，
     绝不读 prompt / runs_json / thread_title
  5. 日志接口只按**服务端定义**的 source_id 读取，前端不能传路径；
     真实路径由 logview.py 的白名单校验，且内容脱敏、截断
  6. 跨域只允许同源：不再返回 Access-Control-Allow-Origin: *
"""

import argparse
import json
import os
import subprocess
import sys
import time

import atomic
import collect as collector
import logview
import system_stats
import wb_automations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import JSONDecoder
from urllib.parse import urlparse, unquote, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
VERSION = "2.0.1"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
}

# 静态文件白名单：只有这些能被浏览器取到
STATIC_ALLOW = {
    "dashboard.html",
    "data/scan-data.js",
    "data/automations.js",
    "data/runs.js",
    "data/state.js",
}


# ────────────────────────── 数据读取 ──────────────────────────

def read_js_var(path):
    """从 `window.__XXX__ = {...};` 这种文件里取出 JSON 对象体"""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
        i = txt.index("=")
        obj, _ = JSONDecoder().raw_decode(txt[i + 1:].strip())
        return obj
    except Exception:
        return None


def read_automations():
    return (wb_automations.read_live()
            or read_js_var(os.path.join(DATA, "automations.js")))


def automations_js_text():
    """首屏 <script src="data/automations.js">：服务在时直接给实时数据"""
    live = wb_automations.read_live()
    if live:
        return ("/* 由 serve.py 实时生成：WorkBuddy 本地库只读 4 列，非磁盘快照 */\n"
                "window.__AUTOMATIONS__ = "
                + json.dumps(live, ensure_ascii=False, indent=2) + ";\n")
    try:
        with open(os.path.join(DATA, "automations.js"), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "window.__AUTOMATIONS__ = null;\n"


def state_js_text():
    st = atomic.read_json(os.path.join(DATA, "state.json"), default=None)
    if st is None:
        return ("/* 采集器尚未跑过第一轮 */\n"
                "window.__STATE__ = null;\n")
    return ("/* 由 serve.py 读取 data/state.json 生成：采集器心跳 */\n"
            "window.__STATE__ = " + json.dumps(st, ensure_ascii=False, indent=2) + ";\n")


# ────────────────────────── HTTP 处理 ──────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "MacWorkbench/" + VERSION

    def log_message(self, fmt, *args):
        pass

    # ── 路由 ──
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)
        if path == "/api/refresh":
            self.handle_refresh()
        elif path == "/api/sys":
            self.handle_sys()
        elif path == "/api/log":
            self.handle_log(qs)
        elif path in ("/", "/index.html"):
            self.serve_data_static("dashboard.html")
        else:
            self.serve_static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _headers(self, ctype, length):
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        # 只允许本页自身用内联样式/脚本，禁止外链任何资源
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; "
                         "connect-src 'self'; frame-ancestors 'self'")

    # ── 刷新（采集一轮：本机任务 + 运行记录 + 心跳）──
    def handle_refresh(self):
        t0 = time.time()
        try:
            rc = collector.collect(notify=False, quiet=True)
        except Exception:
            rc = 1
        system = atomic.read_json(os.path.join(DATA, "scan-data.json"), default=None)
        runs = read_js_var(os.path.join(DATA, "runs.js"))
        autos = read_automations()
        state = atomic.read_json(os.path.join(DATA, "state.json"), default=None)
        ok = bool(system) and rc == 0
        payload = {
            "ok": ok,
            "system": system,
            "automations": autos,
            "wb_source": "db" if (autos or {}).get("live") else "snapshot",
            "runs": runs,
            "state": state,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
        if not ok:
            payload["error"] = (state or {}).get("error") or "采集未完成，见心跳"
        print("  [刷新] %s · %d 条任务 · %.1fs"
              % ("完成" if ok else "部分失败",
                 ((system or {}).get("counts") or {}).get("total", 0),
                 payload["elapsed_ms"] / 1000.0), flush=True)
        self.send_json(payload)

    # ── 系统状态（轻量，可单独高频轮询）──
    def handle_sys(self):
        try:
            data = system_stats.get_all()
            self.send_json({"ok": True, "sys": data})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    # ── 日志（两级：摘要 / 明细）──
    def handle_log(self, qs):
        tid = (qs.get("task_id") or [""])[0]
        src = (qs.get("source") or [""])[0]
        if not tid:
            self.send_json({"ok": False, "error": "缺少 task_id"})
            return
        try:
            if src:
                self.send_json(logview.read_source(tid, src))
            else:
                self.send_json(logview.summary_for(tid))
        except Exception:
            self.send_json({"ok": False, "error": "读取日志时出错"})

    # ── 静态文件 ──
    # 首次运行（data/ 还是空的）时，用结构完整的空壳顶上，
    # 前端照常渲染「暂无数据」，不会因为 404 卡在加载中。
    EMPTY_JS = {
        "data/runs.js": ('window.__RUNS__ = {"generated_at": null, "stale_minutes": 30,'
                         ' "miss_grace_minutes": 120, "retain_days": 90,'
                         ' "summary": {}, "sources": {}, "tasks": {}, "pruned": []};\n'),
        "data/scan-data.js": ('window.__SCAN_DATA__ = {"generated_at": null, "counts": {},'
                              ' "tasks": [], "errors": [], "macos": null, "scan_scope": {}};\n'),
    }

    def ensure_data_file(self, rel, full):
        """缺失的数据文件现场生成一份；失败就静默，交给 EMPTY_JS 兜底"""
        try:
            if rel == "data/runs.js":
                import build_runs
                build_runs.build()
            elif rel == "data/scan-data.js":
                import scan_tasks
                scan_tasks.write_outputs(scan_tasks.scan())
        except Exception:
            pass

    def serve_data_static(self, rel):
        """按白名单提供静态文件（数据 JS 走实时生成）"""
        if rel not in STATIC_ALLOW:
            self.send_error(404)
            return
        if rel == "data/automations.js":
            body = automations_js_text().encode("utf-8")
        elif rel == "data/state.js":
            body = state_js_text().encode("utf-8")
        else:
            full = os.path.join(BASE, rel)
            if not os.path.isfile(full):
                # 首次运行（刚 clone 下来、data/ 还是空的）时不要让首屏 404。
                # 能算就现算一份，算不出来也给一个结构完整的空壳，前端照常渲染空状态。
                self.ensure_data_file(rel, full)
            if not os.path.isfile(full):
                body = self.EMPTY_JS.get(rel, b"").encode("utf-8")
            else:
                try:
                    with open(full, "rb") as f:
                        body = f.read()
                except OSError:
                    self.send_error(500)
                    return
        ext = os.path.splitext(rel)[1].lower()
        self.send_response(200)
        self._headers(MIME.get(ext, "application/octet-stream"), len(body))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def serve_static(self, path):
        rel = unquote(path).lstrip("/")
        self.serve_data_static(rel)

    def send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


# ────────────────────────── 启动 ──────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Mac 智能工作台本地服务")
    ap.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    port = args.port
    httpd = None
    for _ in range(12):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    if httpd is None:
        print("❌ %d ~ %d 端口都被占用了，换个端口试试。" % (args.port, args.port + 11))
        return 1

    # 后台每 2 秒采一次 CPU，接口读结果 → 请求不用等采样
    system_stats.start_sampler(2.0)

    url = "http://127.0.0.1:%d" % port
    print("─" * 58)
    print("  Mac 智能工作台 · 本地服务 v%s 已启动" % VERSION)
    print("─" * 58)
    print("  访问地址 ：%s" % url)
    print("  监听范围 ：127.0.0.1（只本机，局域网访问不到）")
    print("  能力范围 ：系统状态采集 + 只读任务扫描 + 只读运行/日志（白名单）")
    print("  不做的事 ：不启停任务、不读凭据、不对外发送任何数据")
    print("─" * 58)
    print("")

    if not args.no_open:
        subprocess.Popen(["open", url])

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 已手动停止服务。")
    finally:
        system_stats.stop_sampler()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
