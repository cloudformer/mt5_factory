ENV_FILE ?= env/.dev.env
WORKERS ?= 4   # 回测 worker 副本数(唯一源, 进git=CI生效): 0=只有api内1路(原行为); 要并行改这里提交, 临时 make scale WORKERS=N
COMPOSE = docker compose --env-file $(ENV_FILE)

.PHONY: up down build logs ps psql health test clean scale

up:  # 启动(必要时重建镜像) → 等 healthcheck → 冒烟测试; schema 由 api 启动自动对齐
	$(COMPOSE) up -d --wait --build --scale worker=$(WORKERS)
	@./scripts/smoke.sh

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

health:
	curl -s http://localhost:8010/health

clean:  # 停止并删除数据卷(会清空数据库!)
	$(COMPOSE) down -v
