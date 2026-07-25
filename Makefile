ENV_FILE ?= env/.dev.env
COMPOSE = docker compose --env-file $(ENV_FILE)

.PHONY: up down build logs ps psql health test clean

up:  # 启动(必要时重建镜像) → 等 healthcheck → 冒烟测试; schema 由 api 启动自动对齐
	$(COMPOSE) up -d --wait --build
	@./scripts/smoke.sh

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
