# 01. Product requirements

## 1. Purpose

Handshake defines and implements the interface and operating model through which an external purchasing agent can interact with Valio Aimo's systems: find products, obtain a binding fixed-price offer, and place an order within its authority or with human sign-off.

## 2. Goals and non-goals

### Goals

1. Cover the three core requirements of the challenge brief end to end.
2. Make Valio Aimo's price, stock and origin claims verifiable by an agent without a manual check.
3. Keep every limit enforced on the gateway, never on the agent.
4. Produce a record of each purchase that a human can read.
5. Work with any agent that can make HTTP calls; show this with a second client through MCP.

### Non-goals

- A marketplace, multi-supplier catalog, or supplier onboarding.
- Payment, invoicing or credit handling.
- A chatbot or replacement webshop UI.
- Negotiated quotes or counter-offers. Prices are fixed.
- A recipe engine. Recipe reasoning belongs to the buyer's agent.
- Writing orders into a Valio Aimo backend. The challenge provides no order API (A-01).

## 3. Users

| User | Role | Main interaction |
|---|---|---|
| Purchasing agent | Software acting for a customer | Calls the API: search, offer, dry-run, order, status, receipt |
| Purchasing manager (approver) | Human at the customer | Sets the mandate; approves or declines orders on a phone |
| Valio Aimo operations | Human at Valio Aimo | Reviews receipts and agent activity |
| Anonymous agent | Unregistered software | May search public product data only |

## 4. Functional requirements

Each requirement has an ID, a priority, and the brief item it answers. "Core 1/2/3" and "Optional 1/2/3" refer to the brief's lists in order.

### 4.1 Lookup (Core 1)

| ID | Priority | Requirement |
|---|---|---|
| FR-01 | P0 | The gateway MUST let an agent search products by free text, category, required claims, excluded allergens and labels, and return structured results. |
| FR-02 | P0 | Search MUST use static product data only and MUST NOT call the upstream price or availability APIs. |
| FR-03 | P0 | The gateway MUST return a single product's full structured attributes by product ID. |
| FR-04 | P0 | Structured fields MUST be separated from supplier free text, and free text MUST be marked untrusted. |
| FR-05 | P0 | Anonymous callers MUST be able to search and read public product attributes, and MUST NOT receive prices or availability. |
| FR-06 | P1 | Search results SHOULD include delivery reliability and group-average carbon footprint where the data exists, and omit them where it does not. |

### 4.2 Offer and order (Core 2)

| ID | Priority | Requirement |
|---|---|---|
| FR-10 | P0 | An authenticated agent MUST be able to request an offer for one or more lines, each with a quantity and a delivery date. |
| FR-11 | P0 | An offer MUST state a fixed price per line, totals, an expiry time and the delivery date, and MUST be signed by the gateway. |
| FR-12 | P0 | Quantities MAY be requested in litres, kilograms or sales units; the gateway MUST convert to whole sales units and state the conversion. |
| FR-13 | P0 | Issuing an offer MUST place a hold on the offered quantity of Stock products until the offer expires or is used. |
| FR-14 | P0 | An offer MUST NOT reveal total available stock, only what is offered and whether the request is fully met. |
| FR-15 | P0 | An order MUST reference exactly one unexpired, unused offer issued to the same agent. There is no other way to order. |
| FR-16 | P0 | The gateway MUST offer no counter-offer or price negotiation operation. |
| FR-17 | P0 | Repeating an order request with the same idempotency key MUST return the original result and MUST NOT create a second order. |
| FR-18 | P0 | Each offer line MUST state the stock assurance: `confirmed` for Stock products, `unconfirmed` for Terminal products. |
| FR-19 | P1 | When one product cannot meet the quantity, the gateway SHOULD fill the remainder from the agent's alternates, then from substitutes, and mark each line's role. |
| FR-20 | P1 | The gateway SHOULD return up to three ranked options (best, better, good) with stated differences. |
| FR-21 | P1 | An agent SHOULD be able to ask, per offer line, that the gateway fill from the cheapest of its listed candidates (`prefer: "lowest_price"`), because only the gateway knows the prices when the offer is built (added 2026-11-14). |
| FR-22 | P1 | An agent SHOULD be able to withdraw its own unused offer when it requests a corrected one (`replaces_offer_id`), so the new request does not compete with the old offer's stock holds (added 2026-11-14). |

### 4.3 Permission and approval (Core 3)

| ID | Priority | Requirement |
|---|---|---|
| FR-30 | P0 | Each agent MUST be bound to one customer and one active mandate. |
| FR-31 | P0 | Every order MUST be evaluated against the mandate on the gateway and return exactly one outcome: `accepted`, `needs_approval` or `rejected`. |
| FR-32 | P0 | The response MUST list every rule that fired, with a machine-readable code and a plain-language reason. |
| FR-33 | P0 | An agent MUST be able to run the same evaluation without committing (dry-run). |
| FR-34 | P0 | An order needing approval MUST be parked with its stock held, and a named approver MUST be able to approve or decline it from a mobile page. |
| FR-35 | P0 | The agent MUST NOT receive the approval link or any credential that would let it approve its own order. |
| FR-36 | P0 | The agent MUST be able to poll order status. |
| FR-37 | P0 | A hard allergen constraint MUST be enforced on every line, and missing allergen data MUST be treated as not confirmed safe. |
| FR-38 | P1 | An agent SHOULD be able to read its own mandate. |

### 4.4 Comparison (Optional 1)

| ID | Priority | Requirement |
|---|---|---|
| FR-40 | P1 | Offer lines SHOULD carry delivery reliability, labels and group-average carbon footprint where available. |
| FR-41 | P1 | Ranked options SHOULD include a score breakdown per criterion. |
| FR-42 | P2 | An order-level carbon estimate MAY be given where product weight is known. |

### 4.5 Substitution and exceptions (Optional 2)

| ID | Priority | Requirement |
|---|---|---|
| FR-50 | P1 | Substitutes SHOULD be drawn first from Valio Aimo's own substitution rules, then from products in the same category that satisfy the line's requirements. |
| FR-51 | P1 | Each substitute SHOULD state its differences from the requested product: price, claims, labels, origin, reliability. |
| FR-52 | P2 | The gateway MAY notify an agent of a stock-out affecting a confirmed order, with a ready-made substitute offer. |

### 4.6 Traceability (Optional 3)

| ID | Priority | Requirement |
|---|---|---|
| FR-60 | P1 | Every step of a purchase MUST be logged as an event linked to a trace. |
| FR-61 | P1 | Events in a trace SHOULD be hash-chained so that tampering is detectable. |
| FR-62 | P1 | A signed receipt SHOULD be available as JSON and as a plain-language timeline, with a buyer view and a Valio Aimo view. |

### 4.7 Openness

| ID | Priority | Requirement |
|---|---|---|
| FR-70 | P0 | The gateway MUST publish a discovery document and its public signing keys at well-known URLs. |
| FR-71 | P0 | The gateway MUST publish an OpenAPI description. |
| FR-72 | P1 | The same operations SHOULD be exposed through an MCP server. |

### 4.8 Stretch

| ID | Priority | Requirement |
|---|---|---|
| FR-80 | P2 | Offers inbox for a Valio Aimo selling agent, limited to categories the mandate opts into. |
| FR-81 | P2 | Standing-order drift rule: flag a renewal more than 15% above the 8-week average. |
| FR-82 | P2 | Sandbox conformance suite an agent must pass before receiving a live key. |

## 5. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-01 | The upstream API key MUST be stored as a secret and never returned, logged or sent to an agent. |
| NFR-02 | The gateway MUST NOT fetch prices or availability for products the caller has not shortlisted. An offer request is limited to 25 candidate products per line and 10 lines. |
| NFR-03 | Upstream answers MUST be cached for 60 seconds per (product, delivery date) to respect undisclosed rate limits. |
| NFR-04 | Policy evaluation MUST be deterministic: the same order, mandate version and offer always give the same outcome. No language model takes part in it. |
| NFR-05 | An offer request for one line with up to 25 candidates SHOULD complete within 3 seconds when upstream responds within 1 second. |
| NFR-06 | All state MUST live in MariaDB. The gateway process holds no state between requests other than the short upstream cache. |
| NFR-07 | The demo MUST be resettable to a known state with one command. |
| NFR-08 | The data set and keys MUST NOT appear in the repository. |

## 6. Acceptance scenario

The brief's example request is the acceptance test for P0.

> "I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our requirements based on price and availability."

1. The agent searches with category `10400` and claim `FREE_FROM_LACTOSE` and receives plain lactose-free milks.
2. The agent requests an offer for 1,000 L for delivery on Monday 16 November 2026.
3. The gateway returns a signed offer with whole sales units, fixed prices, totals and stock assurance. If one product is short, the offer is split (P1).
4. The agent verifies the signature against the published key.
5. The agent runs a dry-run and learns the outcome and the rules that fired.
6. The agent places the order. If the total exceeds the mandate's cap, the outcome is `needs_approval`.
7. The approver opens the approval page and approves.
8. The agent polls and sees the order `confirmed`.
9. A receipt shows the full timeline.

The mandate's spend cap for the demo is set after live prices are known (Q-03).

## 7. Traceability to the brief

| Brief item | Requirements |
|---|---|
| Core 1: product, price and availability lookup | FR-01 to FR-06, FR-10 to FR-12 |
| Core 2: inquiry to commitment on fixed prices | FR-10 to FR-18 |
| Core 3: permission and human approval | FR-30 to FR-38 |
| Optional 1: delivery and sustainability comparison | FR-06, FR-40 to FR-42 |
| Optional 2: substitution and exceptions | FR-19, FR-20, FR-50 to FR-52 |
| Optional 3: traceability and explainability | FR-60 to FR-62 |
| Trust protocol | FR-11, FR-18, FR-70 |
| Guardrails model | FR-31 to FR-37 |
| Response logic (partial availability, good-better-best) | FR-19, FR-20, FR-50, FR-51 |
| Audit trail | FR-60 to FR-62 |
| "Safely and openly readable" | FR-04, FR-05, FR-14, FR-70 to FR-72, NFR-01 |
