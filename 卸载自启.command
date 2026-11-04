#!/bin/bash
# 定时任务工作台 · 取消开机自启
#
# 双击运行：停止本地服务，并从登录项中移除。
# 只是取消自启，工作台文件和扫描脚本都会保留，
# 需要看数据随时还能双击「刷新.command」。

set -u

PLIST="$HOME/Library/LaunchAgents/com.taskworkbench.mac.plist"
LABEL="com.taskworkbench.mac"
DOMAIN="gui/$(id -u)"

echo "────────────────────────────────────────"
echo "  定时任务工作台 · 取消开机自启"
echo "────────────────────────────────────────"
echo ""

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "① 停止服务…"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && echo "   已停止" || echo "   停止失败，可能需要重启电脑"
else
    echo "① 服务未在运行，跳过"
fi

if [ -f "$PLIST" ]; then
    echo "② 删除启动配置…"
    rm -f "$PLIST" && echo "   已删除 $PLIST"
else
    echo "② 启动配置不存在，跳过"
fi

rm -f /tmp/taskscheduler-workbench.log 2>/dev/null

echo ""
echo "────────────────────────────────────────"
echo "✅ 已取消开机自启"
echo ""
echo "   注意：看板上的「刷新」按钮依赖这个服务，"
echo "   取消后按钮会失效，改用「刷新.command」扫描即可。"
echo "   想恢复：双击「安装自启.command」。"
echo "────────────────────────────────────────"
echo ""
read -n 1 -s -r -p "按任意键关闭…"
echo ""
