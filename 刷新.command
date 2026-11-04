#!/bin/bash
# 定时任务工作台 · 一键刷新
# 双击运行：重新扫描本机定时任务 → 生成数据 → 自动打开看板
# 全程只读，不会修改 / 停用 / 删除任何任务

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
    echo "❌ 没找到 python3，无法扫描。请联系搭建方处理。"
    echo ""
    read -n 1 -s -r -p "按任意键关闭…"
    exit 1
fi

echo "使用 Python：$("$PY" -V 2>&1)"
echo ""

"$PY" scan_tasks.py
RC=$?

# 顺带重算运行记录汇总（MISSED / STALE 判定），失败不影响看板打开
"$PY" build_runs.py
echo ""

if [ $RC -eq 0 ]; then
    echo "正在打开看板…"
    open "dashboard.html"
else
    echo "❌ 扫描失败（退出码 $RC）。请检查上面的报错信息。"
fi

echo ""
read -n 1 -s -r -p "按任意键关闭…"
echo ""
exit $RC
