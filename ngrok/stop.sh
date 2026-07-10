#!/usr/bin/env bash
# 停止 ngrok — 服务模式和前台/nohup 模式都能停
if systemctl is-active --quiet ngrok 2>/dev/null; then
    sudo ngrok service stop
    echo ">> ngrok 服务已停止 (重新启动: sudo ngrok service start)"
elif pkill -x ngrok 2>/dev/null; then
    echo ">> ngrok 进程已结束"
else
    echo ">> 没有在运行的 ngrok"
fi
