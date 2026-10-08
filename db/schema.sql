CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============ People ============
CREATE TABLE staff_users (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT NOT NULL,
  email           TEXT UNIQUE NOT NULL,
  password_hash   TEXT NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('agent','technician','warehouse','admin')),
  phone           TEXT,
  skills          TEXT[] NOT NULL DEFAULT '{}',   -- technicians: {'battery','ram','display','cmos'}
  city            TEXT,                           -- technicians: the city they cover, e.g. 'Bengaluru' (§7.7)
  is_available    BOOLEAN NOT NULL DEFAULT TRUE,
  avatar_url      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE customers (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  full_name   TEXT,
  email       TEXT UNIQUE,
  phone       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE customer_identities (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id       UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  channel           TEXT NOT NULL CHECK (channel IN ('discord','telegram','email','web')),
  external_user_id  TEXT NOT NULL,
  display_name      TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (channel, external_user_id)
);

CREATE TABLE addresses (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  line1        TEXT NOT NULL,
  line2        TEXT,
  city         TEXT NOT NULL,
  state        TEXT,
  postal_code  TEXT,
  location_url TEXT,                               -- optional Google / Apple Maps link the customer pasted (§7.6)
  is_default   BOOLEAN NOT NULL DEFAULT TRUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Catalog ============
CREATE TABLE product_models (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  model_number     TEXT UNIQUE NOT NULL,          -- shared by many units
  brand            TEXT NOT NULL,
  name             TEXT NOT NULL,
  category         TEXT NOT NULL CHECK (category IN ('laptop','desktop','headphones','accessory')),
  specs            JSONB NOT NULL DEFAULT '{}',   -- cpu, ram_gb, storage, battery_wh, ...
  warranty_months  INT NOT NULL DEFAULT 12,
  image_url        TEXT
);

CREATE TABLE products (                            -- one physical unit
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  serial_number    TEXT UNIQUE NOT NULL,          -- unique per unit
  model_id         UUID NOT NULL REFERENCES product_models(id),
  color            TEXT,
  config           JSONB NOT NULL DEFAULT '{}',   -- as-sold config, e.g. {"ram_gb":16,"storage":"512GB SSD"}
  purchase_date    DATE,
  warranty_until   DATE,
  customer_id      UUID REFERENCES customers(id), -- owner, may be null until registered
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON products (model_id);
CREATE INDEX ON products (customer_id);

CREATE TABLE parts (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sku         TEXT UNIQUE NOT NULL,
  name        TEXT NOT NULL,
  part_type   TEXT NOT NULL CHECK (part_type IN
               ('battery','cmos_battery','ram','ssd','charger','keyboard','display','fan','ear_cushion','cable','other')),
  unit_price  NUMERIC(10,2) NOT NULL,
  specs       JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE part_compatibility (
  part_id   UUID REFERENCES parts(id) ON DELETE CASCADE,
  model_id  UUID REFERENCES product_models(id) ON DELETE CASCADE,
  PRIMARY KEY (part_id, model_id)
);

CREATE TABLE service_catalog (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code            TEXT UNIQUE NOT NULL,           -- BATTERY_REPLACE, RAM_UPGRADE, CMOS_REPLACE, SSD_UPGRADE, OS_REINSTALL
  name            TEXT NOT NULL,
  part_type       TEXT,                           -- null for software-only services
  labour_fee      NUMERIC(10,2) NOT NULL,
  requires_visit  BOOLEAN NOT NULL DEFAULT TRUE,
  required_skill  TEXT
);

-- ============ Inventory ============
CREATE TABLE warehouses (
  id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name  TEXT NOT NULL,
  city  TEXT NOT NULL
);

CREATE TABLE inventory (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  part_id            UUID NOT NULL REFERENCES parts(id),
  warehouse_id       UUID NOT NULL REFERENCES warehouses(id),
  qty_on_hand        INT NOT NULL DEFAULT 0 CHECK (qty_on_hand >= 0),
  qty_reserved       INT NOT NULL DEFAULT 0 CHECK (qty_reserved >= 0),
  reorder_threshold  INT NOT NULL DEFAULT 5,
  reorder_qty        INT NOT NULL DEFAULT 20,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (part_id, warehouse_id)
);

-- ============ Tickets ============
CREATE SEQUENCE ticket_seq START 1;

CREATE TABLE tickets (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_number     TEXT UNIQUE NOT NULL
                    DEFAULT ('SR-' || to_char(now(),'YYYY') || '-' || lpad(nextval('ticket_seq')::text, 5, '0')),
  customer_id       UUID NOT NULL REFERENCES customers(id),
  product_id        UUID REFERENCES products(id),
  source_channel    TEXT NOT NULL CHECK (source_channel IN ('discord','telegram','email','web')),
  category          TEXT NOT NULL DEFAULT 'unknown' CHECK (category IN ('hardware','software','unknown')),
  issue_type        TEXT NOT NULL DEFAULT 'other',
  title             TEXT NOT NULL,
  description       TEXT NOT NULL,
  ai_summary        TEXT,
  status            TEXT NOT NULL DEFAULT 'new' CHECK (status IN
                    ('new','in_progress','awaiting_customer','awaiting_payment','scheduled','resolved','closed')),
  priority          TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('low','medium','high','urgent')),
  duplicate_count   INT NOT NULL DEFAULT 0,
  flags             TEXT[] NOT NULL DEFAULT '{}', -- unverified_product, ownership_mismatch, out_of_warranty
  assigned_agent_id UUID REFERENCES staff_users(id),
  embedding         VECTOR(384),
  search_tsv        TSVECTOR GENERATED ALWAYS AS
                    (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(ai_summary,''))) STORED,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at       TIMESTAMPTZ
);
CREATE INDEX ON tickets (status, priority, updated_at DESC);
CREATE INDEX ON tickets (customer_id, product_id);
CREATE INDEX ON tickets USING GIN (search_tsv);
CREATE INDEX ON tickets USING hnsw (embedding vector_cosine_ops);

CREATE TABLE conversations (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id         UUID NOT NULL REFERENCES customers(id),
  channel             TEXT NOT NULL CHECK (channel IN ('discord','telegram','email','web')),
  external_thread_id  TEXT NOT NULL,
  ticket_id           UUID REFERENCES tickets(id),
  context             JSONB NOT NULL DEFAULT '{}',  -- {"awaiting":"serial_number"} or {"awaiting":"payment_details","collected":{...}}
  last_message_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (channel, external_thread_id)
);

CREATE TABLE messages (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id      UUID REFERENCES conversations(id),
  ticket_id            UUID REFERENCES tickets(id),
  sender_type          TEXT NOT NULL CHECK (sender_type IN ('customer','agent','ai','technician','system')),
  sender_staff_id      UUID REFERENCES staff_users(id),
  channel              TEXT NOT NULL CHECK (channel IN ('discord','telegram','email','web','internal')),
  body                 TEXT NOT NULL,
  body_original        TEXT,                        -- agent's raw note before polish
  is_internal_note     BOOLEAN NOT NULL DEFAULT FALSE,
  attachments          JSONB NOT NULL DEFAULT '[]',
  external_message_id  TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON messages (ticket_id, created_at);
CREATE INDEX ON messages (conversation_id, created_at);

CREATE TABLE ticket_events (                         -- timeline + audit
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id   UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  type        TEXT NOT NULL,                         -- created, status_changed, followup, priority_raised, payment_*, job_*, note
  payload     JSONB NOT NULL DEFAULT '{}',
  actor       TEXT NOT NULL,                         -- 'ai', 'system', 'customer', staff user id
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON ticket_events (ticket_id, created_at);

CREATE TABLE diagnostic_steps (                      -- "what was tried, what worked"
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id     UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  position      INT NOT NULL,
  step          TEXT NOT NULL,
  suggested_by  TEXT NOT NULL CHECK (suggested_by IN ('ai','agent','playbook')),
  result        TEXT NOT NULL DEFAULT 'pending' CHECK (result IN ('pending','worked','failed','skipped')),
  notes         TEXT,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Knowledge ============
CREATE TABLE kb_playbooks (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  issue_type  TEXT NOT NULL,
  category    TEXT NOT NULL,
  model_id    UUID REFERENCES product_models(id),     -- null = applies to all models in category
  title       TEXT NOT NULL,
  steps       JSONB NOT NULL,                          -- [{"step":"...","expected":"...","resolves_if":"..."}]
  embedding   VECTOR(384)
);

-- ============ Staff tools ============
CREATE TABLE slash_commands (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id         UUID REFERENCES staff_users(id) ON DELETE CASCADE,  -- null = built-in / global
  name             TEXT NOT NULL,                      -- without slash, lowercase, [a-z0-9-]
  description      TEXT NOT NULL,
  prompt_template  TEXT NOT NULL,
  allowed_tools    TEXT[] NOT NULL DEFAULT '{}',
  is_builtin       BOOLEAN NOT NULL DEFAULT FALSE,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (owner_id, name)
);

CREATE TABLE notifications (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
  type        TEXT NOT NULL,
  title       TEXT NOT NULL,
  body        TEXT,
  link        TEXT,
  read_at     TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Payments ============
CREATE TABLE payments (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id           UUID NOT NULL REFERENCES tickets(id),
  customer_id         UUID NOT NULL REFERENCES customers(id),
  service_code        TEXT NOT NULL,
  amount              NUMERIC(10,2) NOT NULL,          -- computed in code (§7.6), never taken from a model or a customer
  currency            TEXT NOT NULL,
  line_items          JSONB NOT NULL,                  -- [{"kind":"part","label":"Aurora 14 battery 70Wh (BAT-AX14)","amount":"5.40"},{"kind":"labour",...}]
  status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                      ('pending','verifying','paid','failed','expired','cancelled','refunded')),
  provider            TEXT NOT NULL DEFAULT 'upi_utr',
  provider_ref        TEXT,
  public_token        TEXT UNIQUE NOT NULL,            -- used in /pay/[token]
  invoice_number      TEXT UNIQUE,                     -- INV-YYYY-NNNNN
  utr                 TEXT UNIQUE,                     -- the customer's 12-digit UPI reference (UTR)
  utr_submitted_at    TIMESTAMPTZ,
  utr_attempts        INT NOT NULL DEFAULT 0,
  verified_at         TIMESTAMPTZ,
  verified_by         TEXT,                            -- 'bank_alert', or the id of the staff user who marked it paid
  needs_review        BOOLEAN NOT NULL DEFAULT FALSE,  -- verifying for PAYMENT_VERIFY_TIMEOUT_MINUTES with no bank alert
  service_address_id  UUID REFERENCES addresses(id),
  expires_at          TIMESTAMPTZ NOT NULL,
  paid_at             TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bank_alerts (                            -- forwarded bank credit SMS (§7.6): audit + idempotency, never the SMS text
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  gmail_message_id    TEXT UNIQUE NOT NULL,
  sender              TEXT NOT NULL,
  utr                 TEXT,                            -- null when the alert was rejected before it was parsed
  amount              NUMERIC(10,2),
  raw_sha256          TEXT NOT NULL,                   -- hash of the body, so a copy can be recognised without keeping it
  parsed_ok           BOOLEAN NOT NULL,
  reject_reason       TEXT,
  matched_payment_id  UUID REFERENCES payments(id),    -- the payment this alert was reconciled against
  received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at        TIMESTAMPTZ
);

-- ============ Field service ============
CREATE TABLE service_jobs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id       UUID NOT NULL REFERENCES tickets(id),
  technician_id   UUID NOT NULL REFERENCES staff_users(id),
  address_id      UUID NOT NULL REFERENCES addresses(id),
  service_code    TEXT NOT NULL,
  part_id         UUID REFERENCES parts(id),
  warehouse_id    UUID REFERENCES warehouses(id),
  scheduled_date  DATE NOT NULL,
  status          TEXT NOT NULL DEFAULT 'assigned' CHECK (status IN
                  ('assigned','accepted','en_route','on_site','completed','cancelled')),
  notes           TEXT,
  completed_at    TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON service_jobs (technician_id, scheduled_date);

CREATE TABLE inventory_movements (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  part_id       UUID NOT NULL REFERENCES parts(id),
  warehouse_id  UUID NOT NULL REFERENCES warehouses(id),
  change        INT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('reserve','release','consume','restock','adjust')),
  ticket_id     UUID REFERENCES tickets(id),
  job_id        UUID REFERENCES service_jobs(id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE restock_requests (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  part_id       UUID NOT NULL REFERENCES parts(id),
  warehouse_id  UUID NOT NULL REFERENCES warehouses(id),
  qty           INT NOT NULL,
  reason        TEXT,
  status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','ordered','received','cancelled')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Plumbing ============
CREATE TABLE outbox (                                 -- reliable outbound delivery to channels
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id  UUID REFERENCES conversations(id),      -- null for send_email: no channel conversation
  message_id       UUID REFERENCES messages(id),
  payload          JSONB NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
  attempts         INT NOT NULL DEFAULT 0,
  last_error       TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ai_runs (                                -- every LLM call + tool call, powers Agent Activity panel
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id      UUID REFERENCES tickets(id),
  role           TEXT NOT NULL,                       -- intake, copilot, writer, automation
  trigger        TEXT NOT NULL,                       -- message.received, /payments, payment.paid, ...
  model          TEXT,
  input_tokens   INT,
  output_tokens  INT,
  tool_calls     JSONB NOT NULL DEFAULT '[]',          -- [{"tool":"inventory__reserve_part","ok":true,"ms":42}]
  latency_ms     INT,
  error          TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
