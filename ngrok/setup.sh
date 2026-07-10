#!/usr/bin/env bash
# 安装 ngrok agent 并准备配置 — 每台机器跑一次
# 之后: 编辑 ngrok/ngrok.yml (密钥/域名/密码) → ./start.sh 发布
set -e
cd "$(dirname "$0")"

if ! command -v ngrok >/dev/null 2>&1; then
    echo ">> installing ngrok agent to /usr/local/bin ..."
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64)  URL=https://ngrok-agent.s3.amazonaws.com/ngrok-v3-stable-linux-amd64.tgz ;;
        Linux-aarch64) URL=https://ngrok-agent.s3.amazonaws.com/ngrok-v3-stable-linux-arm64.tgz ;;
        Darwin-*)      echo ">> mac 上请用: brew install ngrok"; exit 1 ;;
        *) echo "unsupported platform"; exit 1 ;;
    esac
    curl -sSL "$URL" | sudo tar xz -C /usr/local/bin
fi
ngrok version

if [ ! -f ngrok.yml ]; then
    cp ngrok.yml.example ngrok.yml
    echo ""
    echo ">> 已生成 ngrok/ngrok.yml — 填好 authtoken / 域名 / 用户密码后运行 ./start.sh"
else
    echo ">> ngrok/ngrok.yml 已存在, 直接 ./start.sh"
fi
