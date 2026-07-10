#!/usr/bin/env bash
# 启动 ngrok 常驻服务 (systemd: 开机自启+崩溃自愈, 与 SSH/终端无关) — 与 stop.sh 对称
# 前台调试模式 (现场看转发日志, Ctrl+C 退出, 跟随会话): ./start.sh --fg
set -e
cd "$(dirname "$0")"
[ -f ngrok.yml ] || { echo "先运行 ./setup.sh 并填写 ngrok.yml"; exit 1; }

if [ "$1" = "--fg" ]; then
    exec ngrok start web --config ngrok.yml
fi

# 首次: 注册 systemd 服务 (幂等, 只装一次)
if ! systemctl list-unit-files 2>/dev/null | grep -q '^ngrok.service'; then
    echo ">> 首次运行: 安装 ngrok systemd 服务 ..."
    sudo ngrok service install --config "$(pwd)/ngrok.yml"
fi

if systemctl is-active --quiet ngrok; then
    echo ">> ngrok 已在运行"
else
    sudo ngrok service start
    echo ">> ngrok 已启动"
fi
echo ">> 常驻运行(开机自启, 关终端无碍) | 停止: ./stop.sh | 日志: journalctl -u ngrok -f"
