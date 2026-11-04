#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 任务列表 · 只读实时数据源

只做一件事：只读 ~/.workbuddy/workbuddy.db 的 automations 表，
取 id / name / status / rrule 四列、过滤软删除（deleted_at IS NULL），
返回与 data/automations.js 的 items 同构的列表。

为什么存在：
  页面上的「🔄 刷新」原先只重扫本机 launchd 任务，WorkBuddy 那张表读的是
  手抄快照 data/automations.js，启停状态一变快照就过期，必须人工喊一句
  「刷新定时任务工作台里的 WorkBuddy 任务列表」重抄一遍。
  本地库与客户端实时同步，改读它就能让「打开看板」第一屏就是准的。

安全约束（别改坏）：
  * 绝不 SELECT * —— prompt 字段含完整业务提示词，可能包含内网信息，
    绝不能经本地 HTTP 服务返回给页面。
  * 只读连接（mode=ro），不写库、不新建/启停任何任务、不注册定时任务。
  * 库不存在 / 被锁 / 表结构变更 / 查不到有效行 → 一律返回 None，
    由调用方降级到静态快照（快照里的 generated_at 会让页面标红提醒）。
"""

import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy.db")

# 只取这四列，且排除软删除行；状态值原样透传，不臆造
_SELECT = (
    "SELECT id, name, status, rrule FROM automations "
    "WHERE deleted_at IS NULL AND id IS NOT NULL AND name IS NOT NULL"
)


def read_live():
    """返回 {generated_at, source, live, items}；任何异常都返回 None（交给调用方降级）"""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=3)
        try:
            rows = con.execute(_SELECT).fetchall()
        finally:
            con.close()
    except Exception:
        return None

    items = [
        {"id": r[0], "name": r[1], "status": r[2], "rrule": r[3] or ""}
        for r in rows
    ]
    if not items:
        return None
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "source": "WorkBuddy 本地库 workbuddy.db（只读，实时）",
        "live": True,
        "items": items,
    }


def read_items():
    """只要 items 列表；实时源不可用返回 None"""
    d = read_live()
    return d["items"] if d else None
