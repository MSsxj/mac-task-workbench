"""任务档案：一句话说明 + 结果去哪看（只读模块）。

数据源是本机私有的 ``task_meta.json``，由人工核对后维护。这个模块：
  * 不读 WorkBuddy 的 prompt，不读任何 SQL，不接触数据库或凭证；
  * 只做 JSON 读取与字段清洗，长度 / 条数都设上限；
  * 文件缺失、格式损坏、字段类型不对时一律降级为「未登记」，不影响抽屉其余部分。

配置文件不入库、不上传；仓库里只放 ``task_meta.example.json`` 作为格式样例。
"""

import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
META_JSON = os.path.join(BASE, "task_meta.json")

MAX_DESC = 300
MAX_LABEL = 180
MAX_PATH = 600
MAX_OUTPUTS = 8
MAX_TASKS = 500
KINDS = {"feishu", "excel", "file", "web", "other"}

_CACHE = None          # (文件指纹, 清洗后的字典, 读取时刻)
_CACHE_TTL = 10        # 秒；编辑档案后最多等 10 秒生效


def _s(value, limit):
    """只接受字符串，去掉首尾空白并截断；其它类型一律当空值。"""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _url(value):
    """只放行 http(s) 链接，避免前端渲染出 javascript: 之类的伪协议。"""
    v = _s(value, MAX_PATH)
    return v if v.lower().startswith(("http://", "https://")) else ""


def _load():
    global _CACHE
    now = time.time()
    if _CACHE and now - _CACHE[2] < _CACHE_TTL and _CACHE[0] == _stamp():
        return _CACHE[1]
    try:
        with open(META_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        tasks = raw.get("tasks") if isinstance(raw, dict) else None
        if not isinstance(tasks, dict):
            tasks = {}
        clean = {}
        for tid, item in list(tasks.items())[:MAX_TASKS]:
            if not isinstance(tid, str) or not isinstance(item, dict):
                continue
            outs = []
            raw_outs = item.get("outputs")
            if not isinstance(raw_outs, list):
                raw_outs = []
            for o in raw_outs[:MAX_OUTPUTS]:
                if not isinstance(o, dict):
                    continue
                label = _s(o.get("label"), MAX_LABEL)
                if not label:
                    continue
                kind = o.get("kind")
                outs.append({
                    "kind": kind if kind in KINDS else "other",
                    "label": label,
                    "path": _s(o.get("path"), MAX_PATH),
                    "url": _url(o.get("url")),
                })
            desc = _s(item.get("desc"), MAX_DESC)
            if not desc and not outs:
                continue
            clean[tid] = {
                "desc": desc,
                "source": _s(item.get("source"), MAX_LABEL),
                "outputs": outs,
                "updated": _s(item.get("updated"), 40),
            }
        _CACHE = (_stamp(), clean, now)
        return clean
    except (OSError, ValueError, TypeError):
        _CACHE = (_stamp(), {}, now)
        return {}


def _stamp():
    try:
        st = os.stat(META_JSON)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def for_task(task_id):
    """给日志摘要用的档案字段；未登记时返回 registered=False。"""
    item = _load().get(str(task_id or ""))
    if not item:
        return {"registered": False}
    out = {"registered": True}
    out.update(item)
    return out


def registered_count():
    return len(_load())
