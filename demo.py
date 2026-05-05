"""
demo.py — provisions virtual keys with budgets and simulates burst traffic
to show how LiteLLM enforces rate limits and budget guardrails.

what this demonstrates:
1. creating a virtual key with a daily budget cap
2. creating a virtual key with an hourly token limit
3. sending a burst of requests to trigger 429s
4. the fallback chain kicking in when the primary hits its limit
"""

import asyncio
import httpx
import os
import time
from dataclasses import dataclass
from typing import Optional

BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-dev-master-key")
ADMIN_HEADERS = {
    "Authorization": f"Bearer {MASTER_KEY}",
    "Content-Type": "application/json",
}


@dataclass
class RequestResult:
    key_alias: str
    status: int
    served_model: Optional[str]
    tokens_used: Optional[int]
    cost: Optional[float]
    error: Optional[str]
    latency_ms: float


async def provision_key(
    client: httpx.AsyncClient,
    alias: str,
    max_budget: float,       # in USD
    budget_duration: str,    # "24h", "7d", "30d"
    rpm_limit: int,
    tpm_limit: int,
    models: Optional[list[str]] = None,
) -> str:
    """create a virtual key with budget and rate limits."""
    allowed_models = models or ["ministral", "llama"]
    resp = await client.post(
        f"{BASE_URL}/key/generate",
        headers=ADMIN_HEADERS,
        json={
            "key_alias": alias,
            "models": allowed_models,
            "max_budget": max_budget,
            "budget_duration": budget_duration,
            "rpm_limit": rpm_limit,
            "tpm_limit": tpm_limit,
            "metadata": {"team": "demo", "purpose": "rate-limit-poc"},
        },
    )
    if resp.status_code != 200:
        print(f"Error: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    key = resp.json()["key"]
    print(f"  provisioned key '{alias}': budget=${max_budget}/{budget_duration}, rpm={rpm_limit}, tpm={tpm_limit}")
    return key


async def send_request(
    client: httpx.AsyncClient,
    key: str,
    key_alias: str,
    prompt: str,
    request_model: str = "ministral",
    max_tokens: int = 100,
    timeout_s: float = 15,
) -> RequestResult:
    start = time.monotonic()
    try:
        resp = await client.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": request_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
            timeout=timeout_s,
        )
        latency = (time.monotonic() - start) * 1000

        if resp.status_code == 200:
            data = resp.json()
            usage = data.get("usage", {})
            return RequestResult(
                key_alias=key_alias,
                status=200,
                served_model=data.get("model"),
                tokens_used=usage.get("total_tokens"),
                cost=data.get("_hidden_params", {}).get("response_cost"),
                error=None,
                latency_ms=latency,
            )
        else:
            error_body = resp.json()
            return RequestResult(
                key_alias=key_alias,
                status=resp.status_code,
                served_model=None,
                tokens_used=None,
                cost=None,
                error=error_body.get("error", {}).get("message", str(resp.status_code)),
                latency_ms=latency,
            )
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return RequestResult(
            key_alias=key_alias,
            status=0,
            served_model=None,
            tokens_used=None,
            cost=None,
            error=str(e),
            latency_ms=latency,
        )


def print_result(r: RequestResult, idx: int):
    if r.status == 200:
        model = r.served_model or "?"
        tokens = f"{r.tokens_used} tok" if r.tokens_used else "?"
        cost = f"${r.cost:.6f}" if r.cost else "?"
        print(f"  [{idx:02d}] ✓  {r.key_alias:<20} {model:<10} {tokens:<10} {cost:<12} {r.latency_ms:.0f}ms")
    else:
        icon = "⛔" if r.status == 429 else "✗"
        print(f"  [{idx:02d}] {icon}  {r.key_alias:<20} HTTP {r.status}  {r.error[:60] if r.error else ''}")


async def burst_test(
    client: httpx.AsyncClient,
    key: str,
    alias: str,
    n: int,
    prompt: str,
    request_model: str = "ministral",
    max_tokens: int = 100,
    timeout_s: float = 15,
):
    """send n concurrent requests and print results as they arrive."""
    tasks = [
        send_request(
            client,
            key,
            alias,
            prompt,
            request_model=request_model,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )
        for _ in range(n)
    ]
    results = await asyncio.gather(*tasks)
    ok = sum(1 for r in results if r.status == 200)
    blocked = sum(1 for r in results if r.status == 429)
    errors = sum(1 for r in results if r.status not in (200, 429))
    for i, r in enumerate(results):
        print_result(r, i + 1)
    print(f"\n  summary: {ok} ok / {blocked} rate-limited / {errors} errors out of {n} requests")
    return results


async def check_spend(client: httpx.AsyncClient, key: str, alias: str):
    resp = await client.get(
        f"{BASE_URL}/key/info",
        headers={**ADMIN_HEADERS, "Authorization": f"Bearer {key}"},
    )
    if resp.status_code == 200:
        data = resp.json()
        info = data.get("info", {})
        spent = info.get("spend", 0)
        budget = info.get("max_budget", "?")
        print(f"\n  spend for '{alias}': ${spent:.6f} / ${budget} (budget remaining: ${float(budget or 0) - spent:.6f})")


async def main():
    suffix = str(int(time.time()))
    print("\n═══════════════════════════════════════════════════")
    print("  LiteLLM rate limiting + budget guardrails demo")
    print("═══════════════════════════════════════════════════\n")

    async with httpx.AsyncClient(timeout=60) as client:

        # -- scenario 1: burst that exceeds rpm limit --
        print("── scenario 1: burst exceeds rpm limit ──────────────\n")
        print("  provisioning key with rpm=3 (to make it easy to trigger)...")
        burst_key = await provision_key(
            client, alias=f"burst-test-{suffix}",
            max_budget=1.0, budget_duration="24h",
            rpm_limit=3, tpm_limit=10000,
        )
        print(f"\n  sending 8 concurrent requests (rpm limit: 3)...\n")
        await burst_test(client, burst_key, f"burst-test-{suffix}", n=8, prompt="Reply with exactly one word: hello")
        await check_spend(client, burst_key, f"burst-test-{suffix}")

        print()

        # -- scenario 2: budget exhaustion --
        print("── scenario 2: budget cap enforced ─────────────────\n")
        print("  provisioning key with max_budget=$0.001 (tiny budget to exhaust)...")
        budget_key = await provision_key(
            client, alias=f"budget-test-{suffix}",
            max_budget=0.0001, budget_duration="24h",  # ~$0.0001 — exhausts in 1-2 calls
            rpm_limit=60, tpm_limit=100000,
        )
        print(f"\n  sending 5 requests (budget cap: $0.0001)...\n")
        await burst_test(client, budget_key, f"budget-test-{suffix}", n=5, prompt="Summarize the French Revolution in 3 sentences.")
        await check_spend(client, budget_key, f"budget-test-{suffix}")

        print()

        # -- scenario 3: fallback chain demonstration --
        print("── scenario 3: fallback chain (rpm exhausted -> llama) ───\n")
        print("  provisioning key for fallback demo...")
        fallback_key = await provision_key(
            client,
            alias=f"fallback-test-{suffix}",
            max_budget=1.0,
            budget_duration="24h",
            rpm_limit=120,
            tpm_limit=200000,
            models=["ministral-limited", "llama"],
        )
        print("\n  sending 8 concurrent requests to model='ministral-limited' (rpm cap: 2)...")
        print("  expected behavior: first 2 served by ministral, rest routed to llama fallback\n")
        fallback_results = await burst_test(
            client,
            fallback_key,
            f"fallback-test-{suffix}",
            n=8,
            prompt="Reply with exactly one word: hello",
            request_model="ministral-limited",
            max_tokens=20,
            timeout_s=45,
        )

        ok_total = sum(1 for r in fallback_results if r.status == 200)
        fallback_ok = sum(1 for r in fallback_results if r.status == 200 and r.served_model and "llama" in r.served_model)
        ministral_ok = sum(1 for r in fallback_results if r.status == 200 and r.served_model and "ministral" in r.served_model)
        print("\n  fallback summary:")
        print(f"    served by ministral-limited:  {ministral_ok}")
        print(f"    served by llama (fallback):   {fallback_ok}")


if __name__ == "__main__":
    asyncio.run(main())
