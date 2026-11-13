# 10. Assumptions and open questions

This is the only place where assumptions and open questions are defined. Other documents cite them by ID.

- An **assumption** (A-nn) is something the design relies on that has not been confirmed. Each states what breaks if it is wrong.
- An **open question** (Q-nn) must be answered by a person or by a live check. Each has an owner, a deadline phase, and a default the team uses until it is answered.

When an item is resolved: record the answer here with the date, then update the listed sections.

## 1. Assumptions

### Scope and upstream

| ID | Assumption | If wrong | Affects |
|---|---|---|---|
| A-01 | The challenge has no upstream order API. An order is "confirmed" when Handshake records it; nothing is written to Valio Aimo. | If an order API exists, add an upstream call on confirmation and drop committed holds from the availability arithmetic. | 01 §2, 02 §5, 04 §4, 05 §6 |
| A-02 | The upstream `customer` value does not change prices or availability. Contract pricing is described in the pitch, not demonstrated. Tiered visibility means anonymous versus authenticated. | If customer-specific data appears, pass `customers.upstream_customer` and add a demo step. | 01 FR-05, 06 §1 |
| A-18 | Upstream `availableAmount` is not reduced by Handshake orders, so Handshake subtracts its own holds. | If upstream stock changes during the event, holds may double-count; acceptable for the prototype. | 04 §4 |
| A-19 | `logisticModel` contains the words "Stock" or "Terminal" in some casing. Anything else is treated as unknown and unconfirmed. **Confirmed 2026-11-14 (Q-05):** the live values are exactly `Stock` and `Terminal`. | Wrong assurance labels. | 06 §1.4 |
| A-21 | Deposits (returnable packaging) are not added to offer totals. | Totals understate the real invoice for 506 products with deposits. See Q-12. | 06 §6 |

### Money and quantity

| ID | Assumption | If wrong | Affects |
|---|---|---|---|
| A-03 | Spend caps compare the order total excluding tax, as is usual in B2B purchasing. | Change the comparison field in rule R6 and R7. | 05 §2, §3 |
| A-04 | Products are ordered in whole sales units. An `allowedLotSize` of 0.01 (16,872 products) is treated as "any whole number" unless `variableUnit` is true. | Quantity rounding is wrong for weight-sold products. See Q-08. | 06 §5 |
| A-25 | Tax is computed per line as invoices usually do: line total incl. tax = line total excl. tax x (1 + `taxRate` / 100), rounded half-up to cents once per line; order tax = sum of line taxes. Upstream `valueWithTax` is a per-unit price already rounded to cents (464306: 1.01 excl. becomes 1.15 incl.), so multiplying it by the units overstates tax (140.00 instead of 136.35 on 1010.00); it is kept in the price snapshot only. Valio Aimo's invoice rounding is not confirmed. | Totals including tax differ from Valio Aimo's invoice by a few cents per line. | 03 §5, 06 §1.3 |
| A-05 | `catalogPrice.value / comparisonPrice.value` gives the litres or kilograms in one sales unit accurately enough for ordering. **Checked 2026-11-14 (T-005):** for the six shortlist products with a volume in the product data, the ratio is within 0.07% of it. 465039 and 463038 (cases) have no volume in the product data, so only the ratio works for them (5.0073 L and 19.9427 L for a 5 L and a 20 L case; the gap comes from the comparison price being rounded to cents). | Wrong sales-unit counts. | 06 §5 |

### Product data

| ID | Assumption | If wrong | Affects |
|---|---|---|---|
| A-06 | Category, unit and allergen code names in `code_lookup` are inferred by the team and marked unverified. | Wrong labels on screen; no effect on logic, which uses codes. | 03 §3, 04, 06 §3.3 |
| A-07 | A product with no statement for an allergen has unknown status for it, even if it has statements for other allergens. | If silence means "free from" for products that list other allergens, the default is stricter than needed and more orders are rejected. See Q-09. | 05 §3, 06 §4 |
| A-08 | A lactose-free requirement is met only by the claim `FREE_FROM_LACTOSE`. `LOW_LACTOSE` does not count. | Fewer candidates than a buyer would accept. | 06 §4.1 |
| A-16 | Delivery reliability is a fill rate (quantity delivered versus ordered), not punctuality. | Mislabelled comparison field. | 06 §7 |
| A-17 | The carbon value is a product-group average per kilogram and is always labelled as such. | None for logic. | 06 §7 |

### Policy, approval and audit

| ID | Assumption | If wrong | Affects |
|---|---|---|---|
| A-09 | Delivering the approval link to the approver is out of band. In the demo it is a QR code on the console page, which only an admin can open. | A real deployment needs email, SMS or an app notification. | 02 §5.3, 03 §8 |
| A-10 | One mandate per agent and one approver per order are enough. | Multi-approver or per-category mandates need schema changes. | 05 §2 |
| A-11 | Offer lifetime 15 minutes and approval window 60 minutes suit the demo. Both are configuration. | Adjust `OFFER_TTL_SECONDS` and `APPROVAL_TTL_SECONDS`. | 02 §7 |
| A-12 | Expiry of offers, holds and approvals is applied lazily when a record is read. There is no background scheduler. | Stale `held` or `pending` rows remain until touched; arithmetic ignores expired holds, so results stay correct. | 02 §5.5, 05 §6 |
| A-13 | One signing key for the whole event; no rotation or revocation. | Out of scope for the prototype. | 05 §5 |
| A-20 | Event hash chains are per trace. There is no global chain across traces. | Deleting a whole trace would not be detectable. Acceptable for the prototype. | 05 §7.3 |
| A-22 | When several options are returned, each holds stock separately until expiry. Brief over-reservation is acceptable. | If it starves the demo, return one option only. | 05 §6 |
| A-23 | The buyer's receipt view omits `aimo_only` events but the chain covers all events, so the buyer can check the signed head but cannot recompute it. | If buyers must verify the chain themselves, keep two chains or expose event hashes without bodies. | 05 §7.4 |
| A-24 | If live stock does not produce a split for 1,000 L, the demo may pre-create a committed hold to force one, and the team says so. | None, provided it is disclosed. | 09 §5 |

### Tooling and team

| ID | Assumption | If wrong | Affects |
|---|---|---|---|
| A-14 | Library and platform details were written from general knowledge and must be checked against current documentation before use: Modal deployment and secrets, PyJWT's EdDSA and JWS calls, the MCP Python SDK, Claude model IDs, and the standards named in the pitch. | Minor code changes. | 02 §3, 05 §5.3, 05 §9, 07 |
| A-15 | About 30 working hours remain and the team has three or four people. | Apply the cut order in document 08. | 08 |

## 2. Open questions

| ID | Question | Owner | Needed by | Default until answered | Affects |
|---|---|---|---|---|---|
| Q-01 | Where does MariaDB run so that Modal can reach it, and does that instance support system-versioned tables? | BE-A; ask the MariaDB booth | Phase 0 | A small cloud VM running MariaDB 10.6 or later | 02 §8, 04 |
| Q-02 | What do the live price and availability APIs return for the eight shortlist products on 16 November 2026? Does any single product have 1,000 L? | AG (task T-005) | Phase 0 | Examples in the documents are illustrative | 03 §5, 06 §4.2, 09 §5 |
| Q-03 | What should the demo spend cap be? | AG, after Q-02 | Phase 2 | `1000.00` EUR | 05 §2, 09 §5 |
| Q-04 | What are the string formats of `closingTime`, `availableDate`, `validFrom`, `validTo`, `timeNow` and `pricingDate`? | AG (task T-005) | Phase 0 | Tolerant ISO 8601 parsing; no offset means Europe/Helsinki | 04 §2, 06 §1.4 |
| Q-05 | What exact values does `logisticModel` take, and which shortlist products are Terminal? | AG (task T-005) | Phase 0 | A-19 | 06 §1.4 |
| Q-06 | What are the upstream rate limits? | Ask Valio Aimo mentors | Phase 1 | 60-second cache; one price and one availability call per offer request; one retry | 06 §2 |
| Q-07 | What do the category, unit and temperature-condition codes mean? | Ask Valio Aimo mentors | Phase 1 | Inferred names, marked unverified | 06 §3.3 |
| Q-08 | What does `allowedLotSize` 0.01 mean for products sold by the piece or case? | Ask Valio Aimo mentors | Phase 1 | A-04 | 06 §5 |
| Q-09 | For a product that lists some allergens, does the absence of a statement for another allergen mean "free from" or "not declared"? | Ask Valio Aimo mentors | Phase 2 | A-07 (unknown) | 05 §3 |
| Q-10 | What are the judging criteria, pitch length and demo format? | FE; ask organisers | Phase 3 | Three-minute demo inside a five-minute pitch | 09 §6, §7 |
| Q-11 | If `closingTime` has passed, can the product still be ordered for a later date, and does `availableDate` already reflect that? | AG (task T-005) | Phase 1 | Exclude candidates whose cut-off has passed | 06 §6 |
| Q-12 | Should deposits appear in offer totals, and does the price API already include them? | Ask Valio Aimo mentors | Phase 4 | A-21 (excluded) | 06 §6 |
| Q-13 | May the code repository be public for judging if it contains no challenge data or keys? | FE; ask organisers | Phase 6 | Keep the repository private; share access on request | README |
| Q-14 | Does Valio Aimo want substitutions limited to its own substitution rules, or is "same category, same claims" acceptable as a second tier? | Ask Valio Aimo mentors | Phase 4 | Both tiers; second tier needs approval by default | 05 §3, 06 §6 |

### 2.1 Answers

| ID | Date | Answer | Source |
|---|---|---|---|
| Q-02 | 2026-11-14 | Live answers for 16 Nov 2026 are in document 06, section 4.2. All eight products are priced (`error: false`, `netPriceFound: true`) and all have `availableDate` 2026-11-16. Five are Stock, three are Terminal. Of the Stock products, 469363 reports 384 units and 462775 reports 51; 464398, 464201 and 464306 report `availableAmount` 9999999999, which is above the 999999 threshold and therefore "quantity unknown" under document 06, section 1.4. No product reports a known quantity of 1,000 L; the cheapest, 464306, can fill 1,000 L alone only if 9999999999 means "ample". See Q-15. | T-005: `/api/price` and `/api/availability`, called 2026-11-14 00:11 |
| Q-04 | 2026-11-14 | `closingTime`, `timeNow`, `requestedDate`, `pricingDate`, `validFrom` and `validTo` are ISO 8601 date-times with a numeric offset **without a colon**, for example `2026-11-15T17:00:00+0200`. `availableDate` is a plain date, `2026-11-16`. An open-ended `validTo` is `9999-12-31T00:00:00+0200`. Python 3.12 `datetime.fromisoformat` parses all of them. | T-005 |
| Q-03 | 2026-11-14 | The demo spend cap is **1000.00 EUR excl. tax**. The cheapest way to buy 1,000 L is 1010.00 EUR (464306), and every other product or mix costs more, so the example request always needs approval. 464306's price is valid from 2026-09-01 to 2026-12-31, which covers Sunday 15 and Monday 16 November. | Team decision on the T-005 prices |
| Q-05 | 2026-11-14 | `logisticModel` is exactly `Stock` or `Terminal`. Terminal in the shortlist: 465039, 464179, 463038. Terminal products report `availableAmount` 999999, as documented. | T-005 |

Observations from T-005 that bear on questions still open:

- **Q-11.** Decision 2026-11-14: keep the rule (a passed cut-off excludes the candidate). When every candidate fails on the delivery date, the `422 no_candidates` body names the earliest date known for each excluded product (document 03, section 5), so the agent can propose it.
- **Q-11 (observation).** The availability answer does not depend on `requestedDate`: requesting 2026-11-16 and 2026-11-23 returned identical bodies (`availableDate` 2026-11-16 for every product). So `closingTime` is the cut-off for `availableDate`, not for the requested date. All three Terminal products return `closingTime` 2026-11-12T15:00+0200, which had already passed when called. Under the current default (exclude candidates whose cut-off has passed), every Terminal shortlist product is excluded with `not_deliverable_by_date`. Stock products have `closingTime` 2026-11-15T17:00+0200, so offers for Monday 16 November cannot be issued after Sunday 17:00.
- **Q-06.** No rate-limit headers were returned. Each call took about 130 ms.

New question raised by T-005:

| ID | Question | Owner | Needed by | Default until answered | Affects |
|---|---|---|---|---|---|
| Q-15 | Stock products may report `availableAmount` 9999999999 (not the Terminal sentinel 999999). Does it mean "ample stock" or "unknown"? | Ask Valio Aimo mentors | Phase 1 | **Decided 2026-11-14 for the prototype:** any `availableAmount` of 999999 or more means no fill limit and no hold arithmetic. Assurance follows `logisticModel` only, so such a Stock product stays `confirmed` (FR-18); Terminal stays `unconfirmed`. | 04 §4; 06 §1.4, §6; 09 §5 |

## 3. Known limitations (accepted, not open)

- No payment, invoicing or credit checks.
- No real order is placed with Valio Aimo.
- Contract pricing per customer is not demonstrated.
- No key rotation or agent key revocation flow beyond suspending an agent.
- No notification channel for approvers other than the demo QR code.
- Without rule R7, the per-order cap can be avoided by splitting orders. R7 and R10 were cut for the deadline (document 08, section 0).
- No rate limiting (T-504 cut). Candidate limits (25 per line, 10 lines) still apply.
- No MCP server (T-502 cut) and one option per offer request (T-501 cut).
- The live demo cannot show an `unconfirmed` (Terminal) line: every Terminal shortlist product's cut-off has passed (Q-11). The behaviour is covered by test F-04.
- The upstream cache is per process; several gateway containers may each call upstream.
- Category, unit and some allergen names are inferred.

## 4. Change log

| Date | Item | Change |
|---|---|---|
| 2026-11-13 | All | First version |
| 2026-11-14 | A-25 | Per-line tax rounding adopted; upstream valueWithTax kept in snapshots only |
| 2026-11-14 | Q-03, Q-15, Q-11 | Cap set to 1000.00 EUR; Q-15 default adopted; Q-11 rule kept, earliest date added to 422; scope cut recorded under known limitations |
| 2026-11-14 | Q-02, Q-04, Q-05, A-05, A-19 | Answered from live calls in T-005; observations on Q-06 and Q-11 recorded; Q-15 added |
