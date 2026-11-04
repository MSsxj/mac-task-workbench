#!/bin/bash
# 定时任务工作台 · 启动服务（推荐入口）
# 双击运行：启动本地服务 → 自动打开浏览器 → 之后点页面上的「刷新」按钮就能重新扫描
#
# ⚠ 这个终端窗口不要关，关掉就等于服务停止。
# 不用了就在这个窗口按 Ctrl + C 退出。

cd "$(dirname "$0")" || exit 1

# 从 Finder 双击启动时 PATH 很短，这里显式挑一个可用的 python3
PY=""
for c in "$HOME/.workbuddy/binaries/python/envs/default/bin/python" \
         "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
         "/usr/local/bin/python3" \
         "/usr/bin/python3"; do
    if [ -x "$c" ]; then PY="$c"; break; fi
done
[ -z "$PY" ] && PY="$(command -v python3 2>/dev/null)"

if [ -z "$PY" ]; then
    echo "❌ 没找到 python3，无法启动服务。请联系搭建方处理。"
    echo ""
    read -n 1 -s -r -p "按任意键关闭…"
    exit 1
fi

exec "$PY" serve.py
