ENV_FILE ?= env/.dev.env
COMPOSE = docker compose --env-file $(ENV_FILE)

.PHONY: up down build logs ps psql health test clean

up:  # 启动并等 healthcheck 通过, 然后自动冒烟测试全部 API
	$(COMPOSE) up -d --wait
	@./scripts/smoke.sh

test:  # 手动冒烟测试
	@./scripts/smoke.sh

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
