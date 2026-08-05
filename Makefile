ENV_FILE ?= env/.dev.env
WORKERS ?= 9   # 回测 worker 副本数(唯一源, 进git=CI生效): 0=只有api内1路(原行为); 要并行改这里提交, 临时 make scale WORKERS=N
               # 2026-08-05 实测(20年全货币交叉, 数据全在页缓存/SSD零IO): vCPU 1×8+9worker=1:32(最优)
               # · 1×12=1:34 · 1×6=1:52(核少反而慢 — 本负载是多进程 pg+9worker, 吃逻辑线程数)
               # 瓶颈 = pg 喂数据(~280% CPU, 同品种被多 worker 重复拉), 加 worker 只是排队
COMPOSE = docker compose --env-file $(ENV_FILE)

.PHONY: up down build logs ps psql health test clean scale

up:  # 启动(必要时重建镜像) → 等 healthcheck → 冒烟测试; schema 由 api 启动自动对齐
	$(COMPOSE) up -d --wait --build --scale worker=$(WORKERS)
	@./scripts/smoke.sh
	@echo "K线分区焐进 OS 页缓存(~2分钟, 失败不影响启动; 单独跑: make prewarm)"
	@$(COMPOSE) run --rm prewarm || true

scale:  # 只调 worker 副本数(不重建镜像不动其他服务): make scale WORKERS=2
	$(COMPOSE) up -d --no-build --no-deps --scale worker=$(WORKERS) worker

test:  # 手动冒烟测试
	@./scripts/smoke.sh

test-engine:  # 引擎回归测试(v2.4): 撮合/聚合/指标/对账/集成 — 改引擎前后必跑
	@pip install -q -r containers/api/requirements-dev.txt && \
	  python -m pytest containers/api/tests -q

down:
	$(COMPOSE) down

build:
	$(COMPOSE) build

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

psql:
	docker exec -it mt5_postgres psql -U mt5user -d mt5factory

prewarm:  # K线分区焐进 OS 页缓存(~2分钟): VM 重启后跑一次; 幂等, 避开跑批时段
	$(COMPOSE) run --rm prewarm

health:
	curl -s http://localhost:8010/health

clean:  # 停止并删除数据卷(会清空数据库!)
	$(COMPOSE) down -v
