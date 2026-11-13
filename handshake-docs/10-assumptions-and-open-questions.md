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
| A-19 | `logisticModel` contains the words "Stock" or "Terminal" in some casing. Anything else is treated as unknown and unconfirmed. | Wrong assurance labels. Confirm with Q-05. | 06 §1.4 |
| A-21 | Deposits (returnable packaging) are not added to offer totals. | Totals understate the real invoice for 506 products with deposits. See Q-12. | 06 §6 |

### Money and quantity

| ID | Assumption | If wrong | Affects |
|---|---|---|---|
| A-03 | Spend caps compare the order total excluding tax, as is usual in B2B purchasing. | Change the comparison field in rule R6 and R7. | 05 §2, §3 |
| A-04 | Products are ordered in whole sales units. An `allowedLotSize` of 0.01 (16,872 products) is treated as "any whole number" unless `variableUnit` is true. | Quantity rounding is wrong for weight-sold products. See Q-08. | 06 §5 |
| A-05 | `catalogPrice.value / comparisonPrice.value` gives the litres or kilograms in one sales unit accurately enough for ordering. | Wrong sales-unit counts. Cross-check against product volume in T-005. | 06 §5 |

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

## 3. Known limitations (accepted, not open)

- No payment, invoicing or credit checks.
- No real order is placed with Valio Aimo.
- Contract pricing per customer is not demonstrated.
- No key rotation or agent key revocation flow beyond suspending an agent.
- No notification channel for approvers other than the demo QR code.
- Without rule R7, the per-order cap can be avoided by splitting orders.
- The upstream cache is per process; several gateway containers may each call upstream.
- Category, unit and some allergen names are inferred.

## 4. Change log

| Date | Item | Change |
|---|---|---|
| 2026-11-13 | All | First version |
