# 09. Testing and demo

## 1. Test strategy

| Level | Scope | Upstream | Database |
|---|---|---|---|
| Unit | Policy engine, quantity conversion, fill algorithm, hash chain, signing | None | None |
| API | Each endpoint through FastAPI's test client | Recorded fixtures | Test schema in MariaDB |
| End to end | Scripted agent against the deployed gateway | Live | Demo database |

Upstream fixtures are recorded once in task T-005 and replayed through a fake adapter. Fixtures contain challenge data and are not committed (README, data handling rules).

## 2. Unit tests

### 2.1 Policy engine

One test per row. "Base" is an offer of one requested line in category `10400`, total `500.00`, delivery on a Monday, Stock, with a mandate that has cap `1000.00`, no category limit, no forbidden allergens, `official_rules_only`, weekdays 1 to 6.

| ID | Change from base | Expected outcome | Expected codes |
|---|---|---|---|
| P-01 | None | accepted | none |
| P-02 | Total `1000.00` | accepted | none (cap is exceeded only when greater) |
| P-03 | Total `1000.01` | needs_approval | `spend_cap_exceeded` |
| P-04 | Agent suspended | rejected | `mandate_inactive` |
| P-05 | Mandate `valid_to` in the past | rejected | `mandate_inactive` |
| P-06 | `allowed_categories` = `["10430"]` | rejected | `category_not_allowed` |
| P-07 | Forbidden `AM`; product has `AM` `CONTAINS` | rejected | `forbidden_allergen` |
| P-08 | Forbidden `AN`; product has `AN` `MAY_CONTAIN`; states include it | rejected | `forbidden_allergen` |
| P-09 | Forbidden `AN`; product has `AN` `MAY_CONTAIN`; states = `["CONTAINS"]` | accepted | none |
| P-10 | Forbidden `AN`; product has `AN` `FREE_FROM` | accepted | none |
| P-11 | Forbidden `AN`; product has no row for `AN`; `unknown` = `reject` | rejected | `allergen_data_missing` |
| P-12 | As P-11 with `unknown` = `needs_approval` | needs_approval | `allergen_data_missing` |
| P-13 | As P-11 with `unknown` = `allow` | accepted | none |
| P-14 | Line role `substitute`, source official rule | accepted | none |
| P-15 | Line role `substitute`, source same category | needs_approval | `substitution_needs_approval` |
| P-16 | As P-15 with `auto_approve` = `any_meeting_requirements` | accepted | none |
| P-17 | As P-14 with `auto_approve` = `none` | needs_approval | `substitution_needs_approval` |
| P-18 | Line role `alternate` | accepted | none |
| P-19 | Delivery on a Sunday | needs_approval | `delivery_day_not_allowed` |
| P-20 | Total `1000.01` and forbidden allergen present | rejected | `forbidden_allergen`, `spend_cap_exceeded` (both reported) |
| P-21 | Total `1000.01`, Sunday | needs_approval | `spend_cap_exceeded`, `delivery_day_not_allowed` |
| P-22 | Line product lacks a claim in the line's `required_claims` | rejected | `requirement_not_met` |
| P-23 | `require_confirmed` true; line assurance `unconfirmed` (P1) | needs_approval | `unconfirmed_stock` |
| P-24 | Weekly cap `1500.00`; week total `1200.00`; this order `500.00` (P1) | needs_approval | `period_cap_exceeded` |
| P-25 | Same inputs evaluated twice | identical output | |
| P-26 | P-07 where the product has claim `FREE_FROM_LACTOSE` | rejected | message contains "Lactose-free is not milk-free." |

### 2.2 Quantity conversion

| ID | Input | Expected |
|---|---|---|
| C-01 | Unit price 10.00, comparison 1.00 per L, request 1000 L | 10 L per unit; 100 units |
| C-02 | Unit price 1.05, comparison 1.05 per L, request 1000 L | 1 L per unit; 1000 units |
| C-03 | 12 L per unit, request 1000 L | 84 units; offered 1008 L |
| C-04 | Comparison unit `KG`, request in `L`, product volume 1 L per unit known | Method `product_data`; 1000 units |
| C-05 | Comparison unit `KG`, request in `L`, no product volume | Excluded `no_quantity_conversion` |
| C-06 | `allowed_lot_size` 5, raw need 83 units | 85 units |
| C-07 | Comparison price 0 or null | Falls through to `product_data` |

### 2.3 Fill algorithm

| ID | Setup | Expected |
|---|---|---|
| F-01 | Requested product has capacity for the full quantity | One line, role `requested`, `complete: true`, `split: false` |
| F-02 | Requested has 600 units, first alternate has 500, need 1000 | Two lines: 600 `requested`, 400 `alternate` |
| F-03 | As F-02 with `fulfilment: exact` | One line of 600, `complete: false` |
| F-04 | Requested is Terminal | One line, assurance `unconfirmed`, full quantity |
| F-05 | Requested has `availableDate` after `delivery_date` | Excluded `not_deliverable_by_date`; alternate used |
| F-06 | Requested `salesBlock` true | Excluded `sales_blocked` |
| F-07 | Existing active hold of 300 on a product with upstream 600 | Capacity 300 |
| F-08 | Existing expired hold of 300, not yet marked released | Capacity 600 |
| F-09 | All candidates excluded | `422 no_candidates` with `excluded[]` |
| F-10 | Substitution allowed; requested short; official substitute exists | Substitute line with source `valio_aimo_substitution_rule` |
| F-11 | Candidate fails `required_claims` | Excluded `fails_requirements` before any upstream call |
| F-12 | Mandate forbids `AM`; candidate contains `AM` | Excluded `forbidden_by_mandate` |

### 2.4 Signing and chain

| ID | Check |
|---|---|
| S-01 | A signed offer verifies with the published key |
| S-02 | Changing one character of the payload makes verification fail |
| S-03 | A token with header `alg` other than EdDSA is refused by the verifier |
| S-04 | Chain of five events verifies; editing the body of event 3 breaks verification at event 3 |
| S-05 | Receipt `chain.head` equals the hash of the last event in the trace |

## 3. API tests

| ID | Call | Expected |
|---|---|---|
| API-01 | `GET /v1/products?category=10400&claims=FREE_FROM_LACTOSE&limit=50` | 35 items |
| API-02 | Same, anonymous | 35 items; no price or availability fields anywhere |
| API-03 | `GET /v1/products?exclude_allergens=AM&category=10400` | No item has `AM` in state `CONTAINS` or `MAY_CONTAIN`; every item has `AM` `FREE_FROM` |
| API-04 | `GET /v1/products/464306` | `substitutes` has four IDs; `untrusted_text.notice` present |
| API-05 | `POST /v1/offers` without a key | `401 unauthenticated` |
| API-06 | `POST /v1/offers` with 26 product IDs in one line | `400 invalid_request` |
| API-07 | `POST /v1/offers` valid | `201`; JWS verifies; holds exist; `offer.issued` event exists |
| API-08 | `POST /v1/orders/dry-run` twice | Same result; offer still `active` |
| API-09 | `POST /v1/orders` under the cap | `201`, `confirmed`; holds `committed` |
| API-10 | `POST /v1/orders` over the cap | `202`, `pending_approval`; response has no token or URL |
| API-11 | Repeat API-10 with the same `Idempotency-Key` | Same `order_id`; one order row |
| API-12 | Same key, different `offer_id` | `409 idempotency_conflict` |
| API-13 | Order on a consumed offer with a new key | `409 offer_consumed` |
| API-14 | Order on an expired offer | `409 offer_expired` |
| API-15 | Agent B reads agent A's order | `403 forbidden` |
| API-16 | `POST /approve/{token}` approve | Order `confirmed`; `approval.decided` event |
| API-17 | Reuse the same token | Explanatory page; no state change |
| API-18 | Decline | Order `declined`; holds `released` |
| API-19 | Approve after `expires_at` | Order `expired`; holds `released` |
| API-20 | Upstream fake raises a timeout during an offer request | `503 upstream_unavailable`; no offer row |
| API-21 | Milk-allergy agent requests an offer for lactose-free milk | `422 no_candidates`; every candidate excluded `forbidden_by_mandate` |

## 4. End-to-end test

`python agent/scripted.py --scenario milk_order` against the deployed gateway MUST finish with a `confirmed` order after a manual approval, and `--scenario allergy_guard` MUST finish with a refusal. Run both after every deploy.

## 5. Demo data

Created by `scripts/seed_demo.py`. All names are fictional.

| Item | Value |
|---|---|
| Customer 1 | Nordlys Hotels |
| Agent 1 | "Nordlys purchasing agent" |
| Approver 1 | Maria Virtanen |
| Mandate 1 | Cap placeholder `1000.00` EUR per order (Q-03); all categories; no forbidden allergens; `official_rules_only`; weekdays 1 to 6 |
| Customer 2 | Koivula School Kitchens |
| Agent 2 | "Koivula purchasing agent" |
| Approver 2 | Antti Laine |
| Mandate 2 | Cap `500.00`; forbidden allergens `["AM"]`, states `CONTAINS` and `MAY_CONTAIN`, `unknown` = `reject` |

Set mandate 1's cap just below the live cost of 1,000 L of the cheapest shortlist product, so the example request needs approval. If live stock means no split occurs naturally, pre-create a committed hold on the first-choice product in `seed_demo.py` so that it has less than 1,000 L available, and say so in the demo (A-24).

## 6. Demo script

About three minutes; confirm the allowed length and format with the organisers (Q-10). Split screen: agent console on the left, a phone on the right. Run `reset_demo.py` first.

| Step | Action | On screen | Proves |
|---|---|---|---|
| 1 | Type the brief's example request, word for word | The request | The brief is met as written |
| 2 | Agent calls `get_mandate`, then `search_products` | Structured results with claims and allergens | Core 1 |
| 3 | Agent calls `request_offer` | Signed offer, expiry countdown, lines with roles and stock assurance | Core 2; exception handling if split |
| 4 | Run `verify_offer.py` | "Signature valid", then "Signature invalid" for the tampered copy | Trust protocol |
| 5 | Agent calls `dry_run_order` | `needs_approval: spend_cap_exceeded` | Core 3, guardrails |
| 6 | Agent calls `place_order`; scan the QR on the console | Approval page on the phone; tap Approve | Core 3, human sign-off |
| 7 | Agent polls; order `confirmed` | Receipt timeline | Traceability |
| 8 | Run the anonymous search | Same products, no prices, no stock | Data ownership |
| 9 (optional) | Run `allergy_guard` | Refusal: "Lactose-free is not milk-free." | Unknown is not safe |

### Fallbacks

| Problem | Response |
|---|---|
| Model slow or unavailable | Run `scripted.py --scenario milk_order`; same console output |
| Venue network down | Play the recorded video |
| Upstream API down | Play the recorded video; explain that offers are never issued from stale data |
| Approval page will not load on the phone | Open the approval link in a desktop browser from the console |

## 7. Pitch outline

1. **Problem (30 s).** Agents are about to do the ordering. Whoever defines how they read a wholesaler owns the customer.
2. **Idea (30 s).** Three verbs from the brief: what agents can see, decide, and be trusted with.
3. **Demo (3 min).**
4. **Why it is open (30 s).** Published schema, signatures anyone can verify, any agent can connect.
5. **Next (30 s).** Agent conformance tests, proactive offers, stock-out events.

## 8. Likely judge questions

| Question | Answer |
|---|---|
| Why not just an API key and a REST API? | A key says who is calling. It does not say what they may decide, and gives the agent nothing to verify. Mandates and signed offers add both. |
| Does a signature prove the stock is there? | No. It proves Valio Aimo said so and is bound by it. Stock claims state whether they are confirmed, and the hold backs them. |
| What about products where you do not know the stock? | Terminal products are marked `unconfirmed`. A mandate can require approval for those. |
| What stops a competitor reading your inventory? | No stock figures beyond the offered quantity, authentication for offers, candidate and rate limits. |
| What if the agent misbehaves? | Every limit is enforced on Valio Aimo's side. |
| Can an agent split an order to stay under the cap? | The weekly cap rule covers that. If it was cut: it is a known limitation with a designed fix. |
| How does this fit the future marketplace? | Suppliers can sign their own claims in the same envelope. |
| Where is payment? | Out of scope. An approved order on a signed offer is the input a payment step would need. |
| Is this an MCP server or an API? | The rules are transport-neutral. Both are shown; which one production uses is outside the challenge scope. |
| How do you handle missing allergen data? | Nearly half the catalog has none. Unknown is treated as not confirmed safe. |

## 9. Demo readiness checklist

- [ ] Q-02 and Q-03 answered; cap set
- [ ] `reset_demo.py` returns the system to a known state
- [ ] Three clean run-throughs in a row
- [ ] Backup video recorded after the final deploy
- [ ] Phone on venue network can open the approval page
- [ ] Console font readable from the back of the room
- [ ] Submission mentions MariaDB (system-versioned tables, hash-chained log) and Modal
