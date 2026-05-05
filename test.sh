#!/usr/bin/env bash
# test.sh — quick curl tests against the LiteLLM proxy
# usage: ./scripts/test.sh

BASE_URL="${LITELLM_BASE_URL:-http://localhost:4000}"
MASTER_KEY="${LITELLM_MASTER_KEY:-sk-dev-master-key}"

separator() { printf '\n%s\n' "──────────────────────────────────────────"; }

# 1. health check
separator
echo "1. health check"
curl -sf "$BASE_URL/health" | python3 -m json.tool

# 2. create a key with a tight budget ($0.001, rpm=3)
separator
echo "2. create virtual key with budget + rate limit"
KEY_RESPONSE=$(curl -sf -X POST "$BASE_URL/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "test-key",
    "models": ["ministral"],
    "max_budget": 0.001,
    "budget_duration": "24h",
    "rpm_limit": 3,
    "tpm_limit": 5000
  }')
echo "$KEY_RESPONSE" | python3 -m json.tool
VIRTUAL_KEY=$(echo "$KEY_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['key'])")
echo "virtual key: $VIRTUAL_KEY"

# 3. single request
separator
echo "3. single request"
curl -sf -X POST "$BASE_URL/chat/completions" \
  -H "Authorization: Bearer $VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "ministral", "messages": [{"role": "user", "content": "say hello"}], "max_tokens": 20}' \
  | python3 -m json.tool

# 4. burst — should trigger 429
separator
echo "4. burst (5 concurrent requests, rpm_limit=3) — expect 429s"
for i in $(seq 1 5); do
  curl -sf -o /tmp/r$i.json -w "%{http_code}" -X POST "$BASE_URL/chat/completions" \
    -H "Authorization: Bearer $VIRTUAL_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model": "ministral", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}' &
done
wait
for i in $(seq 1 5); do
  echo -n "  request $i: "
  cat /tmp/r$i.json | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if 'choices' in d else f'blocked — {d.get(\"error\",{}).get(\"message\",\"\")[:80]}')" 2>/dev/null || echo "http error"
done

# 5. check spend
separator
echo "5. check spend for key"
curl -sf "$BASE_URL/key/info" \
  -H "Authorization: Bearer $VIRTUAL_KEY" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); i=d.get('info',{}); print(f'spent: \${i.get(\"spend\",0):.6f} / \${i.get(\"max_budget\",\"?\")} | rpm_limit: {i.get(\"rpm_limit\")} | tpm_limit: {i.get(\"tpm_limit\")}')"

separator
echo "done. spend dashboard: $BASE_URL/ui"
