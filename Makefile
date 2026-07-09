ENV_FILE ?= env/.dev.env
COMPOSE = docker compose --env-file $(ENV_FILE)

.PHONY: up down build logs ps psql health test db-migration clean

up:  # 启动 → 等 healthcheck → 自动迁移schema → 冒烟测试
	$(COMPOSE) up -d --wait
	@$(MAKE) db-migration
	@./scripts/smoke.sh

db-migration:  # 应用幂等迁移(对齐已有库的schema); make up 会自动调用, 也可单独手动跑
	@echo "applying containers/postgres/migrations/migrate.sql ..."
	@docker exec -i mt5_postgres sh -c 'exec psql -q -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' \
		< containers/postgres/migrations/migrate.sql
	@echo "db-migration done"

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
