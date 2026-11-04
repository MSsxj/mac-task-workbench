#!/bin/bash
# Mac 智能工作台 · 采集器启动封装
#
# 用途：给 launchd 调用。launchd 的 PATH 很短，直接写 python3 可能找不到解释器，
#       所以这里显式挑选。改这个文件后需要重新加载采集器 LaunchAgent 才生效。
#
# 干的事就一件：采一轮（扫本机任务 + 汇总运行记录 + 写心跳），不跑任何业务任务。

cd "$(cd "$(dirname "$0")" && pwd)" || exit 1

PY=""
for c in "$HOME/.workbuddy/binaries/python/envs/default/bin/python" \
         "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
         "/usr/local/bin/python3" \
         "/usr/bin/python3"; do
    if [ -x "$c" ]; then PY="$c"; break; fi
done
[ -z "$PY" ] && PY="$(command -v python3 2>/dev/null)"

if [ -z "$PY" ]; then
    echo "[$(date)] 没找到 python3，采集器无法运行" >&2
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 采集开始"
"$PY" collect.py --quiet
rc=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 采集结束 rc=$rc"
exit $rc
