# Windows MT5 Worker

Windows VM 上的两个进程（同机、互不依赖）：
- **bridge**（端口 8020）：MT5↔HTTP 转换，供 Linux api 下载数据、心跳、远程下发账户
- **runner**：从 api 拉取 DEMO/ACTIVE 策略，收盘 bar 决策，本地直接下单（必带 SL/TP）

## 部署（干净 Windows → 就绪，一条命令）

```powershell
# 1. 整个 repo 复制/clone 到 VM; 把 Linux 上配置好的 env/.dev.env 复制到 repo 的 env/ 下
# 2. 双击 windows_mt5\setup.bat 即可 (自动提权+绕过执行策略+窗口保持打开)
#    Python、依赖、MT5 终端默认全装，本机已有的会自动检测并跳过
#    不想装 MT5 终端时, 在 cmd 里: setup.bat -SkipMT5
```

setup.ps1 结束时自检并输出结果：绿色 = 就绪（自动出现在 web Workers 页）；
黄色 = bridge 已运行但 MT5 未登录（按提示补账户）；红色 = 哪一步失败（修复后重跑，幂等安全）。
env 没填 `DOCKER_COMPOSE_HOST` 时会明确提示并停下。

`start_bridge.bat` / `start_runner.bat` 平时不用手动碰（开机自启 + 看门狗），只在排错时手动跑看日志。

**开机自检**（`selftest.bat`，自启第三项）：等 bridge/MT5 就绪后自动测全链路——
端口 → MT5 账户 → 算法交易开关 → runner → 报价新鲜度 → 下单开平一轮（仅 demo 账户，
live 自动跳过）→ 对账数据。结果显示在状态页 `http://<本机>:8020/` 的"开机自检"行，
**无需登录 Windows**；失败时窗口保持打开。手动重跑：双击 `selftest.bat`。

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

**账户的干净配置流程（推荐走 web，Windows 上零手工）：**
1. setup.bat 装完后 bridge 自动注册，Workers 页出现本机，状态为 **MT5未就绪**（不是离线——bridge 活着，只是没账户；真离线才显示"离线"）
2. Workers 页底部"下发 MT5 账户"→ 选中本机 → 填账号/密码/服务器 → 登录
3. 30 秒内状态变 **在线**。密码不落库（api 只记 login/server），MT5 终端自己记住密码，重启后自动恢复登录，无需再次下发

验证：

```bash
curl http://<windows_ip>:8020/health     # bridge + MT5 状态
```

在数据库注册 worker（Linux 侧）：

```sql
INSERT INTO mt5_hosts (name, host, port, download, runner, account_type)
VALUES ('win-worker-1', '192.168.x.x', 8020, TRUE, 'demo', 'DEMO');
```

## 注意事项：算法交易开关 / 市场报价

### 算法交易开关（Algo Trading）

- **这是 runner 能否下单的总开关**。关着时策略照常盯盘算信号，但每笔 `order_send` 都被拒（retcode 10027 "AutoTrading disabled by client"），表现为策略永远"等待信号"却从不成交——最坑的沉默故障
- **已固化进配置**：bridge 拉起终端时带 `/config:bridge\terminal_start.ini`（`[Experts] AllowLiveTrading=1`）自动开启，克隆/重装的新机不用手工点。⚠️ 手动双击打开的终端不读这个文件，得自己点工具栏 **Algo Trading** 按钮（变绿）
- 确认方法（任一）：web Workers 页状态详情"交易许可: 是"；本机 `http://localhost:8020/` 状态页；跑 `test\order_check.bat` 冒烟测试（在 demo 账户开一笔最小单立即平掉，验证整条下单链路，只花一个点差；拒绝在真实账户运行）

### 市场报价（Market Watch）

- 策略品种必须在券商的报价列表里。bridge/runner 会自动 `symbol_select` 添加进报价窗，**一般无需手工操作**
- Demo/Live 页出现 **"无报价 — 未加载"** = 这家券商没有这个品种名。不同券商命名不同（XAUUSD / GOLD / XAUUSD.m 都存在），在终端报价窗（Ctrl+M）搜真实名称；策略品种名与券商不符时，策略会被 runner 跳过并在页面提示，修正后一分钟内自动重试
- **"报价停滞(休市/断流?)"** 徽章：周末/假日休市属正常；交易时段出现 = 数据断流，看 bridge 窗口日志
- 纪律提醒（CLAUDE.md）：回测数据必须从**实际交易的同一家券商服务器**下载，否则信号层对不齐

## Bridge API（端口 8020 固定）

| 端点 | 鉴权 | 说明 |
|------|------|------|
| `GET /` | 无 | **本机状态页** (浏览器打开): bridge/MT5/账户/runner 一屏, 10秒自刷新 |
| `GET /health` | 无 | 心跳: MT5 连接/交易许可/账户概要 |
| `POST /connect` | X-API-Key | 远程下发 MT5 账户并登录 |
| `GET /account` | X-API-Key | 账户完整信息 |
| `GET /symbols` `GET /symbol/{s}` | X-API-Key | 品种列表/详情 |
| `GET /rates?symbol=&timeframe=M1&from_ts=&to_ts=` | X-API-Key | 按时间范围取K线 (epoch秒) |
| `GET /trades?days=30` | X-API-Key | **交易流水** (只读): 持仓+成交明细原样透传, web /mt5 页数据源; `fmt=html` 本机免鉴权直接看 |
| `GET /recon?days=90` | 无 | **交易对账页** (只读): 成交按 magic 分组, 与 web 战绩逐行对应; `fmt=json` 出数据 |
| `POST /ordertest?symbol=XAUUSD` | 无 | **下单冒烟测试**: 最小单开平各一次; 硬保护仅限 DEMO 账户 (状态页有按钮) |

## Runner 行为（CLAUDE.md 四纪律的落地）

- 每 60s 从 api 刷新策略清单（`RUN_STATUS` 决定跑 DEMO 还是 ACTIVE）
- 每 10s 轮询：只取**已收盘** bar（`copy_rates_from_pos` 位置 1 起），同一收盘 bar 只处理一次
- 下单前查 MT5 真实持仓（magic 归属），**无状态**，重启零恢复成本
- 无 SL/TP 的信号直接拒绝；单策略异常隔离，不拖累其他策略
- 崩溃由 start_runner.bat 看门狗 10s 拉起，启动文件夹快捷方式登录自启（等价双击；计划任务环境实测 MT5 IPC 附着不上，勿改回）
