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
| API-03 | `GET /v1/products?exclude_allergens=AM&category=10400` | No item has `AM` in state `CONTAINS` or `MAY_CONTAIN`; every item has `AM` `FREE_FROM`. In the real data no product in 10400 states `AM` `FREE_FROM`, so the list is empty; the test also checks `exclude_allergens=AM,AE` across the whole catalog for a non-empty case. |
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
| Mandate 1 | Cap `1000.00` EUR per order excl. tax (Q-03, confirmed); all categories; no forbidden allergens; `official_rules_only`; weekdays 1 to 6 |
| Customer 2 | Koivula School Kitchens |
| Agent 2 | "Koivula purchasing agent" |
| Approver 2 | Antti Laine |
| Mandate 2 | Cap `500.00`; forbidden allergens `["AM"]`, states `CONTAINS` and `MAY_CONTAIN`, `unknown` = `reject` |

Mandate 2 otherwise follows mandate 1 (all categories, `official_rules_only`, weekdays 1 to 6) and has no preferred labels. Approver contacts are `maria@example.com` and `antti@example.com`. `seed_demo.py` is idempotent: it reuses customers and agents by name and replaces a mandate only when its content differs, so re-running it restores the demo mandates. Agent keys are generated when an agent is created (or with `--rotate-keys`), stored as hashes, and written in clear only to the git-ignored `keys/demo_agents.json`; they are printed only with `--show-keys`.

Set mandate 1's cap just below the live cost of 1,000 L of the cheapest shortlist product, so the example request needs approval. On 2026-11-14 that cost was 1010.00 EUR excl. tax (464306; document 06, section 4.2), so the cap is `1000.00` (Q-03).

The main scenario does not split: 1,000 L of 464306 is filled by one product, because its stock is reported as 9999999999 (no fill limit, Q-15). No hold is pre-created, so A-24 is not used. The optional split scenario below uses real stock instead.

## 6. Demo script

About three minutes; confirm the allowed length and format with the organisers (Q-10). Split screen: agent console on the left, a phone on the right. Run `reset_demo.py` first.

| Step | Action | On screen | Proves |
|---|---|---|---|
| 1 | Type the brief's example request, word for word | The request | The brief is met as written |
| 2 | Agent calls `get_mandate`, then `search_products` | Structured results with claims and allergens | Core 1 |
| 3 | Agent calls `request_offer` | Signed offer for 1,000 units of 464306, expiry countdown, stock assurance `confirmed` | Core 2 |
| 4 | Run `verify_offer.py` | "Signature valid", then "Signature invalid" for the tampered copy | Trust protocol |
| 5 | Agent calls `dry_run_order` | `needs_approval: spend_cap_exceeded` | Core 3, guardrails |
| 6 | Agent calls `place_order`; scan the QR on the console | Approval page on the phone; tap Approve | Core 3, human sign-off |
| 7 | Agent polls; order `confirmed` | Receipt timeline | Traceability |
| 8 | Run the anonymous search | Same products, no prices, no stock | Data ownership |
| 9 (optional) | Run `allergy_guard` | Refusal: "Lactose-free is not milk-free." | Unknown is not safe |
| 10 (optional) | Run `scripted.py --scenario milk_split`: 1,000 L with 469363 listed first, then 464306, `substitution: allowed` | Split offer: 384 units of 469363 (`requested`, the real stock figure) plus 616 units of 464306 (`alternate`) | Exception handling on real partial availability |

The split in step 10 comes from the agent's own alternates (T-106), so it works even if T-404 (gateway-added substitutes) is not built. It consumes 384 units of 469363 for the rest of the run, so run `reset_demo.py` before repeating it.

**No unconfirmed-stock step in the live demo.** Every Terminal product in the shortlist has an upstream cut-off of 12 November 15:00 that has already passed, and the gateway does not offer past a cut-off (Q-11). An `unconfirmed` line therefore cannot appear on real data. The behaviour is covered by test F-04 and is answered verbally (section 8, "What about products where you do not know the stock?").

### 6a. Public demo for judges (added 2026-11-14)

The submission's live URL is the landing page `https://minhanhhh233--handshake-web.eu-west.modal.run/`. A judge needs no setup:

| Button | What happens | Proves |
|---|---|---|
| Run the example order | Mandate, search, earliest deliverable date from upstream, signed offer (signature verified against the published key), dry run (`needs_approval`), order. The judge opens the approval link or scans the QR, approves, and sees the order confirmed and the signed receipt. | Core 1, 2, 3; trust protocol; audit trail |
| Milk-allergy guard | The milk-allergy agent's offer request is refused for every product, with "Lactose-free is not milk-free." | Unknown is not safe |
| Anonymous search | Search without a key: structured attributes, no price or stock field | Data ownership |

Landing page structure (redesign 2026-11-14): hero with the discovery `curl` command and a copy button, "Run the example order" and "View API docs", and badges for the standards used (MCP, OpenAPI, JWS EdDSA, JWKS, GS1 GTIN); six capability cards; four figures read from the system (product count from the database, 8 agent tools, 3 outcomes, offer lifetime from configuration); the live demo band; a five-step "How it works" strip; and the footer disclaimer, which also appears on the approval, run and receipt pages. One accent colour (spruce); lingonberry is kept only as a status colour for over-limit, declined and refused states.

Dark theme and developer sections (2026-11-14, last feature change before submission): near-black background, 1px borders, rounded cards; spruce stays the one accent (lightened `#6CC49A` for monospace paths, commands and JSON). Each capability card has a small self-drawn icon top-left and a pill tag top-right. New sections after the guided demo:

- Playground: six cards for the example order (search, request offer, verify signature, dry run, place order, receipt), each with the tool name, the JSON input, "Try it live" and the real JSON response in a scrollable block (`POST /demo/playground/{step}`, document 03, section 8). Runs as the public-demo agent under the same per-visitor limit; the visitor sees only their own order's approval link.
- Discovery: one row per public endpoint (path in monospace, standard badge, description, content type, open link), including the new `/llms.txt`.
- Quick start: four copyable cards (discovery `curl`, anonymous search `curl`, PyJWT signature check, `claude mcp add` with an `<agent key>` placeholder).
- Run page: an expandable "Request / response" block under each step, with copy buttons.

JavaScript: one self-hosted file (`/static/site.js`) on the landing and run pages only, CSP `script-src 'self'`; approval and receipt pages stay script-free.

Rules: separate public-demo customer and agents (rehearsal data untouched); no Claude API calls; 5 runs per 10 minutes per visitor; public-demo approvals expire after 30 minutes; the admin key, the console and other orders' approval links are never reachable from these pages. Tests: `tests/test_public_demo.py` (scenarios, approval, expiry, limit, tokens), `tests/test_playground.py` (all six steps end to end, prerequisites, session tokens and scope, shared visitor limit and search cap, 415, no agent or admin key and no approval link in the agent response, `llms.txt`, `site.js`, script policy only on landing and run pages) and `tests/test_public_demo_browser.py` (390 and 1280 px: guided demo, request/response blocks, playground "Try it live" through approval and receipt, copy button, no CSP violations, no horizontal scroll measured against the requested width).

### Fallbacks

| Problem | Response |
|---|---|
| Model slow or unavailable | Run `scripted.py --scenario milk_order`; same console output |
| Venue network down | Play the recorded video |
| Upstream API down | Play the recorded video; explain that offers are never issued from stale data |
| Approval page will not load on the phone | Open the approval link in a desktop browser from the console |

### 6c. Personas and sandboxes (stage 1, added 2026-11-14)

- Persona bar, sticky at the top of the page, no login: Visitor, Hotel chain, Restaurant, School kitchen, Hospital kitchen, New agent on probation; Approver and Valio Aimo operations are shown but disabled until stages 2 and 4.
- Each visitor gets a private sandbox (cookie `hs_sbx`, 2 hours): per-session copies of each persona's customer, agent and mandate, created on first use. Holds, orders, weekly totals and events belong to those parties; holds are counted per sandbox.
- Mandates (approver: you): Hotel chain (Revontuli Hotels) 1000 EUR per order, all categories, any day. Restaurant (Bistro Kallio) 300 EUR per order and 800 EUR per week, Monday to Friday. School kitchen (Metsälä) 600 EUR, never milk (AM) or tree nuts (AN) in CONTAINS or MAY_CONTAIN, unknown allergen data rejected, category 22701 forbidden (energy and sports drinks: 78 products such as Red Bull, Monster and protein milkshakes), Monday to Friday. Hospital kitchen (Saari) 1500 EUR, never peanuts, tree nuts, sesame, crustaceans or molluscs, unknown rejected, `substitution.auto_approve: none`. New agent on probation (Pilot Café) 50 EUR, Monday to Friday.
- Mandate card for the active persona with an editable per-order cap (a new mandate version); the visitor can change it and rerun.
- Visitor persona: the agent has no key; search works, and the mandate, prices, availability and offers are refused with `401` and the registration guidance.
- Panels labelled "External buyer agent (not part of Handshake)" and "Handshake: the Valio Aimo interface"; a time-to-order timer per run (to the gateway's answer to place_order; "Run time, no order" otherwise).
- The three original scripted buttons stay until stage 2.

Tests: `tests/test_sandbox.py` (cookie flags and reuse, every persona's card, cap edit and its checks, forged cookie, isolation of parties, expiry stops the key and renews the sandbox, holds counted per sandbox while real customers still share theirs, live run as the persona and invisible to another sandbox, Visitor refused with the registration guidance, R2 on `forbidden_categories`) and the browser test (persona bar, Restaurant card, cap saved as version 2, timer label, at 390 and 1280 px). Verified on the hosted gateway with the real model: Restaurant Bolognese went to approval (over 300 EUR) with time to order 18.2 s; Visitor got search results and the refusal with how to register; School was refused Red Bull for category 22701.

### 6b. Live agent demo (added 2026-11-14, time-boxed to 18:00 the same day)

The centrepiece of the landing page, above the scripted demos (which stay as the fallback). Two panels:

- Buyer agent: a text box (500 characters) with three example chips (Bolognese spaghetti for 400 portions, the lactose-free milk request, a creamy salmon soup for 200). After "Run the agent" it shows the visitor's request, the agent's short plan (ingredients with quantities) and each tool call's raw JSON input, in order.
- Valio Aimo interface (badge "Live API"): the gateway's raw JSON response next to each call; responses over 14 lines are collapsed with an Expand control. The offer's signature check is its own step (verified against the published JWKS).
- Human step: when the order is pending approval, an approval card with the link and QR code for that order only; after the decision, the outcome and a link to the signed receipt. Orders within the 1000 EUR cap (the Bolognese order) are confirmed without approval; the milk request uses `prefer: "lowest_price"`, comes out at 1010.00 EUR (464306) and goes over the cap.
- Delivery: the page polls `GET /demo/live/{trace}` every 1.5 s while the agent runs and every 4 s while the order waits (no stream held open). At 390 px the panels, and each call and response pair, stack.
- Limits and safety: document 07, section 8. When the daily budget is used up the box is disabled with a message pointing to the scripted demos; when upstream has no deliverable date the page says the order window has closed.

Stage 0 tests: `tests/test_offer_prefer_replace.py` (`prefer: "lowest_price"` fills 1,000 L from 464306 at 1010.00 EUR while the listed order starts with 464398; roles keep the agent's list; invalid values refused; `replaces_offer_id` withdraws the old offer, releases its holds and makes it unorderable; another agent's offer is not found; the live agent's retry carries `replaces_offer_id` and a third offer request is refused).

Tests: `tests/test_live_demo.py` (scripted fake model through the real gateway: steps, signature step, candidate and search caps, stop at pending approval, the model never sees approval links or keys, approval then confirmed with the receipt link, visitor text cannot close its tag, input checks, per-visitor limit, wrong token and non-live traces, budget disables the box, tool-call cap, order window closed, cost) and `tests/test_public_demo_browser.py::test_live_agent_panels` (390 and 1280 px: chip, run, approval card with QR, approve, confirmed, no horizontal scroll, no CSP errors). Verified on the hosted gateway with the real model at both widths.

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
