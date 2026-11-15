# Handshake

**Let AI agents buy from Valio Aimo. On Valio Aimo's terms.**

Handshake is an agent gateway prototype for the Valio Aimo Junction 2026 challenge. An external
purchasing agent searches the catalogue, receives a signed fixed-price offer and places an order
within limits that a person set. Anything over those limits waits for that person to approve it
on their phone; the agent never sees the approval link.

- **Demo video:** https://youtu.be/c0lLd0bgRUc
- **Live gateway:** https://minhanhhh233--handshake-web.eu-west.modal.run
  (landing page `/`, live demo `/live`, API docs `/docs`)

Prototype only: not an official Valio Aimo service. Product data is challenge data and not fully
accurate. All customers and people shown are fictional.

## What it does

| | |
|---|---|
| Agents see only what they should | Product data is public. Prices and stock go to registered agents, only for the products they shortlist. |
| A price an agent can prove | Every offer is an Ed25519-signed JWS with price, quantity, delivery date and expiry, held for 2 minutes (`OFFER_TTL_SECONDS`). Agents verify it against `/.well-known/jwks.json`. |
| People set the limits | A versioned mandate per agent: per-order and weekly spend caps, allowed and forbidden categories, allergens, delivery days, substitutes, a trust ladder for new agents. Checked by the gateway on every order. |
| Human approval | Over the limits, the order waits for a named approver (link or QR code, 30 to 60 minutes). Every step is in a hash-chained event log and a signed receipt. |
| Tested against a rogue agent | A Claude agent told to get its order through by any means (raw API access under its own key). A scorecard counts what the gateway refused, held for a person, and let through over a limit (expected 0). |
| Open standards | HTTPS + OpenAPI, a discovery document (`/.well-known/handshake.json`), JWKS, `llms.txt` and an MCP server. |

## Try it in the browser

- **`/`**: what Handshake is, the public discovery endpoints and four copyable quick-start commands.
- **`/live`**: pick who you are (a visitor without a key; a hotel chain, restaurant, school
  kitchen or hospital kitchen; a new agent on probation; or Valio Aimo operations), then either type a request for the live Claude buyer agent ("Honest agent" or
  "Rogue agent") or open "Try a scenario" for one-click scripted runs (ordering, limits and
  approval, exceptions, comparing offers, misbehaving agents, proactive offers, trust). The agent's
  latest message is on the left, Handshake's latest answer on the right, with a playback bar and
  the full step log. Each visitor gets a private sandbox for 2 hours; no sign-in.
- Approving or declining happens on the approval page, opened from the link or QR code in the
  approval bar.
- Live runs are limited (3 per 10 minutes per visitor, 25 tool calls or 2 minutes per run, a daily
  model budget). Scripted scenarios use no AI.

## Repository layout

| Path | Contents |
|---|---|
| `handshake/` | The gateway (FastAPI): `api/` routers, `pages/` server-rendered pages and their two scripts, policy engine, offers, orders, signing, events, sandboxes, live demo and scripted scenarios |
| `agent/` | `handshake_client.py` (verifies signatures, idempotency keys, trace IDs) and `claude_agent.py` (the Claude buyer agent, also a CLI) |
| `mcp_server/` | MCP server exposing the agent tools over stdio |
| `sql/schema.sql` | MariaDB schema (document 04) |
| `scripts/` | `gen_keys.py`, `load_data.py`, `seed_demo.py`, `reset_demo.py`, `migrate_sandboxes.py`, `modal_cli.py` |
| `tests/` | pytest suite: unit, database and Playwright browser tests |
| `modal_app.py` | Modal deployment (one ASGI web endpoint) |
| `handshake-docs/` | The specification |

`data/` (the challenge files), `keys/` and `.env` are git-ignored and never committed.

## Run it locally

Needs Python 3.12 and MariaDB 10.6 or later (system-versioned tables).

```
python -m venv .venv
.venv/bin/pip install -e ".[agent,mcp,dev]"         # Windows: .venv\Scripts\pip
cp .env.example .env                                  # then fill in the values
python scripts/gen_keys.py                            # Ed25519 signing key for SIGNING_KEY_PEM
mysql handshake < sql/schema.sql
python scripts/load_data.py --products data/products.json \
    --reliability data/reliability.csv --carbon data/carbon.csv
python scripts/seed_demo.py                           # demo customers, agents, mandates
uvicorn handshake.api:create_app --factory --port 8000
```

Open http://127.0.0.1:8000/. The main variables (document 02, section 7):

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | MariaDB connection (SQLAlchemy URL) |
| `VALIO_API_BASE`, `VALIO_API_KEY`, `VALIO_CUSTOMER` | Valio Aimo challenge API for live prices and availability |
| `SIGNING_KEY_PEM`, `SIGNING_KID` | Offer and receipt signing key |
| `PUBLIC_BASE_URL` | Base for approval links, receipts and discovery |
| `ADMIN_KEY` | Admin API and console |
| `OFFER_TTL_SECONDS`, `APPROVAL_TTL_SECONDS` | Offer lifetime (120) and approval window (3600) |
| `ANTHROPIC_API_KEY`, `LIVE_DEMO_DAILY_BUDGET_USD` | Live Claude agent on `/live` (optional) |

TLS uses the system certificate store when needed (`truststore`); verification is never disabled.

## Tests

```
pytest                                   # everything except tests marked live
pytest -m "not db and not browser"       # no database, no browser
```

Database tests need `TEST_DATABASE_URL`, a throwaway database the suite wipes and rebuilds from
`sql/schema.sql`. Browser tests use Playwright (`pip install playwright && playwright install
chromium`) and are skipped without it; they check the pages at 390 and 1280 px. Last full run:
367 passed, 1 skipped.

## Deploy (Modal)

```
python scripts/modal_cli.py deploy modal_app.py
```

Two Modal secrets: `handshake` (the variables above, with the hosted `DATABASE_URL`) and
`handshake-anthropic` (`ANTHROPIC_API_KEY`, `LIVE_DEMO_DAILY_BUDGET_USD`). The app keeps one warm
container until Monday 16 November 2026, 12:00 Helsinki.

## Claude buyer agent (CLI)

```
python agent/claude_agent.py "1,000 litres of lactose-free milk for Monday at the best price" \
    --agent nordlys
```

Uses `ANTHROPIC_API_KEY`; `--agent` picks a demo agent key from `keys/demo_agents.json`
(or pass `--key`). Prompt, tools and safeguards: document 07.

## Connecting an assistant over MCP

`mcp_server/server.py` exposes the ten Handshake agent tools to any MCP-capable assistant (Claude
Desktop, Claude Code, ...) over stdio. It is a thin client of the gateway's HTTP API through
`agent/handshake_client.py`: offer signatures are verified before the assistant sees them,
orders carry an idempotency key, one trace ID covers the session, and supplier or approver free
text stays inside blocks marked UNTRUSTED. The assistant can never approve its own order.

```
pip install -e ".[agent,mcp]"
HANDSHAKE_BASE_URL=https://your-gateway.example HANDSHAKE_AGENT_KEY=hsk_... python -m mcp_server.server
```

| Variable | Meaning |
|---|---|
| `HANDSHAKE_BASE_URL` | Gateway base URL (default `http://127.0.0.1:8000`) |
| `HANDSHAKE_AGENT_KEY` | The agent's bearer key (required); issued per agent by the gateway operator |
| `SSL_CERT_FILE` | Optional CA bundle, e.g. behind a TLS-intercepting proxy; verification is never disabled |

Example client configuration (`mcpServers` entry, e.g. `claude_desktop_config.json` or a
project's `.mcp.json`):

```json
{
  "mcpServers": {
    "handshake": {
      "command": "/path/to/handshake/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/handshake",
      "env": {
        "HANDSHAKE_BASE_URL": "https://your-gateway.example",
        "HANDSHAKE_AGENT_KEY": "<agent key>"
      }
    }
  }
}
```

On Windows use `C:\\path\\to\\handshake\\.venv\\Scripts\\python.exe` as the command.

| Tool | What it does |
|---|---|
| `get_mandate` | Read the agent's mandate: spend caps, approvers, allowed categories, allergen and delivery rules. Call first. |
| `search_products` | Search the catalogue with structured filters (category, claims, allergens, labels); at most 20 results, no prices. |
| `get_product` | One product's full detail; supplier text is returned in an UNTRUSTED block. |
| `request_offer` | Get a signed, fixed-price offer for products in order of preference (optionally up to 3 ranked options); the signature is verified before it is returned. |
| `dry_run_order` | Check an offer against the mandate without ordering: accepted, needs_approval or rejected, with reasons. |
| `place_order` | Order a verified offer (idempotent); confirmed, pending_approval or rejected. |
| `get_order` | Current order status; returns immediately, poll no more often than every 5 seconds. |
| `get_receipt` | The signed receipt and event timeline of an order. |
| `list_proposals` | Proactive offers from Valio Aimo's selling agent (only with the customer's consent); each is a normal signed offer, accepted with `dry_run_order` and `place_order`. |
| `decline_proposal` | Decline a proactive offer; its stock hold is released. |
