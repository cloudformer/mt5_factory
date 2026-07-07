# Windows MT5 Worker

Windows VM 上的两个进程（同机、互不依赖）：
- **bridge**（端口 8020）：MT5↔HTTP 转换，供 Linux api 下载数据、心跳、远程下发账户
- **runner**：从 api 拉取 DEMO/ACTIVE 策略，收盘 bar 决策，本地直接下单（必带 SL/TP）

## 部署（干净 Windows → 就绪，一条命令）

```powershell
# 1. 整个 repo 复制/clone 到 VM; 把 Linux 上配置好的 env/.dev.env 复制到 repo 的 env/ 下
# 2. 管理员 PowerShell — 就这一条 (装环境+防火墙+自启任务+启动+自检):
cd mt5_factory\windows_mt5
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -InstallMT5   # 已装MT5则省略参数
```

setup.ps1 结束时自检并输出结果：绿色 = 就绪（自动出现在 web Workers 页）；
黄色 = bridge 已运行但 MT5 未登录（按提示补账户）；红色 = 哪一步失败（修复后重跑，幂等安全）。
env 没填 `DOCKER_COMPOSE_HOST` 时会明确提示并停下。

`start_bridge.bat` / `start_runner.bat` 平时不用手动碰（开机自启 + 看门狗），只在排错时手动跑看日志。

## 日常运维

```powershell
powershell -ExecutionPolicy Bypass -File .\update.ps1    # 更新: git pull + 依赖 → 重启 + 自检
powershell -ExecutionPolicy Bypass -File .\restart.ps1   # 只重启 (改了 env / MT5卡死时)
```

**MT5 终端无需手动开启**：bridge 启动时 `mt5.initialize()` 自动拉起终端并登录；
断线后 30s 自动重连（重新拉起）。env 里配好账户即可开机全自动、无人值守。

MT5 账户三种配置方式（任选）：
1. 什么都不填——用本机 MT5 终端里已登录的账户（终端记住密码，重启自动恢复）
2. env/.dev.env 里填 `MT5_LOGIN/PASSWORD/SERVER`（推荐，配合克隆全自动）
3. web 的 Workers 页"下发 MT5 账户"远程登录

验证：

```bash
curl http://<windows_ip>:8020/health     # bridge + MT5 状态
```

在数据库注册 worker（Linux 侧）：

```sql
INSERT INTO mt5_hosts (name, host, port, download, runner, account_type)
VALUES ('win-worker-1', '192.168.x.x', 8020, TRUE, 'demo', 'DEMO');
```

## Bridge API（端口 8020 固定）

| 端点 | 鉴权 | 说明 |
|------|------|------|
| `GET /` | 无 | **本机状态页** (浏览器打开): bridge/MT5/账户/runner 一屏, 10秒自刷新 |
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
