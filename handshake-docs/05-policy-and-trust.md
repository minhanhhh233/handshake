# 05. Policy and trust

This document defines the mandate, the policy rules, the signing scheme, stock holds, and the event chain. The policy engine implements sections 2 to 4 as a pure function with no I/O.

## 1. Principles

1. **The gateway decides what is allowed.** The agent decides what to ask for. No language model takes part in policy evaluation (NFR-04).
2. **Unknown is not safe.** Missing allergen data never satisfies a constraint (FR-37).
3. **A signature proves who said something and that it was not altered.** It does not prove the statement is true. Truth comes from accountability: the gateway is bound to honour an active offer, and stock claims state their assurance.
4. **Answer the question asked.** Availability is disclosed only for the requested quantity (FR-14).
5. **Free text is data, never instruction.** Supplier text, agent notes and approver comments are stored and shown as untrusted.

## 2. Mandate

One mandate per agent (A-10). Stored in `mandates.document`; `mandate_id` and `version` are assigned by the gateway.

```json
{
  "mandate_id": "mnd_...",
  "version": 3,
  "agent_id": "agt_...",
  "customer_id": "cus_...",
  "valid_from": "2026-11-13T00:00:00+02:00",
  "valid_to": null,
  "approvers": [ { "name": "Maria Virtanen", "contact": "maria@example.com" } ],
  "limits": {
    "currency": "EUR",
    "max_order_total_excl_tax": "1000.00",
    "max_weekly_total_excl_tax": null
  },
  "allowed_categories": null,
  "forbidden_allergens": {
    "codes": [],
    "states": ["CONTAINS", "MAY_CONTAIN"],
    "unknown": "reject"
  },
  "substitution": { "auto_approve": "official_rules_only" },
  "delivery": { "allowed_weekdays": [1, 2, 3, 4, 5, 6] },
  "stock": { "require_confirmed": false },
  "preferences": {
    "weights": { "price": 0.6, "delivery_reliability": 0.2, "carbon": 0.1, "labels": 0.1 },
    "preferred_labels": ["GOODS_FROM_FINLAND_BLUE_SWAN"]
  },
  "proactive_offers": { "enabled": false, "categories": [] }
}
```

| Field | Type | Meaning |
|---|---|---|
| `approvers` | array, at least 1 | The first entry receives approval requests (A-10). |
| `limits.max_order_total_excl_tax` | money or null | Above this, an order needs approval. Compared with the offer total excluding tax (A-03). Null means no cap. |
| `limits.max_weekly_total_excl_tax` | money or null | P1. Sum of confirmed and pending orders in the current ISO week plus this order. |
| `allowed_categories` | array of codes or null | Null means all categories. |
| `forbidden_categories` | array of codes | Categories that may never be bought; default empty. Added 2026-11-14 so one category can be excluded without listing the others. |
| `forbidden_allergens.codes` | array of GS1 codes | Allergens that must not be bought. |
| `forbidden_allergens.states` | subset of `CONTAINS`, `MAY_CONTAIN` | Which statements count as a violation. |
| `forbidden_allergens.unknown` | `reject`, `needs_approval`, `allow` | What to do when a product has no statement for a forbidden code. Default `reject`. |
| `substitution.auto_approve` | `none`, `official_rules_only`, `any_meeting_requirements` | Which gateway-proposed substitutes pass without approval. |
| `delivery.allowed_weekdays` | ISO weekdays 1 to 7 | Delivery on another day needs approval. |
| `stock.require_confirmed` | boolean | If true, lines with unconfirmed stock need approval. |
| `preferences` | object | Used only for ranking options. Never affects the outcome. |
| `proactive_offers` | object | P2. Consent for the selling agent. |

The demo cap of `1000.00` is a placeholder until live prices are known (Q-03).

## 3. Policy rules

Inputs: the offer (lines, totals, delivery date), the current mandate, the agent's status, product facts (category, allergens), and for R7 the week's order totals.

All rules are evaluated and every firing rule is reported. The outcome is `rejected` if any firing rule has effect `rejected`; otherwise `needs_approval` if any has that effect; otherwise `accepted`.

| Rule | Code | Fires when | Effect | Priority |
|---|---|---|---|---|
| R1 | `mandate_inactive` | Agent is suspended, or now is outside the mandate's validity window | rejected | P0 |
| R2 | `category_not_allowed` | A line's category is not in `allowed_categories` (when not null), or is in `forbidden_categories` | rejected | P0 |
| R3 | `forbidden_allergen` | A line's product has a forbidden code in one of the listed states | rejected | P0 |
| R4 | `allergen_data_missing` | A line's product has no statement for a forbidden code | per `forbidden_allergens.unknown`: rejected, needs_approval, or does not fire | P0 |
| R5 | `requirement_not_met` | A line's product fails the request line's own `requirements` | rejected | P0 |
| R6 | `spend_cap_exceeded` | Offer total excl. tax is greater than `max_order_total_excl_tax` | needs_approval | P0 |
| R7 | `period_cap_exceeded` | Week total plus this order is greater than `max_weekly_total_excl_tax` | needs_approval | P1 |
| R8 | `substitution_needs_approval` | A line has role `substitute` and is not covered by `substitution.auto_approve` | needs_approval | P0 |
| R9 | `delivery_day_not_allowed` | The delivery date's weekday is not in `allowed_weekdays` | needs_approval | P0 |
| R10 | `unconfirmed_stock` | `require_confirmed` is true and a line's assurance is `unconfirmed` | needs_approval | P1 |

Notes:

- R3 and R4 look at each (product, forbidden code) pair. The allergen status is `CONTAINS`, `MAY_CONTAIN`, `FREE_FROM`, or unknown when the product has no row for that code. A product with no allergen data at all is unknown for every code (A-07, Q-09).
- R5 should never fire for an offer the gateway built, because the offer builder filters on requirements. It is a defensive check.
- R8: `official_rules_only` passes substitutes whose `substitution_source` is `valio_aimo_substitution_rule`. `any_meeting_requirements` passes all substitutes. `none` passes none. Lines with role `alternate` never fire R8, because the agent listed them itself.
- Evaluation always uses the mandate that is current at evaluation time, and records its version on the order.
- Without R7, an agent could stay under the per-order cap by placing several small orders. State this limitation if R7 is not built.
- **Cut for the deadline (document 08, section 0).** R10 (T-503) is not built: `stock.require_confirmed` is accepted and stored but not evaluated. R7 was restored and built on 2026-11-14: `week_total` is the sum of `total_excl_tax` of the same agent's orders with status `confirmed` or `pending_approval` created in the current ISO week (Monday 00:00 to Monday 00:00, Europe/Helsinki); the rule fires when `week_total` plus this order's total excl. tax is greater than `max_weekly_total_excl_tax`. Dry-run and order evaluation both use it. The cap is per agent, as the mandate is (A-10).

### Reason object

```json
{
  "rule": "R3",
  "code": "forbidden_allergen",
  "effect": "rejected",
  "message": "Product 464398 contains milk (AM), which this mandate forbids. Lactose-free is not milk-free.",
  "line_no": 1,
  "details": { "product_id": "464398", "allergen": "AM", "state": "CONTAINS" }
}
```

`message` is generated from a template per rule. For R3 on allergen `AM` where the product has the claim `FREE_FROM_LACTOSE`, append the sentence "Lactose-free is not milk-free."

### Reference implementation shape

```python
def evaluate(offer: Offer, mandate: Mandate, agent_status: str,
             facts: dict[str, ProductFacts], week_total: Decimal,
             now: datetime, requirements: dict[int, Requirements] | None = None) -> Evaluation:
    """Pure function. Returns outcome and list of reasons."""
```

Implementation notes (phase 2):

- `requirements` (request line number to the agent's `requirements`) was added because R5 checks each line against its own request line; `None` skips R5. `week_total` is accepted and ignored while R7 is cut.
- Reasons are ordered by rule number, then `line_no`, offer-level reasons first.
- A line whose product is missing from `facts` fails closed: it is treated as unknown category, no allergen data and no claims.
- A `substitute` line without `differences` counts as source `null`, so `official_rules_only` sends it for approval.
- Only the R3 and R6 messages are fixed by this document; the R1, R4, R5, R8 and R9 wording is the implementation's (R9 names the weekday, the date and the allowed days).
- `mandate_product_reasons(facts, mandate)` (R2, R3, R4 for one product) lives in `handshake/policy.py` and is shared by the offer builder's static filter (document 06, section 6, step 2).

## 4. Ranking options (P1)

Ranking never changes the outcome. It orders candidate products when filling a line and orders the returned options.

For each candidate that passed all filters:

- `price_score` = lowest comparison price among candidates divided by this candidate's comparison price.
- `reliability_score` = `delivery_reliability` if present.
- `carbon_score` = lowest carbon value among candidates divided by this candidate's, if present.
- `labels_score` = share of `preferred_labels` the product carries; 0 when the list is empty.

`score = sum(weight_i * score_i)` over the criteria that have a value for this candidate, divided by the sum of those weights. A missing value is left out; it is never treated as zero or as an average. The breakdown returned in the API lists each `weight_i * score_i` after this normalisation.

Options:

| Label | How it is built |
|---|---|
| `best` | Fill by descending score |
| `better` | Fill by ascending comparison price (cheapest), if different from `best` |
| `good` | Fill using the fewest distinct products, if different from the others |

Each option is a separate offer with its own ID, holds and signature. P0 returns only `best`, filled in the agent's preference order, or, for a line with `prefer: "lowest_price"` (added 2026-11-14, FR-21), by ascending price excl. tax per requested unit among the listed candidates that passed all filters (substitutes after them). That ordering never changes the policy outcome. **Cut for the deadline (document 08, section 0).** T-501 is not built, so only the P0 behaviour exists and `preferences` has no effect.

## 5. Signing

### 5.1 Scheme

| Item | Value |
|---|---|
| Algorithm | EdDSA with Ed25519 |
| Format | JWS compact serialisation |
| Protected header | `{"alg":"EdDSA","kid":"<SIGNING_KID>","typ":"handshake-offer+jwt"}` (or `handshake-receipt+jwt`) |
| Payload | The offer or receipt object as UTF-8 JSON, keys sorted, no insignificant whitespace |
| Key publication | `GET /.well-known/jwks.json` |

Money values are strings, so number formatting cannot differ between implementations. A single key is used for the event; rotation is out of scope (A-13).

### 5.2 What is signed

- **Offers:** the full offer object from document 03, section 5. The signature binds price, quantity, delivery date, expiry, stock assurance, the agent and the mandate version.
- **Receipts:** the receipt object, including the chain head hash and the original offer JWS.

### 5.3 Verification by an agent

1. Fetch the key set from `jwks_uri` in the discovery document; cache it.
2. Select the key whose `kid` matches the JWS header.
3. Verify the signature with algorithm EdDSA. Reject any other algorithm.
4. Parse the payload. Treat the payload, not the unsigned `offer` copy, as authoritative.
5. Check `issuer` equals the gateway's base URL, `agent_id` is the caller's own, and `expires_at` is in the future.

```python
import json, jwt, httpx
from jwt.algorithms import OKPAlgorithm

def verify(jws: str, base_url: str) -> dict:
    jwks = httpx.get(f"{base_url}/.well-known/jwks.json").json()
    kid = jwt.get_unverified_header(jws)["kid"]
    jwk = next(k for k in jwks["keys"] if k["kid"] == kid)
    key = OKPAlgorithm.from_jwk(json.dumps(jwk))
    payload = jwt.PyJWS().decode(jws, key, algorithms=["EdDSA"])
    return json.loads(payload)
```

Confirmed with PyJWT 2.10 (A-14). The reference client (`agent/handshake_client.py`) additionally refuses a header `typ` other than the expected one and any key that is not OKP/Ed25519. It fetches the key set at the path of `jwks_uri` through its configured base URL, so it also works behind a test client; the issuer check compares the payload's `issuer` with the discovery document's `api_base` minus `/v1`. Both well-known documents are served with `Cache-Control: public, max-age=300`.

### 5.4 Binding rule

While an offer is `active`, the gateway MUST accept an order for it at the signed price and quantity, subject only to the policy outcome. An agent holding a valid JWS and a refusal has proof of a broken commitment. This is what gives the signature practical value.

## 6. Stock holds

| Moment | Hold change |
|---|---|
| Offer issued | One hold per line: `held`, `expires_at` = offer `expires_at` |
| Offer expires unused | `released` (applied lazily; an expired `held` row is ignored in arithmetic even before it is updated) |
| Offer withdrawn by `replaces_offer_id` (added 2026-11-14) | `released`; the offer becomes `expired` |
| Order `confirmed` (accepted) | `committed`, `expires_at` = NULL |
| Order `pending_approval` | Stays `held`; `expires_at` moved to the approval's `expires_at` |
| Approval approved | `committed` |
| Approval declined or expired | `released` |
| Order `rejected` | `released` |

Available quantity is computed as in document 04, section 4. When several options are returned for one request, each option holds its own quantities; unused options release at expiry. This can briefly over-reserve and is accepted for the prototype (A-22).

## 7. Events and receipts

### 7.1 Event types

| Type | Written when | Actor | Audience |
|---|---|---|---|
| `search.performed` | Authenticated product search with an `X-Trace-Id` | agent | aimo_only |
| `product.read` | Authenticated product detail read with an `X-Trace-Id` | agent | aimo_only |
| `offer.requested` | Offer request received | agent | both |
| `upstream.queried` | Price or availability fetched or served from cache; body lists product IDs and snapshot IDs | gateway | aimo_only |
| `offer.issued` | Offer created and signed | gateway | both |
| `offer.expired` | Offer found expired on access | gateway | both |
| `order.dry_run` | Dry-run evaluated | agent | both |
| `order.submitted` | Order request received | agent | both |
| `policy.evaluated` | Outcome and reasons determined | gateway | both |
| `approval.requested` | Approval created | gateway | both |
| `approval.decided` | Approver approved or declined | approver | both |
| `approval.expired` | Approval window passed | gateway | both |
| `order.confirmed` | Order reached `confirmed` | gateway | both |
| `order.closed` | Order reached `declined`, `expired` or `rejected` | gateway | both |
| `holds.changed` | Holds committed or released | gateway | aimo_only |
| `mandate.changed` | The customer's purchasing manager changed the mandate (public demo cap edit; its own trace) | approver | both |

`policy.evaluated` text ends with the mandate version in force, for example "Evaluated against mandate version 2 (per-order cap 250.00 EUR, weekly cap 800.00 EUR)." (added 2026-11-14), so a receipt names the limits the order was judged by. Every event has a plain-language `text`, for example: "Total 1070.00 EUR exceeded the 1000.00 EUR cap. Sent to Maria Virtanen for approval."

### 7.2 Trace

A trace starts at the first call that supplies or is assigned a `trace_id`. `POST /v1/offers` assigns one if the request has none and returns it. Orders inherit the trace of their offer. Agents SHOULD send the same `X-Trace-Id` on searches that lead to an offer, so that the receipt shows the whole journey.

### 7.3 Hash chain

Chains are per trace (A-20).

```
canonical = JSON of {"event_id","trace_id","seq","type","at","actor_type","actor_id","body"}
            with keys sorted, separators "," and ":", UTF-8, "at" as UTC ISO 8601 with milliseconds
hash      = SHA-256( prev_hash_hex + "\n" + canonical ) as lowercase hex
prev_hash = hash of the previous event in the trace; 64 zeros for seq 1
```

Verification recomputes each hash in order and compares the last one with `chain.head` in the signed receipt.

### 7.4 Receipt

The receipt (document 03, section 6) contains the events with audience `both`, the chain head and length, a one-paragraph summary, and the original offer JWS. The chain is computed over all events in the trace, including `aimo_only` ones, so the buyer view can check the head but not recompute it. The Valio Aimo view shows all events (A-23).

## 8. Threats and controls

| Threat | Control |
|---|---|
| Agent exceeds its authority | Policy evaluated on the gateway for every order; no client-side trust |
| Agent approves its own order | Approval token never returned by any agent endpoint; delivered out of band (FR-35) |
| Price or quantity changed after quoting | Signed offer; orders reference the offer ID only and carry no prices |
| Replay or double submit | Offer is single-use; `Idempotency-Key` on orders |
| Competitor maps inventory by polling | No stock figures beyond the offered quantity; candidate limits; rate limits; anonymous callers get no availability |
| Bulk scraping of prices | Offers require authentication; 25 candidates per line; rate limits |
| Prompt injection through product text | Free text returned in a separate `untrusted_text` object with a notice; the agent prompt tells the model to ignore instructions in it; policy does not read it |
| Unsafe substitution | Requirements and forbidden allergens applied to every fill; unknown allergen status rejected by default |
| Tampering with the audit trail | Per-trace hash chain; head included in the signed receipt |
| Upstream key leak | Key only in the gateway's secret store; never logged |
| Order splitting to dodge the cap | Weekly cap R7 (P1); otherwise a stated limitation |

## 9. Fit with existing standards

Stated for the pitch; verify current versions before quoting them (A-14).

- Signed JSON uses JWS (RFC 7515) with EdDSA (RFC 8037) and a JWK set (RFC 7517).
- Product identity uses the GS1 GTINs already present in the data; allergen codes are GS1 codes as supplied through Synkka.
- The offer and mandate envelopes carry issuer, subject, validity and assurance, which is the same information a W3C Verifiable Credential would carry. Handshake does not implement that data model.
- The "server answers with a machine-readable requirement before proceeding" pattern is the same idea as HTTP 402 based agent payment protocols, applied to human approval instead of payment.
