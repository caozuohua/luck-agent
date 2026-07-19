"""Probe new-api base-url forms for the stepfun model. Reads key from .env,
only prints status codes (never the secret)."""
from __future__ import annotations

import asyncio
import json
import os
import re

import httpx


def _load_env(path: str = ".env") -> dict:
    env: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                env[k] = v
    except FileNotFoundError:
        pass
    return env


async def probe(base: str, key: str, model: str) -> int:
    url = base.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}]}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(url, json=payload, headers=headers)
        return r.status_code
    except Exception as e:
        return -1 if "timeout" in str(e).lower() else -2


async def main() -> None:
    env = _load_env()
    key = env.get("LLM_API_KEY", "")
    model = "stepfun-ai/step-3.7-flash"
    if not key:
        print("NO_KEY")
        return
    for base in ["https://api.caozuohua.cloud-ip.cc",
                 "https://api.caozuohua.cloud-ip.cc/v1"]:
        code = await probe(base, key, model)
        print(f"base={base!r} -> HTTP {code}")


if __name__ == "__main__":
    asyncio.run(main())
