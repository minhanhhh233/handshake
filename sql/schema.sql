-- Handshake schema. Source of truth: handshake-docs/04-data-model.md, section 2.
-- MariaDB 10.6 or later (system-versioned tables). utf8mb4, InnoDB. Timestamps are UTC.

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
