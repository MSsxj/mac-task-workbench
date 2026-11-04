#!/bin/bash
# 定时任务工作台 · 服务守护封装
#
# 用途：给 launchd 调用。因为 launchd 运行时的 PATH 很短，
#       直接写 python3 可能找不到解释器，所以要在这里显式挑选。
#       改这个文件后，需要重新加载一次 LaunchAgent 才生效。
#
# ⚠️ 别删：删了它，看板上的「刷新」按钮和自动启动就都失效了。

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
    echo "[$(date)] 没找到 python3，服务无法启动" >&2
    exit 1
fi

exec "$PY" serve.py --no-open --port 8765
