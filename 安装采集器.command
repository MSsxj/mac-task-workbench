#!/bin/bash
# 安装「采集器」开机自启（每 5 分钟采一轮系统与任务状态）
# 双击运行即可。装完不需要重启电脑，立刻生效。

cd "$(dirname "$0")" || exit 1
D="$(pwd)"
LABEL="com.taskworkbench.mac.collector"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "──────────────────────────────────────────────"
echo "  安装采集器自启（每 5 分钟一轮）"
echo "──────────────────────────────────────────────"

if [ ! -f "$D/com.taskworkbench.mac.collector.plist" ]; then
    echo "❌ 找不到 plist 模板，安装终止"
    read -n 1 -s -r -p "按任意键关闭…"; exit 1
fi

chmod +x "$D/采集器.sh"
mkdir -p "$HOME/Library/LaunchAgents"

# 先卸掉旧的（存在才卸），避免重复加载报错
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$PLIST"
# 把模板里的 __INSTALL_DIR__ 换成当前实际路径——这样整个文件夹搬到哪都能用
sed "s|__INSTALL_DIR__|$D|g" "$D/com.taskworkbench.mac.collector.plist" > "$PLIST"

if grep -q "__INSTALL_DIR__" "$PLIST" 2>/dev/null; then
    echo "❌ 路径替换失败，plist 里仍残留占位符，安装终止"
    read -n 1 -s -r -p "按任意键关闭…"; exit 1
fi

if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    echo "✅ 已安装并加载：$LABEL"
else
    # 部分环境不允许 bootstrap，退回旧的 load 写法
    if launchctl load -w "$PLIST" 2>/dev/null; then
        echo "✅ 已用旧方式加载：$LABEL"
    else
        echo "⚠ 自动加载失败。请注销后重新登录，或手动执行："
        echo "   launchctl bootstrap gui/\$(id -u) \"$PLIST\""
    fi
fi

echo ""
echo "先跑一轮看看效果…"
"$D/采集器.sh"
echo ""
echo "完成。看板地址： http://127.0.0.1:8765"
echo "说明：采集器只读本机任务与运行记录，不执行任何业务任务。"
echo "──────────────────────────────────────────────"
read -n 1 -s -r -p "按任意键关闭…"
