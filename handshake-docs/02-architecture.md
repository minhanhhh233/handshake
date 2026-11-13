# 02. Architecture

## 1. Overview

One Python service, the gateway, holds every business rule. Agents and humans are clients of it. MariaDB is the single store. The gateway is the only component that talks to Valio Aimo's upstream APIs.

```mermaid
flowchart TB
  subgraph Clients
    A[Claude buyer agent<br/>tool use over HTTPS]
    B[Any other agent<br/>through the MCP server]
    H[Human approver<br/>approval page on a phone]
    O[Valio Aimo operations<br/>receipt and console pages]
  end

  subgraph Modal
    G[Gateway API<br/>FastAPI]
    P[Policy engine]
    S[Signer]
    F[Offer builder]
    U[Upstream adapter<br/>60 s cache]
    M[MCP server]
  end

  DB[(MariaDB)]
  V[Valio Aimo challenge APIs<br/>/api/price, /api/availability]

  A --> G
  B --> M --> G
  H --> G
  O --> G
  G --> F --> U --> V
  G --> P
  G --> S
  G --> DB
  F --> DB
  P --> DB
```

## 2. Components

| Component | Responsibility | Module |
|---|---|---|
| Gateway API | HTTP routing, authentication, validation, error mapping | `handshake/api/` |
| Catalog | Product search and detail from static data in MariaDB | `handshake/catalog.py` |
| Upstream adapter | Calls `/api/price` and `/api/availability`; caches; stores snapshots | `handshake/upstream.py` |
| Offer builder | Quantity conversion, candidate filtering, split and ranking, holds | `handshake/offers.py` |
| Policy engine | Pure function: (order draft, mandate, product facts) to outcome and reasons | `handshake/policy.py` |
| Signer | Ed25519 JWS for offers and receipts; serves the key set | `handshake/signing.py` |
| Orders | Order creation, idempotency, state transitions, approvals | `handshake/orders.py` |
| Events | Append-only, hash-chained event log; receipt assembly | `handshake/events.py` |
| Human pages | Server-rendered approval, receipt and console pages | `handshake/pages/` |
| Data loader | One-off import of the three challenge files | `scripts/load_data.py` |
| MCP server | Thin wrapper exposing the API operations as MCP tools | `mcp_server/` |
| Buyer agent | Demo client using Claude tool use | `agent/` |

The policy engine MUST have no I/O. The orders module gathers its inputs and persists its output. This is what makes NFR-04 testable.

## 3. Technology stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Web framework | FastAPI with Pydantic v2 | Generates the OpenAPI document (FR-71) |
| Hosting | Modal, one ASGI web endpoint | Verify decorator and secret syntax against current Modal docs (A-14) |
| Database | MariaDB 10.6 or later | System-versioned tables are required (Q-01) |
| DB access | SQLAlchemy 2 Core with PyMySQL | Synchronous; one connection pool per container |
| HTTP client | httpx with `truststore` | For upstream calls, with timeouts. TLS is verified against the operating system's certificate store, which also works where TLS is intercepted by a locally trusted CA. |
| Signing | PyJWT with the `cryptography` backend, algorithm EdDSA (Ed25519) | No hand-written cryptography |
| Templates | Jinja2 | Human pages |
| Agent | Anthropic Python SDK, tool use | Model set by `ANTHROPIC_MODEL` |
| MCP | Official MCP Python SDK | |
| Tests | pytest, with upstream calls replaced by recorded fixtures | |

## 4. Repository layout

```
handshake/
  api/            routers: wellknown, products, offers, orders, mandates, admin
  pages/          approval, receipt, console (templates and routes)
  catalog.py
  upstream.py
  offers.py
  policy.py
  signing.py
  orders.py
  events.py
  models.py       Pydantic schemas shared by API and policy
  errors.py       error codes and problem+json bodies (document 03, section 1.2)
  db.py           engine, session helper
  config.py       settings from environment
  ids.py          prefixed identifier generation
  timeutil.py     the single clock; UTC storage, Europe/Helsinki display, upstream date parsing
  mandates.py     mandate storage: current mandate, replace with next version
  api/deps.py     authentication dependencies (agent bearer key, admin key)
modal_app.py      Modal entry point
pyproject.toml    dependencies (extras: agent, mcp, deploy, dev)
.env.example      local configuration template; the real .env is git-ignored
mcp_server/
agent/
scripts/
  load_data.py
  seed_code_lookup.py  inferred code names for code_lookup (T-007, A-06)
  record_fixtures.py   records live price and availability answers into tests/fixtures/upstream/ (T-005)
  seed_demo.py    customers, agents, mandates for the demo; agent keys go to the git-ignored keys/demo_agents.json
  reset_demo.py
  gen_keys.py     creates the Ed25519 key pair
sql/
  schema.sql      DDL from document 04
tests/
  conftest.py     wipes and rebuilds the TEST_DATABASE_URL schema from sql/schema.sql; copies the catalog once from the dev database
  factories.py    customers, agents and mandates for tests
  fakes.py        FakeUpstream and make_facts(); tests never call the live upstream
  fixtures/       recorded upstream responses (not committed if they contain challenge data; see README)
data/             challenge files, git-ignored
```

## 5. Main flows

### 5.1 Lookup

1. Agent calls `GET /v1/products` with filters.
2. Gateway queries MariaDB only.
3. If the caller is authenticated, a `search.performed` event is written when a trace ID is supplied.
4. Response contains structured attributes. No price or availability.

### 5.2 Offer

```mermaid
sequenceDiagram
  participant Ag as Agent
  participant G as Gateway
  participant U as Upstream adapter
  participant V as Valio Aimo APIs
  participant DB as MariaDB
  Ag->>G: POST /v1/offers (lines, delivery_date)
  G->>DB: load product facts, substitution rules, active holds
  G->>U: price + availability for candidates
  U->>V: POST /api/price, POST /api/availability (if not cached)
  V-->>U: responses
  U->>DB: store snapshots
  U-->>G: normalised facts
  G->>G: filter, convert quantities, fill, rank
  G->>DB: insert offer, lines, holds, event offer.issued
  G->>G: sign offer (JWS)
  G-->>Ag: options[] with signed offers
```

### 5.3 Order with approval

```mermaid
sequenceDiagram
  participant Ag as Agent
  participant G as Gateway
  participant P as Policy engine
  participant DB as MariaDB
  participant H as Approver
  Ag->>G: POST /v1/orders (offer_id, Idempotency-Key)
  G->>DB: lock offer, load mandate
  G->>P: evaluate
  P-->>G: needs_approval + reasons
  G->>DB: insert order (pending_approval), approval, events
  G-->>Ag: 202 outcome=needs_approval
  G--)H: approval link (out of band)
  H->>G: POST /approve/{token} decision=approve
  G->>DB: order confirmed, holds committed, events
  Ag->>G: GET /v1/orders/{id}
  G-->>Ag: status=confirmed
```

Delivery of the approval link is out of band. In the demo the link is shown as a QR code on the console page (A-09).

### 5.4 Order state machine

```mermaid
stateDiagram-v2
  [*] --> confirmed: outcome accepted
  [*] --> pending_approval: outcome needs_approval
  [*] --> rejected: outcome rejected
  pending_approval --> confirmed: approver approves
  pending_approval --> declined: approver declines
  pending_approval --> expired: approval window passes
  confirmed --> [*]
  declined --> [*]
  expired --> [*]
  rejected --> [*]
```

### 5.5 Offer state machine

| From | To | Trigger |
|---|---|---|
| `active` | `consumed` | An order is created from it (any outcome except a validation error) |
| `active` | `expired` | `expires_at` passes; holds released |
| `consumed` | (final) | |
| `expired` | (final) | |

Expiry is evaluated lazily on read and by `reset_demo.py`; there is no background scheduler (A-12).

## 6. Authentication and trust boundaries

| Caller | Credential | May do |
|---|---|---|
| Anonymous | None | `GET /.well-known/*`, `GET /v1/products`, `GET /v1/products/{product_id}` (public fields) |
| Agent | `Authorization: Bearer <agent_key>` | Everything under `/v1` except `/v1/admin` |
| Approver | Single-use approval token in the page URL | Approve or decline one order |
| Operations and admin | `X-Admin-Key` header, or `key` query parameter on pages | `/v1/admin/*`, console, Valio Aimo receipt view |

- Agent keys are stored as SHA-256 hashes.
- The gateway trusts nothing an agent sends about prices, stock, mandates or product attributes. All of these are loaded server-side.
- The upstream API key exists only in the gateway's secret store.

## 7. Configuration

All settings are environment variables, provided on Modal as one secret named `handshake`.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DATABASE_URL` | yes | | `mysql+pymysql://user:pass@host:3306/handshake` |
| `VALIO_API_BASE` | yes | `https://junction26.valioaimo.fi` | Upstream base URL |
| `VALIO_API_KEY` | yes | | Upstream `X-API-Key` |
| `VALIO_CUSTOMER` | no | `123` | Fallback customer number sent upstream when `customers.upstream_customer` is empty; ignored by the challenge APIs (A-02) |
| `SIGNING_KEY_PEM` | yes | | Ed25519 private key, PEM |
| `SIGNING_KID` | yes | | Key ID published in the key set, for example `hs-2026-11` |
| `PUBLIC_BASE_URL` | yes | | External URL of the gateway, used in discovery and links |
| `ADMIN_KEY` | yes | | Shared secret for admin and operations |
| `OFFER_TTL_SECONDS` | no | `900` | Offer lifetime (A-11) |
| `APPROVAL_TTL_SECONDS` | no | `3600` | How long a pending order waits for a decision (A-11) |
| `UPSTREAM_CACHE_TTL_SECONDS` | no | `60` | Cache lifetime for upstream answers |
| `UPSTREAM_TIMEOUT_SECONDS` | no | `5` | Per-call timeout |
| `DEMO_MODE` | no | `false` | Enables `/v1/admin/reset` and the approval QR on the console |

The agent process additionally needs `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `HANDSHAKE_BASE_URL` and `HANDSHAKE_AGENT_KEY`.

## 8. Deployment

1. Create the MariaDB database and run `sql/schema.sql`.
2. Run `scripts/gen_keys.py`; store the private key in the Modal secret.
3. Run `scripts/load_data.py` against the database with the three challenge files in `data/`.
4. Run `scripts/seed_demo.py`.
5. `modal deploy modal_app.py`; set `PUBLIC_BASE_URL` to the URL Modal prints and redeploy.
6. Check `GET {PUBLIC_BASE_URL}/.well-known/handshake.json` and `GET /v1/health`.

Where MariaDB runs is unresolved (Q-01). The gateway only needs a reachable `DATABASE_URL`.

## 9. Error handling principles

- Upstream failure or timeout on an offer request returns `503 upstream_unavailable`. No offer is issued from stale data older than the cache lifetime.
- A product with `error: true` in the upstream price response is dropped from the candidates and reported in `excluded[]`.
- Database writes for one request happen in one transaction. An offer and its holds are created together or not at all.
- Order creation locks the offer row (`SELECT ... FOR UPDATE`) so that two concurrent requests cannot both consume it.

## 10. Observability

- Every response carries `X-Trace-Id`.
- The console page lists the latest events across traces for the demo.
- Application logs are structured JSON to standard output; they MUST NOT contain agent keys, the admin key or the upstream key.
