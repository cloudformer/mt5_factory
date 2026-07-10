# MT5 Factory

量化交易系统：MT5 数据下载 → PostgreSQL → 回测/模拟 → 实盘。

## 拓扑

```
Windows 主机 (VMware Workstation)
├── Linux VM: docker compose
│   ├── postgres   所有数据的唯一真实来源 (K线按年分区)
│   └── app        业务服务 (按需再拆)
└── Windows VM × 1..N: MT5 终端 + bridge
    角色: download(下载) / demo(模拟) / live(实盘), demo与live不能同机
    加 worker = 在 mt5_hosts 表注册, 不改代码
```

## 技术方案（2026-07-05 确认）

1. **全 Python 交易执行**——不用 MQL5/EA，策略代码只有一份（`strategy_core`）
2. **回测直接跑数据库**——Linux app 回测引擎，DB 回放 + 模拟撮合
3. **MT5 Windows 主机实时测试**——runner 与 MT5 同机运行，demo 验证后切实盘

验证三级：DB 快速回测 → demo 实时测试 → 实盘（换配置不换代码）。

### 全 Python 相比 EA (MQL5) 的弊端及对策

| # | 弊端 | 能否忽略 |
|---|------|---------|
| 1 | 信号→下单延迟几十~几百毫秒（EA 终端内亚毫秒） | ✅ 可忽略，前提只做 bar 级策略（M1+）。**硬边界：不能做 tick 级高频** |
| 2 | 无事件推送（无 OnTick 回调），只能轮询 | ✅ 可忽略，bar 级策略轮询 1 秒粒度足够 |
| 3 | runner 独立进程会崩、会失联（EA 活在终端里） | ⚠️ 需补齐：同机部署 + 看门狗自动重启 + **每笔单必带服务端 SL/TP** |
| 4 | 重启后内存状态全丢 | ✅ 可消除：runner 无状态，启动时从 MT5 读持仓 + 从 DB 读配置重建 |
| 5 | 用不了 MT5 Strategy Tester（真实 tick 回测/云优化） | ✅ 基本可忽略：回测本来就跑自己数据库，失真部分由 demo 实时测试覆盖 |
| 6 | 多策略同进程隔离弱（EA 一图表一实例） | ✅ 可补齐：每策略独立 magic number + 异常隔离 |
| 7 | 快速行情下退出可能晚 1 秒 | ✅ 可忽略：保护性退出交给服务端 SL/TP |

四条执行纪律（写入 CLAUDE.md，代码强制执行）：**bar 级 only、订单必带 SL/TP、runner 无状态、看门狗 + 心跳**。

## 目录

```
strategy_core/      策略包 (回测与实时执行共用同一份代码)
containers/
├── api/src/        业务后端 (8010): routes/ 接口层 + services/ 逻辑层
├── web/            Flask 前端 (8000): views/ + templates/, 只调 api
└── postgres/schema/ 数据库结构唯一来源 (幂等SQL, api 启动自动执行)
env/                统一环境配置 (Linux/Windows 共用, 不入 git)
windows_mt5/        Windows worker: bridge + runner + setup/update/restart.ps1
scripts/            setup_linux.sh (Ubuntu一键配置) + smoke.sh (冒烟测试)

开发指南见 DEVELOPMENT.md (加策略/加API/加页面/改表 都有对应做法)
```

## 常用命令

> 规则：凡 `docker compose` 命令必带 `--env-file env/.dev.env`；用 `make` 则免参数。

| make | 完整命令 | 说明 |
|------|---------|------|
| `make up` | `docker compose --env-file env/.dev.env up -d --wait` | 启动 + 等健康 + **自动冒烟测试全部API** |
| `make test` | `./scripts/smoke.sh` | 手动冒烟测试(12个只读端点) |
| `make down` | `docker compose --env-file env/.dev.env down` | 停止（数据保留） |
| `make ps` | `docker compose --env-file env/.dev.env ps` | 容器状态 |
| `make logs` | `docker compose --env-file env/.dev.env logs -f` | 跟踪全部日志 |
| — | `docker compose --env-file env/.dev.env logs -f app` | 只看某个服务日志 |
| `make build` | `docker compose --env-file env/.dev.env build` | 重新构建镜像 |
| — | `docker compose --env-file env/.dev.env up -d --build` | 构建并启动 |
| `make psql` | `docker exec -it mt5_postgres psql -U mt5user -d mt5factory` | 进数据库 |
| `make health` | `curl -s http://localhost:8010/health` | app 健康检查 |
| `make clean` | `docker compose --env-file env/.dev.env down -v` | ⚠️ 停止并删数据 |

其他：

```bash
# 数据库备份 / 恢复 (不用停库)
docker exec mt5_postgres pg_dump -U mt5user mt5factory | gzip > backup.sql.gz
gunzip -c backup.sql.gz | docker exec -i mt5_postgres psql -U mt5user -d mt5factory

# 重启单个服务 (改了 app 代码后)
docker restart mt5_api

# 入口
open http://localhost:8000        # web 页面
open http://localhost:8010/docs   # API 交互文档 (Swagger)
```

## 第一阶段全流程

```bash
# ---- Linux: 一键配置 (新VM一次) ----
./scripts/setup_linux.sh   # docker + env 交互式配置 (每项建议值, 你确认后才写入)
make up                    # 启动 + 自动冒烟测试

# ---- Windows VM: 部署 worker (见 windows_mt5/README.md) ----
#   复制 Linux 配置好的 env/.dev.env 过来 → 双击 setup.bat (自动提权, 全程自动)

# ---- 注册 worker ----
make psql
#   INSERT INTO mt5_hosts (name, host, port, roles, account_type)
#   VALUES ('win-1', '192.168.x.x', 8020, '{download,demo}', 'DEMO');

# ---- 1. 下载数据 ----
curl -X POST localhost:8010/syncdata
curl localhost:8010/syncdata/status      # 进度
curl localhost:8010/syncdata/coverage    # 每品种入库范围

# ---- 2. 生成策略 ----
curl -X POST localhost:8010/strategies/generate \
  -H 'Content-Type: application/json' \
  -d '{"template":"ma_cross","symbols":["EURUSD","XAUUSD"],"timeframe":"M15"}'

# ---- 3. 回测 ----
curl -X POST localhost:8010/backtest/run -H 'Content-Type: application/json' -d '{}'
curl localhost:8010/backtest/status
curl 'localhost:8010/backtest/top?symbol=EURUSD'     # 排名

# ---- 4. 提升到 DEMO, Windows runner 自动开始跑 ----
curl -X POST localhost:8010/strategies/123/status \
  -H 'Content-Type: application/json' -d '{"status":"DEMO"}'
```

## App API

| 端点 | 说明 |
|------|------|
| `GET /health` | 健康: db(url/状态) + 全部 worker 在线状态 |
| `GET /hosts` | worker 列表(完整字段) |
| `POST /hosts/{id}/connect` | 远程给 worker 下发 MT5 账户 |
| `POST /syncdata` `GET /syncdata/status` `GET /syncdata/coverage` | 数据下载(断点续传) / 进度 / 覆盖 |
| `GET /strategies/templates` | 可用策略模板及参数网格 |
| `POST /strategies/generate` | 模板×参数网格×品种 批量生成 |
| `GET /strategies/status?status=&symbol=` | 策略列表筛选 (runner 拉任务) |
| `POST /strategies/{id}/status` | 状态流转 CANDIDATE→DEMO→ACTIVE/ARCHIVED |
| `POST /backtest/run` `GET /backtest/status` | 批量回测(悲观撮合, M1拆bar查SL/TP) |
| `GET /backtest/top` `GET /backtest/results/{id}` | 结果排名 / 单策略历史 |
