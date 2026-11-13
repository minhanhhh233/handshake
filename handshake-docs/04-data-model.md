# 04. Data model

MariaDB 10.6 or later. Character set `utf8mb4`, engine InnoDB. All timestamps are stored in UTC as `DATETIME(3)` and converted to Europe/Helsinki at the API boundary.

## 1. Table overview

| Group | Table | Purpose | Written by |
|---|---|---|---|
| Catalog | `products` | One row per product from the challenge file | Loader |
| Catalog | `product_claims` | Nutritional claims per product | Loader |
| Catalog | `product_allergens` | Allergen statements with state | Loader |
| Catalog | `product_labels` | Packaging labels (certificates) | Loader |
| Catalog | `product_substitutions` | Valio Aimo's substitution rules | Loader |
| Catalog | `code_lookup` | Hand-made names for category, unit, allergen and label codes | Seed script |
| Parties | `customers` | Buyer organisations | Seed script |
| Parties | `agents` | Agents and their key hashes | Seed script, admin |
| Parties | `mandates` | Mandate documents, system-versioned | Admin |
| Upstream | `price_quotes` | Snapshot of each upstream price answer used | Upstream adapter |
| Upstream | `availability_snapshots` | Snapshot of each upstream availability answer used | Upstream adapter |
| Trading | `offers` | Issued offers with their JWS | Offer builder |
| Trading | `offer_lines` | Lines of each offer | Offer builder |
| Trading | `stock_holds` | Reservations, system-versioned | Offer builder, orders |
| Trading | `orders` | Orders and their outcome | Orders |
| Trading | `approvals` | Approval requests and decisions | Orders |
| Trading | `idempotency_keys` | Stored responses for repeated order requests | Orders |
| Audit | `events` | Append-only, hash-chained event log | Events |

`mandates` and `stock_holds` use `WITH SYSTEM VERSIONING`, so earlier states can be queried with `FOR SYSTEM_TIME AS OF`. This lets the gateway show which mandate version applied to an order and what was held at any moment.

## 2. DDL

```sql
-- ---------- Catalog ----------

CREATE TABLE products (
  product_id            VARCHAR(16)  NOT NULL PRIMARY KEY,
  sales_unit_gtin       VARCHAR(20)  NULL,
  consumer_gtin         VARCHAR(20)  NULL,
  name_en               VARCHAR(255) NULL,
  name_fi               VARCHAR(255) NULL,
  brand                 VARCHAR(128) NULL,
  vendor_name           VARCHAR(128) NOT NULL,
  category_code         VARCHAR(16)  NOT NULL,
  sales_unit            VARCHAR(8)   NOT NULL,
  base_unit             VARCHAR(8)   NOT NULL,
  allowed_lot_size      DECIMAL(10,3) NOT NULL,
  variable_unit         BOOLEAN      NOT NULL DEFAULT FALSE,
  country_of_origin     CHAR(2)      NULL,
  temperature_condition TINYINT      NULL,
  has_allergen_data     BOOLEAN      NOT NULL,
  net_volume_l_per_sales_unit  DECIMAL(12,4) NULL,   -- from unitConversions when derivable
  net_weight_kg_per_sales_unit DECIMAL(12,4) NULL,   -- from unitConversions when derivable
  delivery_reliability  DECIMAL(4,3) NULL,           -- from the reliability CSV
  carbon_kg_co2e_per_kg DECIMAL(8,3) NULL,           -- from the carbon CSV (group average)
  units_json            JSON NOT NULL,               -- packaging hierarchy as given
  nutrition_json        JSON NULL,
  ingredient_origins_json JSON NULL,
  untrusted_text_json   JSON NULL,                   -- marketing, key ingredients, instructions
  FULLTEXT KEY ft_names (name_en, name_fi),
  KEY ix_category (category_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE product_claims (
  product_id VARCHAR(16) NOT NULL,
  claim      VARCHAR(64) NOT NULL,
  PRIMARY KEY (product_id, claim),
  KEY ix_claim (claim, product_id),
  CONSTRAINT fk_claim_product FOREIGN KEY (product_id) REFERENCES products(product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE product_allergens (
  product_id VARCHAR(16) NOT NULL,
  allergen   VARCHAR(8)  NOT NULL,                      -- GS1 code, e.g. AM
  state      ENUM('CONTAINS','MAY_CONTAIN','FREE_FROM') NOT NULL,
  PRIMARY KEY (product_id, allergen),
  KEY ix_allergen (allergen, state, product_id),
  CONSTRAINT fk_allergen_product FOREIGN KEY (product_id) REFERENCES products(product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE product_labels (
  product_id VARCHAR(16) NOT NULL,
  label      VARCHAR(64) NOT NULL,
  PRIMARY KEY (product_id, label),
  KEY ix_label (label, product_id),
  CONSTRAINT fk_label_product FOREIGN KEY (product_id) REFERENCES products(product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE product_substitutions (
  product_id            VARCHAR(16) NOT NULL,
  substitute_product_id VARCHAR(16) NOT NULL,
  PRIMARY KEY (product_id, substitute_product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE code_lookup (
  kind    ENUM('category','unit','allergen','label','claim','temperature') NOT NULL,
  code    VARCHAR(64)  NOT NULL,
  name_en VARCHAR(128) NOT NULL,
  verified BOOLEAN NOT NULL DEFAULT FALSE,   -- FALSE = inferred by the team
  PRIMARY KEY (kind, code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- Parties ----------

CREATE TABLE customers (
  customer_id       VARCHAR(32)  NOT NULL PRIMARY KEY,
  name              VARCHAR(128) NOT NULL,
  upstream_customer VARCHAR(32)  NOT NULL DEFAULT '123',
  created_at        DATETIME(3)  NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE agents (
  agent_id     VARCHAR(32)  NOT NULL PRIMARY KEY,
  customer_id  VARCHAR(32)  NOT NULL,
  name         VARCHAR(128) NOT NULL,
  key_sha256   CHAR(64)     NOT NULL UNIQUE,
  status       ENUM('active','suspended') NOT NULL DEFAULT 'active',
  created_at   DATETIME(3)  NOT NULL,
  CONSTRAINT fk_agent_customer FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE mandates (
  mandate_id  VARCHAR(32) NOT NULL PRIMARY KEY,
  agent_id    VARCHAR(32) NOT NULL UNIQUE,     -- one active mandate per agent
  version     INT         NOT NULL,
  valid_from  DATETIME(3) NOT NULL,
  valid_to    DATETIME(3) NULL,
  document    JSON        NOT NULL,            -- see document 05, section 2
  updated_at  DATETIME(3) NOT NULL,
  CONSTRAINT fk_mandate_agent FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 WITH SYSTEM VERSIONING;

-- ---------- Upstream snapshots ----------

CREATE TABLE price_quotes (
  price_quote_id  BIGINT AUTO_INCREMENT PRIMARY KEY,
  product_id      VARCHAR(16) NOT NULL,
  pricing_date    DATE        NOT NULL,
  fetched_at      DATETIME(3) NOT NULL,
  currency        CHAR(3)     NULL,
  unit_price_excl_tax DECIMAL(12,4) NULL,
  unit_price_incl_tax DECIMAL(12,4) NULL,
  comparison_price    DECIMAL(12,4) NULL,
  comparison_unit     VARCHAR(4)    NULL,       -- L or KG
  tax_rate_percent    DECIMAL(5,2)  NULL,
  valid_from      VARCHAR(40) NULL,             -- stored as received (format unconfirmed, Q-04)
  valid_to        VARCHAR(40) NULL,
  net_price_found BOOLEAN     NOT NULL,
  error           BOOLEAN     NOT NULL,
  raw_json        JSON        NOT NULL,
  KEY ix_pq (product_id, pricing_date, fetched_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE availability_snapshots (
  availability_snapshot_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  product_id      VARCHAR(16) NOT NULL,
  requested_date  DATE        NOT NULL,
  fetched_at      DATETIME(3) NOT NULL,
  upstream_time_now VARCHAR(40) NULL,
  closing_time    VARCHAR(40) NULL,             -- stored as received (format unconfirmed, Q-04)
  sales_block     BOOLEAN     NOT NULL,
  logistic_model  VARCHAR(16) NULL,             -- as received; normalised in code
  available_amount DECIMAL(14,3) NULL,          -- in sales units; 999999 = unknown (Terminal)
  available_date  VARCHAR(40) NULL,
  sales_unit      VARCHAR(8)  NULL,
  raw_json        JSON        NOT NULL,
  KEY ix_as (product_id, requested_date, fetched_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- Trading ----------

CREATE TABLE offers (
  offer_id      VARCHAR(32) NOT NULL PRIMARY KEY,
  trace_id      VARCHAR(32) NOT NULL,
  agent_id      VARCHAR(32) NOT NULL,
  customer_id   VARCHAR(32) NOT NULL,
  mandate_id    VARCHAR(32) NOT NULL,
  mandate_version INT       NOT NULL,
  status        ENUM('active','consumed','expired') NOT NULL DEFAULT 'active',
  option_label  ENUM('best','better','good') NOT NULL DEFAULT 'best',
  delivery_date DATE        NOT NULL,
  currency      CHAR(3)     NOT NULL,
  total_excl_tax DECIMAL(14,2) NOT NULL,
  total_tax      DECIMAL(14,2) NOT NULL,
  total_incl_tax DECIMAL(14,2) NOT NULL,
  issued_at     DATETIME(3) NOT NULL,
  expires_at    DATETIME(3) NOT NULL,
  request_json  JSON        NOT NULL,           -- the agent's offer request
  payload_json  JSON        NOT NULL,           -- the signed offer payload
  jws           MEDIUMTEXT  NOT NULL,
  KEY ix_offer_agent (agent_id, issued_at),
  KEY ix_offer_trace (trace_id),
  CONSTRAINT fk_offer_agent FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE offer_lines (
  offer_id      VARCHAR(32) NOT NULL,
  line_no       INT         NOT NULL,
  request_line  INT         NOT NULL,
  product_id    VARCHAR(16) NOT NULL,
  role          ENUM('requested','alternate','substitute') NOT NULL,
  substitute_for VARCHAR(16) NULL,
  substitution_source ENUM('valio_aimo_substitution_rule','same_category_same_claims') NULL,
  quantity_sales_units DECIMAL(14,3) NOT NULL,
  sales_unit    VARCHAR(8)  NOT NULL,
  base_quantity DECIMAL(14,3) NULL,
  base_unit     VARCHAR(4)  NULL,               -- L or KG
  unit_price_excl_tax DECIMAL(12,4) NOT NULL,
  tax_rate_percent    DECIMAL(5,2)  NOT NULL,
  line_total_excl_tax DECIMAL(14,2) NOT NULL,
  line_total_incl_tax DECIMAL(14,2) NOT NULL,
  logistic_model ENUM('stock','terminal','unknown') NOT NULL,
  assurance     ENUM('confirmed','unconfirmed') NOT NULL,
  price_quote_id BIGINT NOT NULL,
  availability_snapshot_id BIGINT NOT NULL,
  PRIMARY KEY (offer_id, line_no),
  CONSTRAINT fk_line_offer FOREIGN KEY (offer_id) REFERENCES offers(offer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE stock_holds (
  hold_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
  offer_id      VARCHAR(32) NOT NULL,
  line_no       INT         NOT NULL,
  product_id    VARCHAR(16) NOT NULL,
  delivery_date DATE        NOT NULL,
  quantity_sales_units DECIMAL(14,3) NOT NULL,
  status        ENUM('held','committed','released') NOT NULL DEFAULT 'held',
  expires_at    DATETIME(3) NULL,               -- NULL once committed
  KEY ix_hold_product (product_id, delivery_date, status),
  KEY ix_hold_offer (offer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 WITH SYSTEM VERSIONING;

CREATE TABLE orders (
  order_id      VARCHAR(32) NOT NULL PRIMARY KEY,
  offer_id      VARCHAR(32) NOT NULL UNIQUE,
  trace_id      VARCHAR(32) NOT NULL,
  agent_id      VARCHAR(32) NOT NULL,
  customer_id   VARCHAR(32) NOT NULL,
  mandate_id    VARCHAR(32) NOT NULL,
  mandate_version INT       NOT NULL,
  status        ENUM('confirmed','pending_approval','declined','expired','rejected') NOT NULL,
  outcome       ENUM('accepted','needs_approval','rejected') NOT NULL,
  reasons_json  JSON        NOT NULL,
  reference     VARCHAR(64) NULL,
  note_untrusted VARCHAR(500) NULL,
  receipt_token CHAR(43)    NOT NULL,
  created_at    DATETIME(3) NOT NULL,
  updated_at    DATETIME(3) NOT NULL,
  KEY ix_order_agent (agent_id, created_at),
  KEY ix_order_trace (trace_id),
  CONSTRAINT fk_order_offer FOREIGN KEY (offer_id) REFERENCES offers(offer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE approvals (
  approval_id   VARCHAR(32) NOT NULL PRIMARY KEY,
  order_id      VARCHAR(32) NOT NULL UNIQUE,
  token_sha256  CHAR(64)    NOT NULL UNIQUE,
  approver_name VARCHAR(128) NOT NULL,
  approver_contact VARCHAR(255) NULL,
  status        ENUM('pending','approved','declined','expired') NOT NULL DEFAULT 'pending',
  comment_untrusted VARCHAR(500) NULL,
  requested_at  DATETIME(3) NOT NULL,
  expires_at    DATETIME(3) NOT NULL,
  decided_at    DATETIME(3) NULL,
  CONSTRAINT fk_approval_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE idempotency_keys (
  agent_id      VARCHAR(32) NOT NULL,
  idem_key      VARCHAR(64) NOT NULL,
  request_sha256 CHAR(64)   NOT NULL,
  response_status INT       NOT NULL,
  response_json JSON        NOT NULL,
  created_at    DATETIME(3) NOT NULL,
  PRIMARY KEY (agent_id, idem_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- Audit ----------

CREATE TABLE events (
  event_id   VARCHAR(32) NOT NULL PRIMARY KEY,
  trace_id   VARCHAR(32) NOT NULL,
  seq        INT         NOT NULL,              -- 1-based position within the trace
  type       VARCHAR(40) NOT NULL,
  at         DATETIME(3) NOT NULL,
  actor_type ENUM('agent','approver','gateway','admin') NOT NULL,
  actor_id   VARCHAR(64) NULL,
  body_json  JSON        NOT NULL,
  text       VARCHAR(500) NOT NULL,             -- plain-language line for the timeline
  audience   ENUM('both','aimo_only') NOT NULL DEFAULT 'both',
  prev_hash  CHAR(64)    NOT NULL,              -- 64 zeros for seq 1
  hash       CHAR(64)    NOT NULL,
  UNIQUE KEY ux_trace_seq (trace_id, seq),
  KEY ix_event_at (at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

## 3. Rules the schema does not enforce

- An offer, its lines and its holds are inserted in one transaction.
- `events` rows are never updated or deleted, except by the demo reset.
- Appending an event locks the trace (`SELECT MAX(seq) ... FOR UPDATE` on the trace's rows, or an advisory lock named by `trace_id`) so that `seq` and `prev_hash` are assigned without gaps.
- `mandates.version` increases by one on each replacement.
- `orders.offer_id` is unique: one offer yields at most one order.
- Agent keys, approval tokens and the admin key are never stored in clear text. `receipt_token` is stored in clear because it only grants read access to one receipt.
- The approval token is not random: it is base64url (no padding) of HMAC-SHA256 keyed with `ADMIN_KEY` over the `approval_id` (43 characters). Only `token_sha256` is stored, yet the admin console can rebuild the approval link (`GET /v1/admin/orders/{order_id}/approval-link`). Rotating `ADMIN_KEY` invalidates every outstanding approval link.

## 4. Available quantity

For a Stock product and delivery date:

```
effective_available = upstream.available_amount
                      - SUM(stock_holds.quantity_sales_units
                            WHERE product_id = ? AND delivery_date = ?
                              AND (status = 'committed'
                                   OR (status = 'held' AND expires_at > NOW())))
```

For a Terminal product the upstream amount is unknown, so no subtraction is done and the line is marked `unconfirmed`. The same applies to any product whose upstream amount is 999999 or more, except that a Stock product keeps assurance `confirmed` (Q-15). Holds are still recorded for audit.

Committed holds are subtracted because the challenge has no upstream order API; upstream never learns about Handshake orders (A-01, A-18).

## 5. Loader mapping

| Target | Source |
|---|---|
| `products.product_id` | `id` |
| `sales_unit_gtin` | `salesUnitGtin` |
| `consumer_gtin` | `synkkaData.gtin` |
| `name_en`, `name_fi` | `synkkaData.names[]` by `language` |
| `brand` | `synkkaData.brand` |
| `vendor_name`, `category_code`, `sales_unit`, `base_unit`, `allowed_lot_size`, `country_of_origin`, `temperature_condition` | Same-named top-level fields (`category`, `salesUnit`, `baseUnit`, `allowedLotSize`, `countryOfOrigin`, `temperatureCondition`) |
| `variable_unit` | `synkkaData.variableUnit` |
| `product_claims` | `synkkaData.classifications[name = nutritionalClaim].values[].id` (the `classifications` array sits under `synkkaData`, as in the next three rows) |
| `product_allergens` | `classifications[name = allergen].values[]` with `unit` as state (`CONTAINS`, `MAY_CONTAIN`); `classifications[name = nonAllergen].values[]` as `FREE_FROM` |
| `has_allergen_data` | TRUE when either group has at least one value |
| `product_labels` | `classifications[name = packagingMarkedLabel].values[].id`, excluding `warmTemperature`, `coolTemperature`, `freezeTemperature` |
| `product_substitutions` | `substitutions[].substituteProduct` (each entry is `{ "product", "substituteProduct" }`) |
| `net_volume_l_per_sales_unit`, `net_weight_kg_per_sales_unit` | Derived from `synkkaData.unitConversions` (level in `synkkaPackagingType`; volume in `LTR` or `MLT`; weight in `GRM`) and top-level `units[]` (`unitId`, `sizeInBaseUnits`); NULL when not derivable (see document 06, section 5) |
| `nutrition_json`, `ingredient_origins_json` | `synkkaData.nutritionalContent` and `synkkaData.ingredients`, stored as received |
| `delivery_reliability` | Reliability CSV, column `deliveryReliability` |
| `carbon_kg_co2e_per_kg` | Carbon CSV, column `AVG kg CO2-eq/kg` |
| `untrusted_text_json` | `synkkaData.marketingTexts`, `keyIngredients`, `preparationInstructions`, `usageInstructions` and `storageInstructions`, each a list of `{ "value", "language" }` |

If one product has both an allergen and a non-allergen statement for the same code, the stricter state wins: `CONTAINS` over `MAY_CONTAIN` over `FREE_FROM`.

Expected row counts after loading (all five confirmed on 2026-11-14): 17,546 products; 250 substitution rules; 4,425 products with reliability; 11,033 with a carbon value; 9,436 products with `has_allergen_data = TRUE`.
