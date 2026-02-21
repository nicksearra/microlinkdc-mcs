.PHONY: up down reset seed logs api-logs status db-shell redis-shell test-api dev

# ── Startup ──────────────────────────────────────────────────────
up:
	./start.sh

full:
	./start.sh full

down:
	./start.sh down

reset:
	./start.sh reset

# ── Database ─────────────────────────────────────────────────────
seed:
	pip install psycopg2-binary --break-system-packages -q 2>/dev/null; \
	DB_HOST=localhost DB_PASSWORD=$${POSTGRES_PASSWORD:-localdev} python db/seed_data.py

db-shell:
	docker compose exec timescaledb psql -U mcs_admin -d mcs

redis-shell:
	docker compose exec redis redis-cli

# ── Logs ─────────────────────────────────────────────────────────
logs:
	docker compose logs -f

api-logs:
	docker compose logs -f api

alarm-logs:
	docker compose logs -f alarm-engine

ingest-logs:
	docker compose logs -f ingestor

# ── Status ───────────────────────────────────────────────────────
status:
	@echo "=== Services ===" && docker compose ps
	@echo "" && echo "=== Health ===" && curl -s http://localhost:8000/health 2>/dev/null | python3 -m json.tool || echo "API not running"
	@echo "" && echo "=== Stats ===" && curl -s http://localhost:8000/stats 2>/dev/null | python3 -m json.tool || true

# ── Quick API Tests ──────────────────────────────────────────────
test-api:
	@echo "--- Health ---" && curl -s http://localhost:8000/health | python3 -m json.tool
	@echo "\n--- Sites ---" && curl -s http://localhost:8000/api/v1/sites | python3 -m json.tool
	@echo "\n--- Blocks ---" && curl -s http://localhost:8000/api/v1/blocks | python3 -m json.tool
	@echo "\n--- Sensors ---" && curl -s "http://localhost:8000/api/v1/sensors/block-01" | python3 -m json.tool
	@echo "\n--- Latest ---" && curl -s "http://localhost:8000/api/v1/telemetry/latest?block_slug=block-01" | python3 -m json.tool
	@echo "\n--- Alarms ---" && curl -s "http://localhost:8000/api/v1/alarms" | python3 -m json.tool

# ── Development ──────────────────────────────────────────────────
dev:
	docker compose up -d timescaledb redis mosquitto
	@echo "Infrastructure running. Start services manually:"
	@echo "  cd platform && uvicorn api.app:create_app --factory --reload --port 8000"
	@echo "  cd dashboard && npm run dev"

# ── Dashboard ────────────────────────────────────────────────────
dashboard-dev:
	cd dashboard && npm install && npm run dev

dashboard-build:
	cd dashboard && npm install && npm run build
