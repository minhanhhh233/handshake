# Handshake: documentation package

Handshake is an agent gateway for the Valio Aimo Junction 2026 challenge. It lets an external purchasing agent look up products, receive a signed fixed-price offer, and place an order within limits set by a human, on top of Valio Aimo's challenge APIs and data files.

This package is the single source of truth for building it. Where it differs from the earlier project plan, this package wins.

## Documents

| # | File | Read it when you need |
|---|---|---|
| 1 | [01-product-requirements.md](01-product-requirements.md) | Scope, users, numbered requirements, priorities |
| 2 | [02-architecture.md](02-architecture.md) | Components, tech stack, request flows, deployment, configuration |
| 3 | [03-api-specification.md](03-api-specification.md) | Every endpoint, request and response shape, error codes |
| 4 | [04-data-model.md](04-data-model.md) | MariaDB schema (DDL), table purposes, retention |
| 5 | [05-policy-and-trust.md](05-policy-and-trust.md) | Mandate schema, policy rules, signing, stock holds, event chain |
| 6 | [06-upstream-integration.md](06-upstream-integration.md) | Valio Aimo APIs and data files, loading, search, quantity conversion, offer building |
| 7 | [07-agent-integration.md](07-agent-integration.md) | Claude buyer agent, tool definitions, MCP server |
| 8 | [08-implementation-plan.md](08-implementation-plan.md) | Phases, tasks, owners, definition of done, cut order |
| 9 | [09-testing-and-demo.md](09-testing-and-demo.md) | Test cases, fixtures, demo script, fallbacks |
| 10 | [10-assumptions-and-open-questions.md](10-assumptions-and-open-questions.md) | Every assumption (A-nn) and unresolved question (Q-nn) |

Other documents cite assumptions and questions by ID, for example (A-03) or (Q-01). Resolve them in document 10 only, then update the affected sections it lists.

## Reading order

- **Everyone:** this file, then document 1 and document 10.
- **Backend:** 2, 3, 4, 5, 6.
- **Agent and integration:** 3, 6, 7.
- **Front end and pitch:** 1, 3 (sections 9 and 10), 9.

## Conventions used in every document

| Convention | Value |
|---|---|
| Requirement keywords | MUST, SHOULD, MAY as in RFC 2119 |
| Priority labels | P0 = must-have (core requirement), P1 = should-have, P2 = stretch |
| API base path | `/v1` (well-known and human pages sit outside it) |
| Field naming | `snake_case` in Handshake JSON; upstream Valio Aimo fields keep their `camelCase` |
| Identifiers | Prefixed strings: `agt_`, `cus_`, `mnd_`, `off_`, `ord_`, `apr_`, `evt_`, `trc_` followed by 20 lowercase base32 characters |
| Money | Decimal strings with two places, for example `"1234.50"`; currency is always stated |
| Timestamps | ISO 8601 with offset, for example `2026-11-14T09:30:00+02:00`; server time zone is Europe/Helsinki |
| Dates | `YYYY-MM-DD` |
| Quantities | An object `{ "value": <number>, "unit": "<unit>" }` |

## Glossary

| Term | Meaning |
|---|---|
| Agent | External software that buys on behalf of a customer. In the demo, a Claude-based agent. |
| Customer | A Valio Aimo buyer organisation, for example a hotel chain. Owns one or more agents. |
| Approver | A named human at the customer who signs off orders outside the agent's authority. |
| Gateway | The Handshake service. The only component that holds business rules. |
| Upstream | Valio Aimo's challenge APIs (`/api/price`, `/api/availability`) and data files. |
| Mandate | Machine-readable rules stating what an agent may decide alone. |
| Offer | A signed, time-limited quote for specific products, quantities and a delivery date. The only path to an order. |
| Hold | A temporary reservation of stock created when an offer is issued. |
| Outcome | The policy result for an order: `accepted`, `needs_approval` or `rejected`. |
| Receipt | A signed, human-readable record of everything that happened for one order. |
| Trace | The chain of events that belongs to one purchasing attempt, identified by `trace_id`. |
| Stock product | A product Valio Aimo stocks itself; available quantity is known. |
| Terminal product | A product delivered from a supplier's warehouse; available quantity is not known upstream. |
| Assurance | How far a claim can be relied on: `confirmed` or `unconfirmed`. |
| Sales unit | The unit a product is sold in upstream (for example a case). |
| Base quantity | A quantity expressed in litres or kilograms. |

## Data handling rules

- The challenge data set and the upstream API key MUST NOT be committed to any repository or shared outside the event (terms of the data set). Keep the repository private until Q-13 is answered.
- The upstream API key MUST stay server-side. Agents never receive it.
- Supplier free text (marketing texts, instructions) is untrusted input and MUST be labelled as such in every API response that includes it.

## Status of this package

- Version 1.0, written Friday 13 November 2026.
- Based on: the challenge brief, the upstream OpenAPI document (version 0.1.0), the product data schema, and an analysis of the three data files.
- Not yet verified: live upstream responses. No call to `/api/price` or `/api/availability` has been made. See Q-02 to Q-05.
