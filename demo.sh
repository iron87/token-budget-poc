#!/usr/bin/env bash
# demo.sh — start the stack and run the burst + budget demo
# usage: ./scripts/demo.sh

set -e

BASE_URL="${LITELLM_BASE_URL:-http://localhost:4000}"

wait_for_litellm() {
  echo "waiting for LiteLLM proxy..."
  for i in $(seq 1 30); do
    if curl -sf -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-sk-dev-master-key}" "$BASE_URL/health" > /dev/null 2>&1; then
      echo "proxy ready"
      return 0
    fi
    sleep 3
  done
  echo "proxy did not start in time" && exit 1
}

echo ""
echo "starting stack..."
docker compose up -d litellm redis postgres
wait_for_litellm

echo ""
echo "running demo..."
source venv/bin/activate
python demo.py

echo ""
echo "check spend dashboard: http://localhost:4000/ui"
echo "(login with master key: ${LITELLM_MASTER_KEY:-sk-dev-master-key})"
