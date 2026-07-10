#!/usr/bin/env bash
# 安装 ngrok agent 并准备配置 — 每台机器跑一次
# 之后: 编辑 ngrok/ngrok.yml (密钥/域名/密码) → ./start.sh 发布
set -e
cd "$(dirname "$0")"

if ! command -v ngrok >/dev/null 2>&1; then
    case "$(uname -s)" in
        Linux)
            echo ">> installing ngrok agent via official apt repo ..."
            curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
                | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
            echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
                | sudo tee /etc/apt/sources.list.d/ngrok.list >/dev/null
            sudo apt-get update -qq
            sudo apt-get install -y ngrok
            ;;
        Darwin) echo ">> mac 上请用: brew install ngrok"; exit 1 ;;
        *) echo "unsupported platform"; exit 1 ;;
    esac
fi
ngrok version

if [ ! -f ngrok.yml ]; then
    cp ngrok.yml.example ngrok.yml
    echo ""
    echo ">> 已生成 ngrok/ngrok.yml — 填好 authtoken / 域名 / 用户密码后运行 ./start.sh"
else
    echo ">> ngrok/ngrok.yml 已存在, 直接 ./start.sh"
fi
