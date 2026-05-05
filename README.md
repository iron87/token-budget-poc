# token-budget-poc

a PoC that demonstrates rate limiting and budget guardrails for LLM calls
using LiteLLM proxy. shows how to enforce hard rpm/tpm limits and per-key
budget caps before requests leave your infrastructure.

companion to: [rate limiting for LLM calls: why your token budget will explode in prod](#)

## architecture

```
your app
  └─► virtual key (rpm, tpm, budget)
        └─► LiteLLM proxy :4000
              ├─ [enforce_model_rate_limits] — hard 429 before upstream
              ├─ [budget check] — block if key budget exhausted
              ├─► Local Ollama (primary: ministral-3:8b)
              └─► Local Ollama (fallback: llama3.2:latest)
                    └─ [circuit breaker]  (circuit_breaker.py)
                          └─ fail fast when downstream is saturated
```

## what this demonstrates

| scenario | what triggers | what happens |
|---|---|---|
| burst (8 req, rpm=3) | rpm limit | 3 pass, 5 get 429 immediately |
| budget exhaustion | per-key spend cap | 1 passes, rest get 429 (ExceededBudget) |
| fallback chain | primary cooldown | router picks fallback without caller seeing it |
| circuit breaker | N consecutive failures | app fails fast, stops retrying into saturated gateway |

## prerequisites

- Docker and Docker Compose
- Ollama installed and running
- Python 3.8+ with venv support

## quickstart

```bash
git clone https://github.com/iron87/token-budget-poc
cd token-budget-poc

# ensure Ollama is running with required models
ollama serve &
ollama pull ministral-3:8b
ollama pull llama3.2:latest

# run the demo
./demo.sh
```

## manual curl

```bash
docker compose up -d

# create a key with a tight budget and rate limit
curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer sk-dev-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "test-key",
    "max_budget": 1.0,
    "budget_duration": "24h",
    "rpm_limit": 10,
    "tpm_limit": 10000
  }'
  -H "Authorization: Bearer sk-dev-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "test",
    "models": ["claude-haiku"],
    "max_budget": 0.01,
    "budget_duration": "24h",
    "rpm_limit": 3,
    "tpm_limit": 5000
  }'

# single request
curl -X POST http://localhost:4000/chat/completions \
  -H "Authorization: Bearer <your-virtual-key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-haiku", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 20}'

# check spend
curl http://localhost:4000/key/info \
  -H "Authorization: Bearer <your-virtual-key>"

# admin dashboard
open http://localhost:4000/ui
```

also: `./test.sh` runs the full curl sequence automatically.

## project structure

```
litellm_config.yaml     # model list, rpm/tpm limits, fallback chain, router settings
demo.py                 # provisions keys + runs burst and budget scenarios
circuit_breaker.py      # application-level circuit breaker for LLM calls
demo.sh                 # start stack + run demo
test.sh                 # manual curl tests
docker-compose.yml      # LiteLLM proxy + Redis (shared rate limit state) + Postgres (spend tracking)
venv/                   # Python virtual environment (created by demo.sh)
```

## key config details

`enforce_model_rate_limits` in `router_settings.optional_pre_call_checks` is required
to make rpm/tpm hard limits. without it, they're routing hints only.

redis is required for shared rate-limit state across multiple proxy instances.
without redis, each instance tracks limits independently and limits are not enforced
correctly under horizontal scale.

## references

- [BerriAI/litellm](https://github.com/BerriAI/litellm)
- [LiteLLM — budgets & rate limits](https://docs.litellm.ai/docs/proxy/users)
- [LiteLLM — load balancing](https://docs.litellm.ai/docs/proxy/load_balancing)
- [LiteLLM — fallbacks](https://docs.litellm.ai/docs/proxy/reliability)
