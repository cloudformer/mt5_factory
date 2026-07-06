#!/usr/bin/env bash
# MT5 Factory - Ubuntu 快速配置 (每台新VM跑一次, 幂等可重跑)
# 用法: ./scripts/setup_linux.sh   完成后: make up
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/4] 基础工具 ==="
sudo apt-get update -qq
sudo apt-get install -y -qq make git curl openssl ca-certificates >/dev/null
echo "make / git / curl 就绪"

echo "=== [2/4] Docker ==="
if command -v docker >/dev/null 2>&1; then
    echo "docker 已安装: $(docker --version)"
else
    curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER"

echo "=== [3/4] 环境配置 env/.dev.env ==="
if [ ! -f env/.dev.env ]; then
    cp env/.dev.env.example env/.dev.env
    echo "已从模板生成 env/.dev.env"
fi
IP=$(hostname -I | awk '{print $1}')
if grep -qE '^DOCKER_COMPOSE_HOST=(127\.|$)' env/.dev.env; then
    sed -i "s|^DOCKER_COMPOSE_HOST=.*|DOCKER_COMPOSE_HOST=$IP|" env/.dev.env
    echo "DOCKER_COMPOSE_HOST=$IP  (本机 IP 自动探测)"
fi
if grep -q '^BRIDGE_API_KEY=change_me' env/.dev.env; then
    sed -i "s|^BRIDGE_API_KEY=.*|BRIDGE_API_KEY=$(openssl rand -hex 16)|" env/.dev.env
    echo "BRIDGE_API_KEY  已生成随机密钥"
fi
if grep -q '^POSTGRES_PASSWORD=mt5pass' env/.dev.env; then
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 12)|" env/.dev.env
    echo "POSTGRES_PASSWORD  已生成随机密码"
fi

echo "=== [4/4] 完成 ==="
if ! id -nG "$USER" | grep -qw docker; then
    echo "!! docker 组权限需重新登录生效: 注销重登 (或执行 newgrp docker)"
fi
echo ""
echo "下一步:"
echo "  make up                        # 启动 + 健康等待 + 自动冒烟测试"
echo "  浏览器打开 http://$IP:8000"
echo ""
echo "别忘了: 把配置好的 env/.dev.env 复制到 Windows repo 的 env/ 下 (两边共用同一份)"
