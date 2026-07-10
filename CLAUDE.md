# MT5 Factory - 项目规范

## 目标架构

- **Linux VM** 跑 docker compose：`postgres`（唯一数据源）+ `api`（业务 API，8010）+ `web`（Flask 前端，8000，只调 api 不碰业务/数据库）
- **Windows VM × 1..N** 跑 MT5 终端 + bridge，是无状态 worker：
  - 从干净 Windows 到就绪必须全脚本化（`windows_mt5/`），克隆即扩容
  - 加 worker = 在 `mt5_hosts` 表注册（host/port + download/runner 职能），不改代码不重启
  - 角色：`download`（下载数据）/ `demo`（模拟）/ `live`（实盘），demo 与 live 不能同机，一台可兼多角色，随时可拆
- **数据流单向**：Windows 只和 MT5 交互，数据全部落库；回测/模拟只读库，不直接拉 MT5
- **全 Python 执行方案**（2026-07-05 确认，不用 MQL5/EA）：策略代码只有一份（`strategy_core` 共享包），回测和实盘跑同一个策略类，只换适配器：
  - 回测：Linux api 的回测引擎，数据源 = DB 回放，执行器 = 模拟撮合
  - 实时/实盘：Windows VM 上的 runner（与 MT5 同机），数据源 = MT5 实时，执行器 = order_send
  - 验证三级：DB 快速回测 → Windows demo 账户实时测试 → 实盘，换配置不换代码
  - 成交回写数据库，与回测结果同表可比
- **执行纪律**（换取 Python 方案可靠性的铁律）：
  1. 只做 bar 级策略（M1+），不做 tick 级高频（方案硬边界）
  2. 每笔订单必带服务端 SL/TP（runner 挂了持仓仍有券商侧保护）
  3. runner 无状态：启动时从 MT5 读持仓/挂单、从 DB 读配置重建，不信任内存
  4. runner 同机部署 + 看门狗自动重启 + 心跳上报；每策略独立 magic number、异常隔离
  5. 不同 worker 不得共用同一 MT5 账户（同账户双跑 = 重复下单）。由数据库唯一索引执法（schema/002，仅对启用主机生效），代码只做"写入失败=撞号"；释放账户 = 停用或删除主机

## 核心原则

1. **简洁优先**：服务能合并就合并，有明确理由才拆；目录整齐，不留占位文件
2. **一次一个模块**：完成当前模块再做下一个
3. **生产级质量**：完善的错误处理、结构化日志、输入验证、重试机制
4. **配置只在一处**：`env/*.env` → docker compose → 环境变量，不用嵌套 yaml

## 策略准入漏斗（单向淘汰，逐级变贵）

```
批量生成(CANDIDATE) → 关1: DB回测 → 关2: demo实时(DEMO) → 关3: 实盘小仓位(LIVE)
任何一关失败 → ARCHIVED（留尸体避免重复生成同类）
```

- **关1 回测撮合故意悲观**（点差取大、滑点缓冲、SL/TP 同 bar 先碰止损、跳空按实际开盘价成交、手续费 swap 全算）→ "回测亏 = 必亏，直接删"这个方向的结论可信
- **关1 反过拟合**（批量生成必有靠运气的漂亮曲线）：样本外验证（训练段筛选、留出段验证）+ 最少交易笔数 + 参数邻域稳健性（参数±10~20% 邻居也得赚）
- **关2 是对账不只是看盈亏**：demo 实际成交 vs 回测预期逐笔比偏离，达标才说明回测模型对该策略是准的
- 信号层零出入的两条纪律：**只在收盘 bar 决策**；**数据从实际交易的券商服务器下载**
- 所有阈值（样本外区间、最少笔数、偏离容忍度）做成配置，不写死

## 数据库

- PostgreSQL 16，单 public schema
- `historical_bars` 按年分区（2000-2036），主键 (symbol, timeframe, time)
- **只下载 M1 作为唯一原始数据**，M5/M15/H1/D1 等全部从 M1 按时间桶聚合派生（缺分钟属正常，按时间戳分桶而非数根数；D1/H4 边界按券商服务器时间切）
  - 红利：回测可用 M1 拆解高周期 bar 内走势，解决 SL/TP 同 bar 顺序盲区
  - 例外：需要超过 M1 历史深度的超长回测时，才补下载原生高周期（timeframe 字段为此保留）
- 品种清单是配置：交易品种（全量下载）+ 验证品种（用于跨品种反过拟合检查）
  - 默认交易品种：EURUSD, GBPUSD, USDJPY, XAUUSD
  - 默认验证品种：AUDUSD, USDCAD, NZDUSD, EURJPY, GBPJPY
- `mt5_hosts`：worker 注册表　`strategies`：策略**实例**表（模板 + 参数 + 品种 + 周期 = 一个实例，独立走漏斗，独立 magic number；模板本身品种无关，在 strategy_core 里）
- 改表结构：`containers/postgres/schema/` **追加**新的编号幂等 SQL 文件（旧文件永不改）。api 每次启动按序自动执行整个目录，空库建全量、老库无害对齐——唯一机制，没有单独的迁移概念

## 文档地图

- `README.md` 运维与全流程　- `DEVELOPMENT.md` 开发手册(加策略/API/页面/表)　- `windows_mt5/README.md` worker 部署

## 常用命令

```bash
make up / down / logs / ps / psql / health
```

## 第一阶段目标（不用太复杂，能跑通为准）

| # | 目标 | 状态 |
|---|------|------|
| 1 | Windows 自动配置 MT5 + API，数据下载到数据库 | 已实现，待 Windows VM 端到端联调 |
| 2 | app 生成策略（模板 × 参数 → strategies 表） | 已实现（ma_cross / breakout 两模板） |
| 3 | 策略在数据库回测，给出结果 | 已实现（合成数据验证通过），待真实数据验证 |
| 4 | 策略加载到 Windows 运行 | 已实现（runner），待 Windows VM 验证 |

第二阶段再考虑：监控面板、成交对账自动化、实盘准入流程强化、
jobs 表 + worker 容器（任务并行化，`--scale worker=N`）、
AI generator 服务（生成模板代码/翻译 MQ5，产出必须过门禁：语法→冒烟回测→历史回测达标→人工 git 审查；参数进 DB，代码永远进 git）。
