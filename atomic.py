#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
原子写工具 v1.0.0

为什么需要它：
  原来 scan_tasks.py / build_runs.py 直接用 open(path, "w") 覆盖数据文件。
  如果正好有另一个进程（另一个浏览器标签页的手动刷新、或采集器的定时轮）
  在同一刻读取，就会读到「写了一半」的 JSON，页面报错或显示残缺数据。
  服务是 ThreadingHTTPServer，并发请求本来就是可能的。

做法：
  先写同目录下的临时文件 → fsync 落盘 → os.replace() 原子替换。
  os.replace 在同一文件系统内是原子操作：读的一方要么看到完整旧文件，
  要么看到完整新文件，不会看到中间状态。

用法：
  import atomic
  atomic.write_text(path, "内容")
  atomic.write_json(path, obj)
  atomic.write_js(path, "__SCAN_DATA__", obj)   # 生成 window.__X__ = {...};
"""

import json
import os
import tempfile


def write_text(path, text):
    """原子写文本文件。失败时清理临时文件，原文件保持不变。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp 建的是 0600，改回常规权限，免得别的用户/工具读不到
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, path)          # ← 原子替换发生在这里
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, obj, indent=2):
    """原子写 JSON 文件（UTF-8，不转义中文）"""
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent))


def write_js(path, var_name, obj, comment=None, indent=2):
    """原子写 `window.__X__ = {...};` 形式的 JS 数据文件

    看板用 <script src> 引入这类文件，是为了绕开 file:// 下 fetch 本地文件
    被浏览器拦截的限制。var_name 传 "SCAN_DATA" 会生成 window.__SCAN_DATA__。
    """
    head = ""
    if comment:
        head = "/* %s */\n" % comment
    body = "%swindow.__%s__ = %s;\n" % (
        head, var_name, json.dumps(obj, ensure_ascii=False, indent=indent))
    write_text(path, body)


def read_json(path, default=None):
    """读 JSON，读不到或解析失败返回 default（用于容忍文件正在被替换的瞬间）"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
