# 开发手册

> 自己动手改代码前看这一篇。架构原则见 CLAUDE.md，运维命令见 README.md。

## 一张图看懂代码结构

```
mt5_factory/
├── strategy_core/                ★ 策略包 (回测与实盘共用同一份代码)
│   ├── base.py                     Strategy 基类 + Signal
│   └── templates/                  策略模板 (加新策略就在这)
│
├── containers/api/src/           ★ 业务后端 (FastAPI, 8010)
│   ├── main.py                     入口: 连接池/心跳任务/health, 不写业务
│   ├── routes/                     接口层: 一个领域一个文件
│   │   ├── hosts.py                  /hosts*      worker 管理
│   │   ├── data.py                   /syncdata* /config*  下载与配置
│   │   ├── strategies.py             /strategies* 生成与流转
│   │   └── backtests.py              /backtest*   回测调度
│   └── services/                   逻辑层: 不碰 HTTP
│       ├── sync.py                   下载(多worker并行) + 心跳状态机
│       └── backtest.py               回测引擎(纯函数: M1回放+悲观撮合)
│
├── containers/web/               ★ 前端 (Flask, 8000, 只调 api 不碰数据库)
│   ├── views/                      一个页面一个 Blueprint
│   ├── templates/                  Jinja2; _macros.html 是公共组件
│   └── api_client.py               调 api 的薄封装
│
├── containers/postgres/schema/   ★ 数据库结构唯一来源 (幂等SQL, api 每次启动按序自动执行)
│
└── windows_mt5/                  ★ Windows worker
    ├── bridge/main.py              MT5↔HTTP + 自动注册 + 状态页(8020)
    ├── runner/main.py              实时执行 (角色跟随 web 指派)
    └── setup / update / restart.ps1
```

调用关系：`浏览器 → web → api → (postgres, bridge)`；`runner → api 拉任务, → 本机MT5 下单`。

## 常见开发任务（改哪里）

### 加一个策略模板
1. `strategy_core/templates/my_strategy.py`——继承 `Strategy`，实现 `warmup` 和 `on_bar()`，
   定义 `PARAM_GRID`（网格生成）和 `RANDOM_SPACE`（随机生成）
2. `strategy_core/templates/__init__.py` 的 `TEMPLATES` 字典注册一行
3. 完事——回测、web 下拉框、Windows runner 全部自动识别（重启 api；Windows 侧跑 update.ps1）

铁律：`on_bar` 只收已收盘 bar；返回的 Signal 必带 sl/tp 绝对价。

### 加一个 API 端点
1. 归属已有领域 → 直接在 `routes/对应文件` 加函数
2. 新领域 → `routes/新文件.py` 建 `router = APIRouter()`，在 `routes/__init__.py` 注册
3. 业务逻辑复杂就放 `services/`，路由文件保持薄
4. `scripts/smoke.sh` 加一行 check（只读端点）

### 加一个 web 页面
1. `views/新页面.py` 建 Blueprint（照抄任意现有文件的结构）
2. `templates/新页面.html`——`extends base.html` + `import _macros.html`
3. `app.py` 注册 Blueprint，`base.html` 导航加一项
4. 徽章/时间/空态用 `_macros.html` 现成宏，不要重写样式

### 改表结构
1. `containers/postgres/schema/` **追加**一个新的编号幂等 SQL 文件（如 `002_add_bucket.sql`），
   旧文件永不修改。幂等 = `IF NOT EXISTS` / `CREATE OR REPLACE` / `DROP ... IF EXISTS` / `ON CONFLICT DO NOTHING`
2. 完事——api 每次启动按文件名顺序自动执行整个目录：空库建全量、老库无害对齐，
   新装机器和已运行的库走**同一条路径**，没有单独的迁移概念

### 加一个配置项（web 可改的）
1. `postgres/schema/` 新增幂等种子 SQL（`INSERT ... ON CONFLICT DO NOTHING`）
2. `routes/data.py` 的 `CONFIG_KEYS` 加 key + 校验分支
3. web 的下载页（或对应页）表单加输入框

### 加回测指标
`services/backtest.py` 的 `_metrics()` 加字段即可——metrics 是 JSONB，表结构不用动；
想在排名页显示就在 `backtests.html` 加一列。

### 纳入朋友的 MQ5 (翻译流程)
1. 拿到 .mq5 源码(不是.ex5) + .set 参数 → 评估: bar级+固定SL/TP可直翻; 移动止损类需先扩展runner; tick级/马丁不收
2. 翻译成 strategy_core 模板 (指标用 numpy 重写), 朋友的参数作为默认组合
3. 交叉验证: MT5 Strategy Tester 跑原版 vs 我们 DB 回测跑翻译版, 同品种同时段信号对齐才算过
4. 系统内验证: web 策略页提交源码跟踪状态; TRANSLATED 后用 POST /strategies/mq5/{id}/verify
   (粘贴 Tester Deals 记录) 出一致率% — ≥90% 保留, <70% 打回
5. commit; 之后可对其参数做邻域搜索(网格/随机)

已验证的 MQ5 导入同时是**回测引擎的标定样本**(外部权威参照): 引擎/数据链路每次大改后,
重跑它们的一致性验证做回归 — 一致率下降 = 引擎改坏了。

## 开发循环

```bash
make up                    # 起环境 (含自动冒烟)
# 改 api 代码 → docker restart mt5_api   (代码是挂载的, 不用重构建)
# 改 web 代码 → 直接刷新浏览器           (gunicorn --reload)
# 改依赖/Dockerfile → docker compose --env-file env/.dev.env up -d --build
make test                  # 冒烟回归
docker logs -f mt5_api     # 看日志 (结构化 JSON)
make psql                  # 直接查库
```

Windows 侧改动：`update.ps1`（拉代码+重启）；调试看日志：手动跑 `start_bridge.bat` / `start_runner.bat`。

## 调试排查路径

1. **web 页面异常** → `docker logs mt5_web`；页面报"api 不可达"→ 查 api
2. **api 异常** → `docker logs mt5_api`（错误带完整 traceback）；`localhost:8010/docs` 单独调接口
3. **worker 离线** → 浏览器开 `http://<win_ip>:8020/` 状态页，一屏看 bridge/MT5/账户/runner
4. **数据没下载** → `/syncdata/status` 的 errors 字段；worker 事件史 `GET /hosts/{id}/events`
5. **runner 不下单** → runner 窗口日志；确认策略状态与主机角色匹配（demo 主机跑 DEMO 策略）

## 代码约定

- 配置零默认值：部署配置只在 `env/.dev.env`，代码缺 env 直接报错（fail-fast）
- api 分层：routes 只做 HTTP 进出，services 只做逻辑，互不越界
- 写库幂等：批量写入用 `ON CONFLICT DO NOTHING`，任何任务可安全重跑
- 无状态：重启任何服务零损失，唯一要保护的是 `containers/postgres/data/`
- 每个文件头部 docstring 写明"职责 + 扩展点"，改之前先读它

## 已知设计权衡（记账不修——疼的那天再动，动之前读这里）

评审结论（2026-07）：以下均为有意取舍，修复成本 > 当前疼痛，**不要顺手优化**。

| 权衡 | 疼痛触发点 | 到时的修法 |
|------|-----------|-----------|
| `strategy_stats` 是近 90 天滚动窗口快照，老单滑出后数字变小 | 需要长期累计战绩时 | P2 成交逐笔回写（trades 表），快照降级为缓存 |
| `historical_bars` 无券商维度，整库绑定单一券商 | 决定做多券商验证时 | 加 broker 列 + 改 PK，回测/对账全链路带维度 |
| `backtests.trades` JSONB 整包存逐笔 | 回测数千次后表偏胖 | 归一化或按策略保留最近 N 次 |
| `mt5_host_events` 无限增长 | 若干年后 | 加保留期清理（一行 DELETE） |
| `config` 值无库级校验 | 多人操作时 | CHECK 或应用层 schema 校验 |
