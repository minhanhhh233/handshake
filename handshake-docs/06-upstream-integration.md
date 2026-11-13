# 06. Upstream integration and offer building

This document covers Valio Aimo's challenge APIs and data files, how they are loaded and called, and the algorithm that turns a request into an offer.

Source: upstream OpenAPI document version 0.1.0, the product data schema, and an analysis of the data files. No live call has been made yet (Q-02).

## 1. Upstream APIs

Base URL: `https://junction26.valioaimo.fi`. Every `/api` call needs the header `X-API-Key`.

| Endpoint | Use in Handshake |
|---|---|
| `GET /api/ping` | Connectivity check at deploy time |
| `POST /api/price` | Called by the upstream adapter when building an offer |
| `POST /api/availability` | Called by the upstream adapter when building an offer |
| `GET /api/products/download` | Used once to fetch the product file (follow the redirect) |
| `GET /api/delivery-reliability/download` | Used once |
| `GET /api/carbon-footprint/download` | Used once |

### 1.1 Constraints stated by Valio Aimo

- Do not retrieve prices or availability for all products. Narrow down with static data first.
- Undisclosed rate limits apply (Q-06).
- At most 1,000 product codes per request; more returns HTTP 422.
- The `customer` value does not affect responses in the challenge (A-02).
- The data files do not change during the challenge unless announced.

### 1.2 Request (both endpoints)

```json
{ "customer": "123", "requestedDate": "2026-11-16", "products": ["464398", "464201"] }
```

### 1.3 Price response

```json
{
  "customerId": "123",
  "currency": "EUR",
  "pricingDate": "...",
  "products": [
    {
      "productId": "464398",
      "quantity": 1.0,
      "catalogPrice":    { "value": 0.0, "valueWithTax": 0.0, "validFrom": "...", "validTo": "...", "currency": "EUR" },
      "priceWithTax":    { "value": 0.0, "validFrom": "...", "validTo": "...", "currency": "EUR" },
      "comparisonPrice": { "value": 0.0, "valueWithTax": 0.0, "validFrom": "...", "validTo": "...", "currency": "EUR" },
      "comparisonUnit": "L",
      "tax": { "taxRate": 13.5, "taxValue": 0.0 },
      "listPrice": { "value": 0.0, "valueWithTax": 0.0, "validFrom": "...", "validTo": "...", "currency": "EUR" },
      "netPriceFound": false,
      "error": false
    }
  ]
}
```

Values are placeholders. Field meanings from the upstream documentation:

| Field | Meaning | Handshake use |
|---|---|---|
| `catalogPrice.value` | Price per sales unit before tax | `unit_price_excl_tax` |
| `catalogPrice.valueWithTax` | Same with tax | Line total including tax |
| `comparisonPrice.value`, `comparisonUnit` | Price per kilogram or litre; unit is `KG` or `L` | `comparison_price`; quantity conversion (section 5) |
| `tax.taxRate` | Percent | `tax_rate_percent` |
| `validFrom`, `validTo` | Price validity period | `price_valid` |
| `listPrice` | Price before customer-specific price or discount | Stored in the snapshot only |
| `netPriceFound` | True when a customer-specific net price was found | Stored in the snapshot only |
| `error` | True when no price could be found | Candidate excluded with `price_unavailable` |

Any of `catalogPrice`, `comparisonPrice`, `tax` may be null. A candidate with a null `catalogPrice` is excluded with `price_unavailable`.

### 1.4 Availability response

```json
{
  "timeNow": "...",
  "requestedDate": "...",
  "products": [
    {
      "productId": "464398",
      "closingTime": "...",
      "salesBlock": false,
      "logisticModel": "...",
      "scheduleLines": [
        { "salesBlock": false, "availableAmount": 0.0, "availableDate": "...", "salesUnit": "ST" }
      ]
    }
  ]
}
```

| Field | Meaning | Handshake use |
|---|---|---|
| `closingTime` | Order by this time to receive goods on `availableDate` | `availability.order_by`; caps `expires_at` |
| `salesBlock` (product or line) | Product cannot be ordered now | Candidate excluded with `sales_blocked` |
| `logisticModel` | Stock products are stocked by Valio Aimo; Terminal products ship from suppliers and their quantity is unknown | `logistic_model` and `assurance` |
| `scheduleLines` | Exactly one entry if available; empty means not deliverable on any date | Empty: excluded with `out_of_stock` |
| `availableAmount` | Quantity in `salesUnit`; always 999999 for Terminal; 0 means not available | Fill limit for Stock products |
| `availableDate` | Earliest date the amount is available; may differ from the requested date | Later than `delivery_date`: excluded with `not_deliverable_by_date` |

Observed live formats (Q-04, Q-05, answered 2026-11-14): date-times such as `closingTime`, `timeNow`, `validFrom` and `validTo` look like `2026-11-15T17:00:00+0200` (offset without a colon); `availableDate` is a plain date; `logisticModel` is `Stock` or `Terminal`. Stock products may report `availableAmount` 9999999999 (Q-15). The answer does not vary with `requestedDate` (see Q-11 in document 10). The adapter MUST still:

- Parse dates with a tolerant ISO 8601 parser and treat values without an offset as Europe/Helsinki.
- Normalise `logisticModel` case-insensitively: a value containing `stock` becomes `stock`, one containing `terminal` becomes `terminal`, anything else becomes `unknown` with assurance `unconfirmed` (A-19).
- Treat `availableAmount >= 999999` (this includes the 9999999999 that some Stock products report) as unknown quantity regardless of `logisticModel`: no fill limit and no hold arithmetic. Assurance still follows `logisticModel`, so such a Stock product is `confirmed` (Q-15).
- Store the raw response in the snapshot tables.

## 2. Upstream adapter

```python
def get_facts(product_ids: list[str], delivery_date: date) -> dict[str, UpstreamFacts]:
    """Returns price and availability per product. Uses the cache; calls upstream for misses only."""
```

- Cache key: (product ID, delivery date). Lifetime `UPSTREAM_CACHE_TTL_SECONDS` (60). The cache is in-process; the snapshot tables are the durable record.
- One price call and one availability call per offer request, each with all cache misses, issued concurrently.
- Timeout `UPSTREAM_TIMEOUT_SECONDS`. On timeout, 5xx or 429: one retry after 500 ms, then raise `upstream_unavailable`.
- Each call writes one `price_quotes` or `availability_snapshots` row per product and an `upstream.queried` event.
- Never called from the search endpoints (FR-02).

Implementation notes (phase 1):

- `comparison_unit` is taken only when `comparisonPrice` is present. Currency comes from `catalogPrice.currency`, falling back to the response's top-level `currency`.
- An answer that is not valid JSON, or not an object, raises `upstream_unavailable` without a retry.
- A product upstream did not return gets no snapshot rows; that empty result is cached for the TTL like any other.
- The `upstream.queried` event is written for every `get_facts` call, including when everything came from cache. Its `actor_id` is null; the agent's ID is in the body, with `delivery_date`, `product_ids`, `fetched`, `cache_hit`, `missing`, `price_quote_ids` and `availability_snapshot_ids`.
- `availability_snapshots.available_amount` stores the raw upstream amount (9999999999 included); only the in-memory value becomes "unknown".

## 3. Data files

| File | Rows | Key | Notes |
|---|---|---|---|
| Product data (JSON, 87 MB) | 17,546 products | `id` | 349 category codes, 648 vendors |
| Delivery reliability (CSV) | 4,425 | `productId` | Column `deliveryReliability`, 0 to 1 |
| Carbon footprint (CSV) | 11,033 | `productId` | Column `AVG kg CO2-eq/kg`; 177 distinct values because it is a product-group average |

### 3.1 Coverage in the product file

| Attribute | Products with a value |
|---|---|
| English name | 17,544 |
| Country of origin | 17,537 |
| Sales unit GTIN | 17,387 |
| Unit conversions (weights, volumes) | 14,474 |
| Nutrition values | 12,251 |
| Any allergen or non-allergen statement | 9,436 (8,110 have none) |
| Structured ingredients | 3,746 |
| Substitution rules | 189 products, 250 rules |

### 3.2 Structured values

- **Claims** (`nutritionalClaim`): `FREE_FROM_LACTOSE` (4,178 products), `FREE_FROM_GLUTEN`, `VEGAN`, `VEGETARIAN`, `LOW_LACTOSE`, `FREE_FROM_EGGS`, `FREE_FROM_SOY`, and others.
- **Allergens**: group `allergen` with state `CONTAINS` or `MAY_CONTAIN`; group `nonAllergen` with state `FREE_FROM`. Codes are GS1 allergen codes.
- **Labels** (`packagingMarkedLabel`): `GOODS_FROM_FINLAND_BLUE_SWAN`, `MADE_IN_FINLAND_FLAG_WITH_KEY`, `FINNISH_HEART_SYMBOL`, `EU_ORGANIC_FARMING`, `SVANEN`, `RAINFOREST_ALLIANCE`, `EU_ECO_LABEL`, `MARINE_STEWARDSHIP_COUNCIL_LABEL`, `FAIR_TRADE_MARK`, `AQUACULTURE_STEWARDSHIP_COUNCIL`, and others. The same group also contains `warmTemperature`, `coolTemperature` and `freezeTemperature`, which are not certificates and are dropped by the loader.
- **Sales units**: `KI` (8,005 products), `ST` (5,607), `KG` (1,068), `PAK`, `BAG`, `RAS`, `PRK`, `BOT`, and others.

### 3.3 Code lookup

The data set has no names for category, unit or temperature codes (Q-07). The team maintains `code_lookup`. Entries below are inferred and stored with `verified = FALSE` (A-06).

| Kind | Code | Inferred name |
|---|---|---|
| category | `10400` | Milk and milk drinks |
| category | `10411` | Fermented milk (viili) |
| category | `10430` | Quark |
| unit | `ST` | Piece |
| unit | `KI` | Case |
| unit | `PAL` | Pallet |
| unit | `KRS` | Pallet layer |
| unit | `PAK` | Pack |
| unit | `PRK` | Tub or jar |
| unit | `RAS` | Box |
| unit | `BOT` | Bottle |
| unit | `BAG` | Bag |
| unit | `KG` | Kilogram |
| allergen | `AM` | Milk |
| allergen | `ML` | Lactose |
| allergen | `AW` | Cereals containing gluten |
| allergen | `UW` | Wheat |
| allergen | `AE` | Eggs |
| allergen | `AN` | Tree nuts |
| allergen | `AP` | Peanuts |
| allergen | `AY` | Soybeans |
| allergen | `AF` | Fish |
| allergen | `AC` | Crustaceans |
| allergen | `UM` | Molluscs |
| allergen | `AS` | Sesame |
| allergen | `BM` | Mustard |
| allergen | `BC` | Celery |
| allergen | `AU` | Sulphites |
| allergen | `NL` | Lupin |

`AM` is confirmed by the data to the extent that all 35 lactose-free products in the milk category carry `AM` with state `CONTAINS`. Verify the other allergen names against the GS1 allergen code list before showing them to users.

## 4. Product search

`GET /v1/products` builds one SQL query over `products` and the three attribute tables.

| Parameter | SQL effect |
|---|---|
| `q` | `MATCH(name_en, name_fi) AGAINST (:q IN NATURAL LANGUAGE MODE)`; results ordered by relevance. If `q` is shorter than 3 characters, fall back to `LIKE`. |
| `category` | `category_code = :category` |
| `claims` | One `EXISTS` on `product_claims` per claim |
| `labels` | One `EXISTS` on `product_labels` per label |
| `exclude_allergens` | For each code: require a `product_allergens` row with state `FREE_FROM`. Products with `CONTAINS`, `MAY_CONTAIN` or no row are excluded. |
| `name_excludes` | `name_en NOT LIKE` and `name_fi NOT LIKE` per term |

Without `q`, order by `delivery_reliability DESC` (nulls last), then `product_id`. Paging uses a keyset cursor on the ordering columns.

### 4.1 Lactose-free milk: what the data requires

- Search by category `10400` and claim `FREE_FROM_LACTOSE`. This returns 35 products.
- Do not search by name alone. Finnish lactose-free milk is labelled "milk drink" (maitojuoma), so many products do not contain the phrase "lactose free milk".
- The 35 include sour milk (piimä), coffee milk, a latte drink and portion packs. For plain milk, pass `name_excludes=sour,piimä,coffee,kahvi,latte,portion,annos,café,cafe`. 26 products remain. One of them is 463380 "Arla Cafè milk" (grave accent), which is a coffee milk; add `cafè` to the list to exclude it as well (25 remain).
- `LOW_LACTOSE` is a different claim and does not satisfy a lactose-free requirement (A-08).
- All 35 carry allergen `AM` with state `CONTAINS`. Lactose-free is not milk-free.
- Delivery reliability is present for about half of them and is 0.99 or higher in every case.

### 4.2 Demo shortlist

These eight products are plain lactose-free milks in 1 litre or 10 litre formats. Live answers for delivery on 16 November 2026, called on 2026-11-14 (Q-02). Prices are EUR per sales unit excluding tax; tax is 13.5% for all eight. "Cost of 1,000 L" is whole sales units at the catalog price, excluding tax.

| Product ID | English name | Sales unit | Case size | Price | Per L | Model | Available | Cut-off | Cost of 1,000 L |
|---|---|---|---|---|---|---|---|---|---|
| 464398 | Valio Eila® semi skimmed milk drink 1 l UHT lactose free | ST | 10 | 1.65 | 1.65 | Stock | 9999999999 (Q-15) | 15 Nov 17:00 | 1650.00 |
| 464201 | Valio semi skimmed milk 1 l lactose free UHT | ST | 10 | 1.56 | 1.56 | Stock | 9999999999 (Q-15) | 15 Nov 17:00 | 1560.00 |
| 465039 | Valio Eila® semi skimmed milk drink 1 l lactose free | KI | 5 | 6.86 | 1.37 | Terminal | unknown | 12 Nov 15:00 (passed) | 1372.00 |
| 464306 | Valio Hyvä suomalainen Arki® Eila® semi skimmed milk drink 1 l lactose free | ST | 20 | 1.01 | 1.01 | Stock | 9999999999 (Q-15) | 15 Nov 17:00 | 1010.00 |
| 469363 | Arla Semi skimmed milkdrink 1 L UHT lactose free Suomesta | ST | 12 | 2.48 | 2.48 | Stock | 384 | 15 Nov 17:00 | 2480.00 |
| 462775 | Arla Milkdrink 1L lactose free fat free UHT Suomesta | ST | 12 | 2.57 | 2.57 | Stock | 51 | 15 Nov 17:00 | 2570.00 |
| 464179 | Arla 10 L Novobox Suomi semi skimmed lactose free milk drink ESL | ST | 1 (sold singly) | 27.22 | 2.72 | Terminal | unknown | 12 Nov 15:00 (passed) | 2722.00 |
| 463038 | Valio Eila® semi skimmed milk drink 10 l novobox lactose free | KI | 2 | 31.31 | 1.57 | Terminal | unknown | 12 Nov 15:00 (passed) | 1596.81 |

Delivery reliability is 1.000 where present (464398, 464201, 465039, 464306). All eight have a carbon value of 1.1 kg CO2e per kg.

Product 464306 has four official substitutes: 465039, 464398, 469363 and 464201.

## 5. Quantity conversion

Upstream prices and availability are per sales unit. Agents ask in litres or kilograms. `base_per_sales_unit` is how many litres or kilograms one sales unit contains.

Methods, in order:

1. **`price_ratio`**: if `comparisonUnit` equals the requested unit (`L` or `KG`) and both prices are positive, `base_per_sales_unit = catalogPrice.value / comparisonPrice.value`. Round to 4 decimals. This works even where the product file has no volume (A-05).
2. **`product_data`**: use `net_volume_l_per_sales_unit` or `net_weight_kg_per_sales_unit` from the loader.
3. Otherwise exclude the candidate with `no_quantity_conversion`.

When both methods give a value and they differ by more than 2%, use `price_ratio` and log a warning.

Loader derivation for method 2, from `synkkaData.unitConversions` and `units`:

| Sales unit | Available data | Result |
|---|---|---|
| Equals base unit | `BASE_UNIT_OR_EACH` volume or net weight | That value |
| `KI` (case) | `CASE` volume or net weight | That value |
| `KI` (case) | Only `BASE_UNIT_OR_EACH` value, and `units` gives the case size in base units | Base value multiplied by case size |
| Anything else | | NULL |

Convert `MLT` to litres and `GRM` to kilograms.

Sales units to order:

```
units = ceil(requested_base / base_per_sales_unit)
if allowed_lot_size >= 1: units = ceil(units / allowed_lot_size) * allowed_lot_size
```

Most products have `allowed_lot_size` 0.01; these are ordered in whole sales units unless `variable_unit` is true (A-04, Q-08). Rounding up can over-deliver slightly; the offer states requested and offered quantities.

## 6. Offer building algorithm

For each request line:

1. **Candidates.** Start with `product_ids` in the agent's order. The first has role `requested`, the rest `alternate`. If `substitution` is `allowed`, append (a) official substitutes of the requested product, role `substitute`, source `valio_aimo_substitution_rule`; then (b) up to 10 products in the same category that satisfy `requirements`, role `substitute`, source `same_category_same_claims` (Q-14). Stop at 25 candidates.
2. **Static filter.** Drop candidates that fail `requirements` (`fails_requirements`), or that the mandate would reject under R2, R3 or R4 with effect rejected (`forbidden_by_mandate`). Record each in `excluded[]`; for `forbidden_by_mandate`, include the policy reason object so the agent can explain the refusal.
3. **Upstream facts.** One adapter call for all remaining candidates across all lines.
4. **Dynamic filter.** Drop candidates with `price_unavailable`, `sales_blocked`, `out_of_stock`, `not_deliverable_by_date` or `no_quantity_conversion`.
5. **Capacity.** For Stock products with a known amount (below 999999), capacity in sales units is effective available quantity (document 04, section 4). For a Stock product with an amount of 999999 or more, capacity is unlimited and assurance stays `confirmed` (Q-15). For Terminal or unknown `logisticModel`, capacity is unlimited and assurance is `unconfirmed`.
6. **Order of use.** P0: requested, then alternates in the agent's order, then substitutes by source (official first). P1: by descending score within each role (document 05, section 4).
7. **Fill.** Take from each candidate in order until the requested base quantity is met. With `fulfilment: exact`, only the first surviving candidate is used.
8. **Result.** If nothing can be filled, the request fails with `422 no_candidates`. If partly filled, the offer is issued with `complete: false` and the shortfall is visible as requested versus offered.

`offer.requested` is written in its own transaction before the upstream call, so a failed attempt (503 or 422) still appears in the trace. Then, in one transaction: insert the offer, its lines and holds; write `offer.issued`; sign the payload; return. Validation, unknown-product and mandate errors (400, 403) are raised before `offer.requested` and leave no event. If no candidate survives the static filter, the gateway returns 422 without calling upstream. Holds are read `FOR UPDATE` in the build transaction and compared with the application clock.

`expires_at` = the earliest of issue time plus `OFFER_TTL_SECONDS` and each line's `order_by`. If any `order_by` is already in the past, that candidate is excluded with `not_deliverable_by_date` (Q-11). Every `not_deliverable_by_date` entry in `excluded[]` carries `available_date` and `order_by` as described in document 03, section 5.

Deposits listed in the product file are not added to totals (A-21, Q-12).

## 7. Comparison data

| Field | Source | Rule |
|---|---|---|
| `delivery_reliability` | Reliability CSV | Share of ordered quantity historically delivered. Not a punctuality measure (A-16). Omitted when absent. |
| `carbon_kg_co2e_per_kg` | Carbon CSV | Always accompanied by `carbon_basis: "product_group_average"` (A-17). Omitted when absent. |
| Order carbon estimate (P2) | Carbon value multiplied by net weight | Only when net weight per sales unit is known |

Expected shortfall can be explained to a user as `quantity * (1 - delivery_reliability)`.

## 8. Loader procedure

`scripts/load_data.py --products data/products.json --reliability data/reliability.csv --carbon data/carbon.csv`

1. Stream-parse the product JSON (it is 87 MB; do not hold raw text and parsed objects together if memory is tight).
2. Insert into `products` and the attribute tables in batches of 500, inside a transaction per batch.
3. Update `delivery_reliability` and `carbon_kg_co2e_per_kg` from the CSV files.
4. Print row counts and compare with the expected counts in document 04, section 5.
5. Print the 35 lactose-free milk products as a sanity check.

The loader is idempotent: it truncates the catalog tables first.
