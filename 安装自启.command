#!/bin/bash
# 定时任务工作台 · 安装开机自启
#
# 双击运行一次即可。之后每次登录，本地服务会自动在后台启动，
# 打开 http://127.0.0.1:8765 就能直接点「刷新」按钮。
#
# ⚠️ 首次双击若被 macOS 拦（"无法打开，因为来自身份不明的开发者"）：
#    右键本文件 → 选「打开」→ 再点一次「打开」。之后就正常了。
# ⚠️ 为什么必须你自己双击：WorkBuddy 的运行环境没有向 launchd 注册服务的权限，
#    只有你自己打开的「终端」App 才有。脚本在你双击后由终端执行，所以能成功。

set -u

WB="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.taskworkbench.mac.plist"
LABEL="com.taskworkbench.mac"
DOMAIN="gui/$(id -u)"
URL="http://127.0.0.1:8765"

echo "────────────────────────────────────────"
echo "  定时任务工作台 · 安装开机自启"
echo "────────────────────────────────────────"
echo ""

# 0. 前置检查
if [ ! -f "$WB/serve.py" ]; then
    echo "❌ 找不到 serve.py：$WB"
    echo "   工作台文件夹可能被移动过，请先确认位置。"
    echo ""
    read -n 1 -s -r -p "按任意键关闭…"
    exit 1
fi
if [ ! -f "$WB/服务守护.sh" ]; then
    echo "❌ 找不到 服务守护.sh：$WB"
    echo ""
    read -n 1 -s -r -p "按任意键关闭…"
    exit 1
fi

# 1. 生成 plist。
#    注意：必须在本脚本（由终端执行）里生成，
#    这样文件不会被系统打上"来源不明"标记，launchd 才会认。
echo "① 生成启动配置…"
rm -f "$PLIST"
defaults write "$PLIST" Label            -string "$LABEL"
defaults write "$PLIST" ProgramArguments -array "/bin/bash" "$WB/服务守护.sh"
defaults write "$PLIST" RunAtLoad        -bool  true
defaults write "$PLIST" KeepAlive        -bool  true
defaults write "$PLIST" ThrottleInterval -int   10
defaults write "$PLIST" StandardOutPath  -string /tmp/taskscheduler-workbench.log
defaults write "$PLIST" StandardErrorPath -string /tmp/taskscheduler-workbench.log
# 顺手去掉可能附带的隔离属性
xattr -d com.apple.quarantine  "$PLIST" 2>/dev/null
xattr -d com.apple.provenance  "$PLIST" 2>/dev/null
echo "   $PLIST"

# 2. 注册并启动
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "② 服务已注册，正在重启…"
    launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null
else
    echo "② 正在注册服务…"
    if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
        echo ""
        echo "❌ 注册失败。"
        echo "   常见原因：系统安全策略拦截。"
        echo "   请试试：完全退出「终端」App 后重新双击本文件。"
        echo ""
        read -n 1 -s -r -p "按任意键关闭…"
        exit 1
    fi
fi

# 3. 等起来并验证
echo "③ 等待服务就绪…"
OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    CODE=$(curl -s -o /dev/null -w "%{http_code}" --noproxy '*' --max-time 3 "$URL/" 2>/dev/null)
    if [ "$CODE" = "200" ]; then OK=1; break; fi
    sleep 1
done

echo ""
echo "────────────────────────────────────────"
if [ "$OK" = "1" ]; then
    echo "✅ 安装完成"
    echo ""
    echo "   访问地址 ：$URL"
    echo "   生效范围 ：以后每次登录自动启动，无需手动操作"
    echo "   日志位置 ：/tmp/taskscheduler-workbench.log"
    echo ""
    echo "   现在就把看板打开？"
    open "$URL"
else
    echo "⚠️ 服务注册了，但暂时没响应"
    echo ""
    echo "   先看日志排查："
    echo "   cat /tmp/taskscheduler-workbench.log"
    echo ""
    echo "   手动试运行看报错："
    echo "   cd \"$WB\" && bash 服务守护.sh"
fi
echo "────────────────────────────────────────"
echo ""
read -n 1 -s -r -p "按任意键关闭…"
echo ""
