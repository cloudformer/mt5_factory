#!/usr/bin/env bash
# MT5 Factory - Ubuntu 快速配置 (每台新VM跑一次, 幂等可重跑)
# 用法: ./scripts/setup_linux.sh   完成后: make up
#
# 原则: env/.dev.env 只在你确认后写入 — 每一项都是"给出建议值 → 你回车采用或输入自定义"。
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

echo "=== [3/4] 环境配置 env/.dev.env (交互式, 确认后才写入) ==="
if [ ! -f env/.dev.env ]; then
    cp env/.dev.env.example env/.dev.env
    echo "已从模板生成 env/.dev.env"
fi

# ask_set <KEY> <建议值> <说明>
# 当前值缺失/仍是默认时: 显示建议值, 回车采用或输入自定义, 之后才写入文件
ask_set() {
    local key=$1 suggestion=$2 desc=$3 input value
    read -r -p "  $key ($desc) [回车使用: $suggestion]: " input
    value=${input:-$suggestion}
    sed -i "s|^$key=.*|$key=$value|" env/.dev.env
    echo "  已写入 $key=$value"
}

if grep -qE '^DOCKER_COMPOSE_HOST=(127\.|$)' env/.dev.env; then
    ask_set "DOCKER_COMPOSE_HOST" "$(hostname -I | awk '{print $1}')" "本机IP, Windows worker 用它访问 api"
fi
if grep -qE '^BRIDGE_API_KEY=(change_me|$)' env/.dev.env; then
    ask_set "BRIDGE_API_KEY" "$(openssl rand -hex 16)" "api与bridge的共享密钥"
fi
if grep -q '^POSTGRES_PASSWORD=mt5pass' env/.dev.env; then
    ask_set "POSTGRES_PASSWORD" "$(openssl rand -hex 12)" "数据库密码"
fi
echo "其余配置项如需修改: nano env/.dev.env"

echo "=== [4/4] 完成 ==="
if ! id -nG "$USER" | grep -qw docker; then
    echo "!! docker 组权限需重新登录生效: 注销重登 (或执行 newgrp docker)"
fi
IP=$(grep '^DOCKER_COMPOSE_HOST=' env/.dev.env | cut -d= -f2)
echo ""
echo "下一步:"
echo "  make up                        # 启动 + 健康等待 + 自动冒烟测试"
echo "  浏览器打开 http://$IP:8000"
echo ""
echo "别忘了: 把配置好的 env/.dev.env 复制到 Windows repo 的 env/ 下 (两边共用同一份)"
