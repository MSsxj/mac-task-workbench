#!/bin/bash
# 卸载「采集器」自启（看板仍可用，只是不再自动更新心跳）

LABEL="com.taskworkbench.mac.collector"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "──────────────────────────────────────────────"
echo "  卸载采集器自启"
echo "──────────────────────────────────────────────"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
launchctl unload -w "$PLIST" 2>/dev/null
rm -f "$PLIST"

if [ -f "$PLIST" ]; then
    echo "⚠ 未能删除 $PLIST，请手动删除"
else
    echo "✅ 已卸载：$LABEL"
fi
echo ""
echo "看板与本地服务不受影响： http://127.0.0.1:8765"
echo "只是不会每 5 分钟自动采集了，打开页面时会刷一次。"
echo "──────────────────────────────────────────────"
read -n 1 -s -r -p "按任意键关闭…"
