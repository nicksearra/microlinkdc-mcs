#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# MCS — Quick Start Script
# ═══════════════════════════════════════════════════════════════════
# Usage: ./start.sh [mode]
#   ./start.sh          → Infrastructure + API only
#   ./start.sh full     → Everything including simulator
#   ./start.sh down     → Tear down all services
#   ./start.sh reset    → Tear down + delete data volumes
# ═══════════════════════════════════════════════════════════════════

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

MODE=${1:-default}

case $MODE in
  down)
    echo -e "${YELLOW}Stopping MCS...${NC}"
    docker compose --profile dev --profile frontend down
    echo -e "${GREEN}✓ MCS stopped${NC}"
    exit 0
    ;;
  reset)
    echo -e "${YELLOW}Resetting MCS (all data will be lost)...${NC}"
    docker compose --profile dev --profile frontend down -v
    echo -e "${GREEN}✓ MCS reset complete${NC}"
    exit 0
    ;;
esac

# Check for .env
if [ ! -f .env ]; then
  echo -e "${YELLOW}No .env found — copying from .env.example${NC}"
  cp .env.example .env
  echo -e "${GREEN}✓ Created .env (edit passwords before production!)${NC}"
fi

echo -e "${CYAN}"
echo "  ███╗   ███╗ ██████╗ ███████╗"
echo "  ████╗ ████║██╔════╝ ██╔════╝"
echo "  ██╔████╔██║██║      ███████╗"
echo "  ██║╚██╔╝██║██║      ╚════██║"
echo "  ██║ ╚═╝ ██║╚██████╗ ███████║"
echo "  ╚═╝     ╚═╝ ╚═════╝ ╚══════╝"
echo -e "${NC}"
echo "  MicroLink Control System"
echo ""

# Step 1: Infrastructure
echo -e "${CYAN}[1/4] Starting infrastructure (TimescaleDB, Redis, Mosquitto)...${NC}"
docker compose up -d timescaledb redis mosquitto

# Wait for DB
echo -e "${CYAN}[2/4] Waiting for TimescaleDB...${NC}"
until docker compose exec -T timescaledb pg_isready -U mcs_admin > /dev/null 2>&1; do
  sleep 1
  echo -n "."
done
echo ""
echo -e "${GREEN}✓ TimescaleDB ready${NC}"

# Note: schema.sql and aggregates.sql auto-run via docker-entrypoint-initdb.d
# on first boot. If the DB already has data, they're skipped automatically.

# Step 3: Platform services
echo -e "${CYAN}[3/4] Starting platform services (API, Ingestor, Alarm Engine)...${NC}"
docker compose up -d api ingestor alarm-engine

# Step 4: Optional services
if [ "$MODE" = "full" ]; then
  echo -e "${CYAN}[4/4] Running seed data + starting simulator + dashboard...${NC}"
  docker compose --profile dev --profile frontend up -d
  echo -e "${YELLOW}Waiting for seed to populate test data...${NC}"
  docker compose logs -f seed 2>/dev/null &
  SEED_PID=$!
  # Wait for seed container to finish (max 60s)
  for i in $(seq 1 60); do
    STATUS=$(docker inspect -f '{{.State.Status}}' mcs-seed 2>/dev/null || echo "missing")
    if [ "$STATUS" = "exited" ]; then break; fi
    sleep 1
  done
  kill $SEED_PID 2>/dev/null
  echo ""
else
  echo -e "${CYAN}[4/4] Skipping simulator & dashboard (use './start.sh full' to include)${NC}"
  echo -e "${YELLOW}  Note: Run 'make seed' to populate test data${NC}"
fi

# Wait for API
echo -e "${CYAN}Waiting for API...${NC}"
for i in $(seq 1 30); do
  if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    break
  fi
  sleep 1
  echo -n "."
done
echo ""

# Final status
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  MCS is running!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  API:        ${CYAN}http://localhost:8000${NC}"
echo -e "  Swagger UI: ${CYAN}http://localhost:8000/docs${NC}"
echo -e "  ReDoc:      ${CYAN}http://localhost:8000/redoc${NC}"
echo -e "  Health:     ${CYAN}http://localhost:8000/health${NC}"
echo ""
if [ "$MODE" = "full" ]; then
  echo -e "  Dashboard:  ${CYAN}http://localhost:3000${NC}"
  echo -e "  Simulator:  ${GREEN}running (MQTT traffic flowing)${NC}"
fi
echo ""
echo -e "  Logs:       docker compose logs -f api"
echo -e "  Stop:       ./start.sh down"
echo -e "  Reset:      ./start.sh reset"
echo ""
