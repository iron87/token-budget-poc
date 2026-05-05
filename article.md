# rate limiting for LLM calls: why your token budget will explode in prod

*TL;DR: an LLM call has no inherent cost ceiling. A single misbehaving agent, a prompt that returns a 4k-token response instead of 20, or a retry loop that doesn't back off can drain a month's budget in hours. This post covers the failure modes that actually hit production and how to enforce limits at the gateway layer with LiteLLM, without touching application code.*

**Repository**: [https://github.com/iron87/token-budget-poc](https://github.com/iron87/token-budget-poc)

---

## Why Classic API Rate Limiting Is Not Enough

LLM traffic behaves differently from classic REST traffic:

1. Cost depends on token volume, not only request count.
2. Retries can amplify load and create self-inflicted saturation.
3. Multi-tenant apps need isolation before traffic reaches the model provider.

In short: request-per-minute limits alone are useful, but not sufficient.

---
## what LiteLLM proxy actually does

LiteLLM is a unified gateway for 100+ LLM APIs. the proxy server adds virtual keys, spend tracking, rate limiting, and guardrails on top of any model, with an OpenAI-compatible interface.

The core model is: **virtual keys** sit between your application and the upstream provider. Each key carries its own budget, rpm limit, and tpm limit. the proxy enforces them before the request ever leaves your network.

```
your app → virtual key (rpm=10, budget=$1/day) → LiteLLM proxy → Provider API
```

By default, rpm and tpm values on a deployment are soft — used only to pick which deployment to route to. to make them hard limits that block requests with a 429, you need `enforce_model_rate_limits` in `router_settings.optional_pre_call_checks`.

```yaml
model_list:
  - model_name: ministral
    litellm_params:
      model: ollama/ministral-3:8b
      api_base: "http://host.docker.internal:11434"
      rpm: 10
      tpm: 50000

  - model_name: llama
    litellm_params:
      model: ollama/llama3.2:latest
      api_base: "http://host.docker.internal:11434"
      rpm: 5
      tpm: 20000

router_settings:
  optional_pre_call_checks:
    - enforce_model_rate_limits
  cooldown_time: 60
  num_retries: 2
  retry_after: 5
  fallbacks:
    - ministral:
        - llama
```

Important detail: `enforce_model_rate_limits` turns rpm/tpm from routing hints into hard blocking limits.

---

## Which Applications Use the Key?

A virtual key should represent a workload, not a whole organization. Typical mapping:

| Application | Virtual Key Alias | Why Separate It | Example Limits |
|---|---|---|---|
| Customer support chat API | support-chat-prod | Needs stable latency and strict burst control | rpm 60, tpm 200k, daily budget |
| Internal Slack bot | slack-assistant-prod | Can be noisy at business hours | rpm 20, tpm 80k |
| Nightly summarization job | nightly-summaries | Batch traffic, predictable windows | high tpm, low rpm, fixed daily budget |
| Agentic workflow orchestrator | agent-runner-prod | Highest risk of retry storms | strict rpm, circuit breaker, fallback |
| Staging environment | staging-shared | Prevent tests from impacting production quota | low budget cap, low rpm |

This gives you isolation, accountability and simpler incident response.

---

## What the Demo Shows

The demo in the repository executes this practical flow:

1. It creates a virtual key with strict limits (`rpm_limit=3`, `tpm_limit=10000`, daily budget).
2. It sends 8 concurrent calls through LiteLLM.
3. It verifies that only the allowed subset passes and the rest are blocked with 429.
4. It creates a second key with a tiny budget and runs another burst to test budget behavior.
5. It sends 8 concurrent requests to a deployment capped at 2 rpm and observes automatic failover to a fallback model.

### Outcome of Scenario 1: Rate Limit Enforcement Works

Real observed output excerpt:

```text
══ scenario 1: burst exceeds rpm limit ══

  provisioned key 'burst-test-1777996022': budget=$1.0/24h, rpm=3, tpm=10000

  [01] ✓  burst-test-1777996022 566 tok    ?            1092ms
  [02] ✓  burst-test-1777996022 566 tok    ?             651ms
  [03] ⛔ burst-test-1777996022 HTTP 429  Rate limit exceeded for api_key: ...
  [04] ⛔ burst-test-1777996022 HTTP 429  Rate limit exceeded for api_key: ...
  [05] ⛔ burst-test-1777996022 HTTP 429  Rate limit exceeded for api_key: ...
  [06] ⛔ burst-test-1777996022 HTTP 429  Rate limit exceeded for api_key: ...
  [07] ⛔ burst-test-1777996022 HTTP 429  Rate limit exceeded for api_key: ...
  [08] ✓  burst-test-1777996022 566 tok    ?             870ms

  summary: 3 ok / 5 rate-limited / 0 errors out of 8 requests
```

This is exactly the expected result for a key capped at 3 rpm under burst load.

Representative success payload returned by the API:

```json
{
  "model": "ministral",
  "choices": [
    {
      "message": {"content": "Hello", "role": "assistant"}
    }
  ],
  "usage": {"completion_tokens": 2, "prompt_tokens": 564, "total_tokens": 566}
}
```

Representative 429 payload returned by the API:

```json
{
  "error": {
    "message": "Rate limit exceeded for api_key: ... Limit type: requests. Current limit: 3, Remaining: 0 ...",
    "code": "429"
  }
}
```

### Outcome of Scenario 2: Budget Test on Local Models

The demo also provisions a second key with a very small budget (`max_budget=0.0001`) and sends additional requests.

With this local Ollama setup, key spend may remain `0.0` unless local model pricing/spend accounting is configured. Because of that, budget exhaustion is not always reproducible out of the box, while rate limiting is consistently reproducible.

Practical takeaway:

1. Rate-limit guardrails are fully validated by the demo.
2. Budget guardrails require spend accounting configuration when using local models.

---

## Fallback Chain: Automatic Rerouting in Action

When a deployment's rate limit is exhausted, the LiteLLM router transparently routes subsequent requests to the configured fallback model — without any change in the client code.

The config for this demo:

```yaml
model_list:
  - model_name: ministral-limited
    litellm_params:
      model: ollama/ministral-3:8b
      rpm: 2          # very low cap — exhausted quickly by a burst

  - model_name: llama
    litellm_params:
      model: ollama/llama3.2:latest
      rpm: 5

router_settings:
  cooldown_time: 60
  fallbacks:
    - ministral-limited:
        - llama
```

The client always sends requests to `ministral-limited`. When its rpm cap of 2 is reached, LiteLLM puts the deployment in cooldown and silently reroutes to `llama`. The caller sees a 200 OK.

**Real output from the demo:**

```
── scenario 3: fallback chain (rpm exhausted -> llama) ───

  sending 8 concurrent requests to model='ministral-limited' (rpm cap: 2)...
  expected behavior: first 2 served by ministral, rest routed to llama fallback

  [01] ✓  fallback-test  ministral-limited       566 tok   21178ms
  [02] ✓  fallback-test  ollama/llama3.2:latest   38 tok   21358ms
  [03] ✓  fallback-test  ollama/llama3.2:latest   38 tok   21578ms
  [04] ✓  fallback-test  ollama/llama3.2:latest   38 tok   21400ms
  [05] ✓  fallback-test  ollama/llama3.2:latest   38 tok   21520ms
  [06] ⛔  HTTP 429  litellm.RateLimitError: Model rate limit exceeded. RPM limit
  [07] ✓  fallback-test  ollama/llama3.2:latest   38 tok   21456ms
  [08] ✓  fallback-test  ministral-limited       566 tok   20974ms

  summary: 7 ok / 1 rate-limited / 0 errors out of 8 requests

  fallback summary:
    served by ministral-limited:  2
    served by llama (fallback):   5
```

Requests 2–5 and 7 were transparently served by `llama` once `ministral-limited` hit its rpm cap. Request 6 got a 429 because `llama` itself hit its own rpm limit (5 rpm) simultaneously — a reminder that fallback does not mean unlimited capacity.

Three important constraints to keep in mind:

1. Fallback does not remove the need to set limits on the fallback model too.
2. Fallback adds latency when cooldown retries are involved — plan for it.
3. Fallback does not replace a circuit breaker in your application layer.

Use fallback for continuity, not for unlimited capacity.

---

## Circuit Breaker in the App Layer

The gateway enforces policy. Your app should still prevent retry storms.

The full implementation is included in the repository linked at the top of this article.

Recommended behavior:

1. Open the circuit after N consecutive 429/5xx/timeouts.
2. Fail fast during cooldown.
3. Allow probe calls before closing.

---

## Practical Production Rules

1. Assign one virtual key per application/workload.
2. Keep per-key rpm/tpm tighter than global model limits.
3. Use fallback only between models with known quality and latency tradeoffs.
4. Alert separately for rate-limit events and budget events.
5. Keep naming consistent across config, code, and docs (`ministral`, `llama`, `LiteLLM`, `Ollama`).

---

## References

- BerriAI LiteLLM: [https://github.com/BerriAI/litellm](https://github.com/BerriAI/litellm)
- LiteLLM proxy users and keys: [https://docs.litellm.ai/docs/proxy/users](https://docs.litellm.ai/docs/proxy/users)
- LiteLLM load balancing and routing: [https://docs.litellm.ai/docs/proxy/load_balancing](https://docs.litellm.ai/docs/proxy/load_balancing)
- LiteLLM reliability and fallback: [https://docs.litellm.ai/docs/proxy/reliability](https://docs.litellm.ai/docs/proxy/reliability)
