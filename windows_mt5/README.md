# Windows MT5 Worker

Windows VM 上的两个进程（同机、互不依赖）：
- **bridge**（端口 8020）：MT5↔HTTP 转换，供 Linux api 下载数据、心跳、远程下发账户
- **runner**：从 api 拉取 DEMO/ACTIVE 策略，收盘 bar 决策，本地直接下单（必带 SL/TP）

## 部署（干净 Windows → 就绪）

```powershell
# 1. 整个 repo 复制/clone 到 VM (runner 需要 strategy_core)
# 2. 管理员 PowerShell:
cd mt5_factory\windows_mt5
.\setup.ps1 -InstallMT5     # 已装 MT5 则省略 -InstallMT5
# 3. 把 Linux 上配置好的 env/.dev.env 复制到 Windows repo 的 env/ 下 (两边共用同一份)
notepad ..\env\.dev.env   # 确认 DOCKER_COMPOSE_HOST / BRIDGE_API_KEY 已填
# 4. 启动 (或注销重登, 计划任务自动拉起)
.\start_bridge.bat
.\start_runner.bat
```

MT5 账户三种配置方式（任选）：
1. 什么都不填——用本机 MT5 终端里已登录的账户
2. env/.dev.env 里填 `MT5_LOGIN/PASSWORD/SERVER`
3. 从 Linux 侧远程下发：`curl -X POST <app>/hosts/1/connect -d '{"login":..,"password":"..","server":".."}'`

验证：

```bash
curl http://<windows_ip>:8020/health     # bridge + MT5 状态
```

在数据库注册 worker（Linux 侧）：

```sql
INSERT INTO mt5_hosts (name, host, port, roles, account_type)
VALUES ('win-worker-1', '192.168.x.x', 8020, '{download,demo}', 'DEMO');
```

## Bridge API（端口 8020 固定）

| 端点 | 鉴权 | 说明 |
|------|------|------|
| `GET /health` | 无 | 心跳: MT5 连接/交易许可/账户概要 |
| `POST /connect` | X-API-Key | 远程下发 MT5 账户并登录 |
| `GET /account` | X-API-Key | 账户完整信息 |
| `GET /symbols` `GET /symbol/{s}` | X-API-Key | 品种列表/详情 |
| `GET /rates?symbol=&timeframe=M1&from_ts=&to_ts=` | X-API-Key | 按时间范围取K线 (epoch秒) |

## Runner 行为（CLAUDE.md 四纪律的落地）

- 每 60s 从 api 刷新策略清单（`RUN_STATUS` 决定跑 DEMO 还是 ACTIVE）
- 每 10s 轮询：只取**已收盘** bar（`copy_rates_from_pos` 位置 1 起），同一收盘 bar 只处理一次
- 下单前查 MT5 真实持仓（magic 归属），**无状态**，重启零恢复成本
- 无 SL/TP 的信号直接拒绝；单策略异常隔离，不拖累其他策略
- 崩溃由 start_runner.bat 看门狗 10s 拉起，计划任务开机自启
