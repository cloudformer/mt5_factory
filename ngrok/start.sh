#!/usr/bin/env bash
# 发布本地 web(8000) 到公网固定域名 (配置全在 ngrok.yml)
# 前台运行看日志; 长期挂后台: nohup ./start.sh >/dev/null 2>&1 &
cd "$(dirname "$0")"
[ -f ngrok.yml ] || { echo "先运行 ./setup.sh 并填写 ngrok.yml"; exit 1; }
exec ngrok start web --config ngrok.yml
