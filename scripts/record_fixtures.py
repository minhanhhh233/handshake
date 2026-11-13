"""Record live upstream responses as test fixtures (task T-005; document 09, section 1).

    python scripts/record_fixtures.py --date 2026-11-16            # the demo shortlist
    python scripts/record_fixtures.py --date 2026-11-16 --products 464398 464201

Writes raw JSON to tests/fixtures/upstream/ (git-ignored: it contains challenge data).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from handshake.config import get_settings, tls_context  # noqa: E402

SHORTLIST = ["464398", "464201", "465039", "464306", "469363", "462775", "464179", "463038"]
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "tests", "fixtures", "upstream")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--products", nargs="*", default=SHORTLIST)
    args = ap.parse_args()

    s = get_settings()
    if not s.valio_api_key:
        print("VALIO_API_KEY is not set", file=sys.stderr)
        return 1
    os.makedirs(OUT, exist_ok=True)
    headers = {"X-API-Key": s.valio_api_key}
    body = {"customer": s.valio_customer, "requestedDate": args.date, "products": args.products}
    with httpx.Client(base_url=s.valio_api_base, headers=headers, timeout=30,
                      verify=tls_context()) as c:
        ping = c.get("/api/ping")
        print("ping", ping.status_code, ping.text[:100])
        for ep in ("price", "availability"):
            t0 = time.perf_counter()
            r = c.post(f"/api/{ep}", json=body)
            ms = (time.perf_counter() - t0) * 1000
            print(f"{ep}: HTTP {r.status_code} in {ms:.0f} ms")
            path = os.path.join(OUT, f"{ep}_{args.date}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"request": body, "status": r.status_code,
                           "headers": {k: v for k, v in r.headers.items()
                                       if k.lower().startswith(("x-rate", "ratelimit", "retry"))},
                           "response": r.json() if r.headers.get("content-type", "").startswith(
                               "application/json") else r.text},
                          f, indent=2, ensure_ascii=False)
            print("  saved", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
