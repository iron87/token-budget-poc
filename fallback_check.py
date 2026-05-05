import asyncio
import time
import httpx
from demo import provision_key, burst_test


async def main() -> None:
    suffix = str(int(time.time()))
    async with httpx.AsyncClient(timeout=30) as client:
        key = await provision_key(
            client,
            alias=f"fallback-only-{suffix}",
            max_budget=1.0,
            budget_duration="24h",
            rpm_limit=120,
            tpm_limit=200000,
            models=["fallback-primary-broken", "llama"],
        )
        print("\n  sending 6 concurrent requests to model='fallback-primary-broken'...\n")
        results = await burst_test(
            client,
            key,
            f"fallback-only-{suffix}",
            n=6,
            prompt="Reply with exactly one word: hello",
            request_model="fallback-primary-broken",
            max_tokens=20,
        )
        ok_total = sum(1 for r in results if r.status == 200)
        fallback_ok = sum(1 for r in results if r.status == 200 and r.served_model == "llama")
        print("\n  fallback summary:")
        print(f"    total successes:              {ok_total}")
        print(f"    fallback responses (llama):   {fallback_ok}")


if __name__ == "__main__":
    asyncio.run(main())
