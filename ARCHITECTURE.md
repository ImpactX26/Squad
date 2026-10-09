# Companion for Company — Architecture (working name)

AI-assisted, omnichannel after-sales service platform for a laptop / PC / headphones company, powered by multiple custom MCP servers.

Customers reach support from **Discord, Telegram, Email, or the website chat**. The AI "brain" understands each message, identifies the exact product by serial number, raises or updates a ticket, and always replies on the channel the customer used. Service-center agents work from one dashboard where AI helps them diagnose, reply, search, and run automations with slash commands. Payment, technician dispatch, inventory, and restock alerts run automatically through MCP tools.

> **Status of this doc:** v1. Payments (§7.6) are UPI QR + UTR, verified against the bank's forwarded credit SMS. Everything is buildable as written.

---

## 0. How to use this document

- Keep this file at the repo root as `ARCHITECTURE.md`. Add a short `CLAUDE.md` that says: "Read ARCHITECTURE.md before writing code. Follow its schema, names, and folder structure exactly." Every Claude session then starts with the same context.
- Names in this doc (tables, tools, events, env vars, routes) are the **contract**. If someone changes one, they update this file in the same commit.
- Sections: 1 scope → 2 decisions → 3 system → 4 brain → 5 MCP servers → 6 channels → 7 flows → 8 database → 9 events → 10 API → 11 frontend → 12 repo → 13 env & keys → 14 build plan → 15 demo safety → 16 open items.

---

## 1. Scope

**In scope (24h build)**

- 4 inbound channels: Discord bot, Telegram bot, Email (Gmail), website chat widget.
- AI intake: understands the issue, classifies hardware vs software, asks for the serial number when missing, creates a ticket.
- Duplicate detection: a repeat complaint about the same problem bumps the existing ticket's priority instead of creating a new ticket.
- Replies always go back on the same channel the customer used.
- Service-provider (SP) dashboard: Apple-style ticket inbox, unified cross-channel timeline, AI summary, suggested diagnostics ("what worked / what didn't"), AI-polished replies, natural-language search, suggested action buttons, built-in and custom slash commands, copilot chat.
- `/payments` automation: collect details → payment link → payment confirmed → technician dispatched → customer notified in chat and by email.
- Technician portal: assigned jobs, address, part to replace, status updates.
- Inventory: stock check, reserve on payment, consume on job completion, automatic low-stock restock request and email to the company.

**Out of scope for the hackathon (roadmap)**

- X (Twitter) and Instagram DMs. Their APIs need app review or paid tiers. The channel adapter pattern (§6) means each one is a single new adapter file later.
- A payment gateway (payments are direct UPI, verified from the bank's SMS, §7.6), real maps routing, SLA engine, multi-tenant companies.

---

## 2. Key decisions

| Area | Decision | Why |
|---|---|---|
| Backend | **Python 3.12 + FastAPI** (async), Pydantic v2 | Official MCP Python SDK, OpenAI SDK, discord.py, python-telegram-bot all native. Team knows Python. |
| Frontend | **Next.js (latest stable, App Router) + TypeScript + Tailwind v4 + shadcn/ui** | Fast to build a premium UI, strong typing. |
| Type safety across the stack | FastAPI OpenAPI → `openapi-typescript` generates frontend types | Backend and frontend can't silently drift. Prevents broken integrations between teammates. |
| Database | **PostgreSQL 16 + pgvector** (Supabase free project, or local Docker as fallback) | Relational data + vector search + full-text search in one DB. Supabase means no Docker on the 8GB laptop and one shared DB for the team. |
| ORM / access | SQLAlchemy 2.0 async + asyncpg; schema in `db/schema.sql` | Plain SQL schema is quickest to review and reset. |
| LLM | **Free tiers only.** **Groq** (OpenAI-compatible) for text and tool loops: `MODEL_FAST` (`qwen/qwen3.8-27b`) for fast/frequent calls, `MODEL_SMART` (`openai/gpt-oss-20b`) for tool loops. **Jev** (TypeSafe's decision model, through a free gateway) for typed decisions, optional and off by default. **Ollama** (`qwen2.5:7b`) on the 16GB laptop as the fallback. See §4.6. | No API budget, so nothing calls a paid API. Groq is fast enough for live demo replies; Ollama keeps replies flowing when Groq rate-limits. |
| Embeddings | **fastembed** with `BAAI/bge-small-en-v1.5` (384-dim, local ONNX, CPU) | Free, fast, no extra key, ~130MB download. |
| MCP | 7 custom MCP servers built with the official Python SDK (`MCPServer`, the class formerly called FastMCP), **Streamable HTTP** on localhost ports 8101–8107 | Each server is independent, testable with MCP Inspector, and can also be plugged into Claude Desktop. |
| MCP client | The backend is the MCP client and runs the tool-use loop itself | Hosted providers can't reach localhost MCP servers. Running the client in the backend keeps everything local. |
| Realtime | FastAPI WebSockets (`/ws/staff`, `/ws/chat/{session}`) + in-process event bus | Simple and enough for one demo host. |
| Auth | JWT (PyJWT) + bcrypt, seeded staff users with roles | No third-party auth setup. |
| Email | Gmail IMAP (poll) + SMTP with an app password | Works from localhost, no OAuth consent screen. |
| Payment | Custom **payments MCP** + **UPI QR and UTR**: the customer pays the company's UPI ID, submits the 12-digit UTR, and the backend matches it against the bank's credit SMS, forwarded by a phone into the project Gmail (§7.6) | No gateway, fees, or merchant onboarding; the method comes from the team's open-source UPI gateway, rebuilt on our backend, MCP tools, and Postgres. |
| Package managers | `uv` (Python), `pnpm` (Node) | Fast, reproducible installs. |

---

## 3. System architecture

```mermaid
flowchart LR
  subgraph Customers
    D[Discord]
    T[Telegram]
    E[Email / Gmail]
    W[Website chat widget]
  end

  subgraph Backend["FastAPI backend :8000"]
    CA[Channel adapters<br/>discord · telegram · email · web]
    IN[Intake pipeline<br/>Jev + fast model]
    AG[Agent runtime<br/>smart-model tool loop]
    WF[Workflow engine<br/>event-driven automations]
    UV[UPI verifier<br/>bank alerts → payment.paid]
    MC[MCP client hub]
    EB[(Event bus)]
    API[REST + WebSocket API]
  end

  subgraph MCP["Custom MCP servers :8101–8107"]
    M1[tickets]
    M2[catalog]
    M3[knowledge]
    M4[messaging]
    M5[payments]
    M6[dispatch]
    M7[inventory]
  end

  DB[(PostgreSQL + pgvector)]
  LLM[[Groq · Jev · Ollama fallback]]
  FE[Next.js :3000<br/>SP dashboard · technician portal · checkout · public site]

  D & T & E & W --> CA --> IN
  IN --> MC
  AG --> MC
  WF --> MC
  IN & AG --> LLM
  MC --> M1 & M2 & M3 & M4 & M5 & M6 & M7
  M1 & M2 & M3 & M5 & M6 & M7 --> DB
  M4 --> CA
  EB --> WF
  E -. forwarded bank SMS .-> UV
  UV --> DB
  UV --> EB
  API <--> FE
  EB --> API
```

### Processes on the demo machine

| Process | Port | Command | RAM (approx.) |
|---|---|---|---|
| Next.js dev server | 3000 | `pnpm dev` | 400–700 MB |
| FastAPI + channel bots + event bus | 8000 | `uv run uvicorn app.main:app --port 8000` (single worker) | 250–400 MB (incl. embedding model) |
| MCP servers (7) | 8101–8107 | `uv run python -m mcp_servers.run_all` | 7 × ~50 MB |
| PostgreSQL | 5432 | Supabase (remote) or Docker `pgvector/pgvector:pg16` | 0 or ~150 MB |

Fits comfortably on the 16GB machine. The 8GB machine can run everything with Supabase instead of Docker.

**Rule:** only one machine (the "demo host") runs the Discord and Telegram bots at a time. Two machines with the same bot token both answer on Discord and conflict on Telegram (HTTP 409). Other teammates set `ENABLE_DISCORD=false`, `ENABLE_TELEGRAM=false`, `ENABLE_EMAIL=false`.

---

## 4. The brain

The brain is four roles sharing one MCP client hub. Each role has its own system prompt, model, and **tool allowlist**.

| Role | Trigger | Model | Allowed MCP tools | Output |
|---|---|---|---|---|
| **Intake** (customer-facing) | Every inbound customer message | `extract_serial` (regex) + `decide.classify_intake` (Jev or `MODEL_FAST`); `MODEL_FAST` for extraction and reply text (§4.4) | catalog (read), tickets (create / find_similar / add_followup / add_message, and `record_diagnostic_results` for the customer's own answer to `/diagnose-send`, validated in code first, §7.1), knowledge (read), messaging.send_reply | Ticket created or updated, reply to customer |
| **Copilot** (staff-facing) | Dashboard chat, slash commands, suggested buttons | `MODEL_SMART` tool loop | All tools, narrowed by the router (§4.3) or by the slash command's `allowed_tools` | Actions + a short answer to the agent |
| **Writer** | Agent clicks "Polish" or sends a reply | `MODEL_FAST` | none | Professional rewrite that keeps the meaning |
| **Automation** | Events (payment.paid, job.completed, stock.low) | Deterministic workflows that call MCP tools; `MODEL_FAST` only writes the customer message text | payments, dispatch, inventory, tickets, messaging | Chained actions with no human click |

### 4.1 Why intake is a guided pipeline, not a free agent

Customer text is untrusted. A free agent with every tool could be talked into "refund me" or "send a technician now". So intake is a state machine that uses the LLM for understanding and wording, and only ever calls a small safe tool set. Payment, dispatch, and inventory tools are **never** reachable from a customer message. Only staff (slash commands, copilot) and the automation workflows can call them.

The intake allowlist is otherwise read-only on `catalog`, with one exception: `catalog.link_product_to_customer`, which registers ownership on first contact (§6.3). It is a **fixed pipeline step, never a tool the model can choose** — it runs only when `lookup_serial` reports the unit has no owner, and a unit registered to someone else is left alone and the ticket flagged `ownership_mismatch`. Intake's allowlist lives in `app/brain/intake.py` as `INTAKE_TOOLS`, a frozenset checked before the hub is called and intersected with `ROLE_SERVERS["intake"]`.

`messaging.send_email` is **not** in `INTAKE_TOOLS`. The one email intake sends is the "ticket raised" confirmation (§7.1), through `IntakeTools.send_ticket_confirmation`, which fixes the template (`ticket_created`), the subject and the data (ticket number, the catalog's device name and serial, the channel) in code. The customer's text decides only the address, and no word they wrote goes into the mail, because the address was typed in a chat. One per new ticket.

### 4.2 Why post-payment automation is a deterministic workflow

"Payment confirmed → reserve part → assign technician → notify technician → notify customer (chat + email) → update dashboard" must work every single time in the demo. It runs as a fixed chain of MCP tool calls triggered by the `payment.paid` event: the receipt (§7.6 step 6), then the booking (§7.7): part reserved, technician found and booked, both of them emailed, the customer told on their channel. A free warranty repair runs the same booking as soon as the customer's details are in, and the job's status changes (`job.status_changed`, `job.completed`) run the completion and cancellation chains (§7.8). Nothing is manual, it's still fully MCP-driven, and it can't be derailed by a model choosing differently. Every call is logged to `ai_runs` and shown live in the dashboard's Agent Activity panel.

### 4.3 Tool-use loop (Copilot and slash commands)

1. At startup, `mcp_hub.py` connects to all 7 servers, calls `list_tools`, and registers tools as `<server>__<tool>` (e.g. `tickets__create_ticket`) in OpenAI function-tool format: `{"type": "function", "function": {"name", "description", "parameters"}}`.
2. For a request, `router.pick_servers(text, role)` picks the servers it needs; slash commands skip this and use their `allowed_tools`. `router.filter_tools` keeps only those servers' tools (always inside the role's allowlist, and never a tool in `MODEL_FORBIDDEN_TOOLS`, §5.5) and `router.compact_tools` trims them. Build the system message (role prompt + ticket context).
3. `runtime.run_tool_loop` calls `llm.complete(tier="smart", tools=...)`. While the reply has `tool_calls` (`finish_reason == "tool_calls"`): execute every call through the MCP hub (concurrently when the model returns several), append `{"role": "tool", "tool_call_id": ..., "content": <compact JSON>}`, and call again. Cap at `AI_MAX_TOOL_ITERATIONS` (default 6). A tool name that isn't in `tools` is rejected, never executed.
4. Publish `agent.tool_called` for each tool call so the dashboard animates it live, and write one `ai_runs` row per run: `model = "<provider>:<model>"`, tokens, `tool_calls` (`[{tool, ok, ms}]`), latency, error. Logging is fire-and-forget; a DB error never fails the request.
5. Stream the final text to the dashboard (SSE, `llm.stream_text`) for a fast feel. The copilot (`app/api/copilot.py`) streams each tool call as it happens and then the loop's own final answer; `llm.stream_text` runs only when the loop stopped at `AI_MAX_TOOL_ITERATIONS` without one, so a question costs no second model call just to be streamed. General questions have read-only aggregate tools: `tickets.count_tickets` ("how many tickets…", in total, open, by status / issue / channel), `inventory.list_stock` (the whole warehouse, a part type, what is running low) and `dispatch.list_technicians`. A list of records is answered as a Markdown table that names people and devices (customer, model and serial), never a bare id, and open and resolved tickets go in separate tables by each row's own `open` value; Any Questions renders it (§11.1). The copilot is never silent: an answer that comes back empty twice ends with a short fallback answer, and `LLMUnavailable` sends the §15-style fallback text as a `delta` before the `error` event. When the router picks `inventory` it also offers `catalog`, because a stock question often names a device or model, whose parts the catalog lists (a part's SKU goes straight to `inventory.check_stock`).

### 4.4 Structured extraction (intake)

Intake builds the record below from `decide.extract_serial` (regex; the model number is the part before `-`) and `decide.classify_intake` (intent, category, issue_type, urgency). It adds one `llm.complete_json` call on `MODEL_FAST` (JSON mode, validated by Pydantic, one repair retry) only when the intent is `new_issue` or `payment_details`, or when the decision was unavailable or low-confidence (below `DECISION_MIN_CONFIDENCE`, which counts as `other` / `unknown`). When `DECISION_PROVIDER=llm`, classification and extraction happen in a single `complete_json` call. JSON mode is used rather than a forced tool call because Ollama's OpenAI endpoint has no `tool_choice`.

```json
{
  "intent": "new_issue | follow_up | provide_info | payment_details | smalltalk | other",
  "category": "hardware | software | unknown",
  "issue_type": "battery | charging | display | keyboard | audio | overheating | boot | os | driver | performance | connectivity | other",
  "summary": "one line",
  "symptoms": ["..."],
  "serial_number": "string or null",
  "model_number": "string or null",
  "urgency": "low | medium | high",
  "language": "en | hi | ...",
  "extracted_fields": { "email": null, "full_name": null, "phone": null, "address": null }
}
```

### 4.5 Speed and free-tier limits

Groq's free tier allows about 30 requests and 6–8K tokens per minute per organization (and 1K requests per day on `openai/gpt-oss-20b`). Everything here is about staying inside that.

- `MODEL_FAST` handles everything the customer waits on (intake, replies, polish, suggestions). `MODEL_SMART` handles only staff-triggered tool loops. Typed decisions (classify, yes/no, pick one) go through `decide`, so they cost one short call or none (Jev).
- **Short prompts and capped output.** Keep system prompts to a few lines. Every call sets `max_tokens` (`LLM_MAX_TOKENS_FAST`, `LLM_MAX_TOKENS_SMART`). `gpt-oss` reasoning tokens count against it, so `GROQ_REASONING_EFFORT=low`.
- **Router.** Each request gets only the servers it needs (§4.3), with descriptions cut to one sentence; all 7 servers' schemas can use up a minute's token budget in one call. Tool results sent back to the model are capped at 4,000 characters.
- **Streaming.** Copilot and command answers stream (SSE), so the agent sees text as soon as the first token arrives.
- **Cache.** `decide` caches answers for 10 minutes (512 entries); suggestions are cached per ticket until the next message.
- **Concurrency.** At most 4 provider calls run at once (one semaphore in `llm.py`), so bursts don't trip the per-minute limit.
- **Loopback by IP, never by name.** `MCP_*_URL` and `BACKEND_URL` use `127.0.0.1`. On an IPv4-only network (the same reason §8 needs the Supabase Session pooler) `localhost` resolves to `::1` first, and the client's happy-eyeballs waits 250 ms per connection attempt before trying IPv4. Each MCP tool call opens several connections, so one call measured 1,290 ms by name against 219 ms by IP — far more than any model call costs. The servers bind to loopback whatever the URL says (`mcp_servers/__init__.py`), so only the client URLs matter.
- Show a typing indicator on Discord/Telegram and the web widget while the brain works.
- Put model names in env (`MODEL_FAST`, `MODEL_SMART`, `OLLAMA_MODEL`) so they can be swapped without code changes.
- Check the real limits with `make llm-check` (it prints Groq's `x-ratelimit-*` headers). `ai_runs.model` shows which provider served each call.

### 4.6 Providers and fallback

Three modules in `app/brain/` are the only code that talks to a model. Everything else calls them.

| Module | Job |
|---|---|
| `llm.py` | The only module that talks to a text model. One `AsyncOpenAI` client per provider (Groq, Ollama), SDK retries off, 2 s connect timeout, read timeout `AI_TIMEOUT_SECONDS` (Groq) / `OLLAMA_TIMEOUT_SECONDS` (Ollama). `complete(messages, tier, max_tokens, tools)`, `complete_json(messages, schema, tier)`, `stream_text(messages, tier)`. `tier="fast"\|"smart"` picks `MODEL_FAST`/`MODEL_SMART` and `LLM_MAX_TOKENS_*`; Ollama always uses `OLLAMA_MODEL`. |
| `decide.py` | Typed decisions: `decide(state, questions)` with `choice` and `noul` (yes/no) questions. `DECISION_PROVIDER=jev` sends one request to Jev; `llm` (the default) answers every question in one `complete_json` on `MODEL_FAST`. Presets: `classify_intake`, `is_duplicate`, and `extract_serial` (a regex, no model). |
| `router.py` | `ROLE_SERVERS`, `pick_servers(text, role)`, `filter_tools`, `compact_tools` (§4.3). |

**Fallback order (text and tool loops).**

1. `LLM_PROVIDER` (Groq). If `GROQ_API_KEY` is empty, it is skipped.
2. On a 429 whose `retry-after` is 2 s or less: wait, and retry Groq once.
3. On any other 429, a timeout, a connection error, or a 5xx: call `LLM_FALLBACK_PROVIDER` (Ollama) once. Other 4xx errors (bad request, bad key) don't fall back.
4. Nothing left: raise `LLMUnavailable`. Customer-path callers catch it and send the friendly fallback reply from §15, so a customer never gets silence. Staff-facing callers show an error.

`stream_text` falls back only when the error comes before the first token. When Groq rejects a tool call because its arguments don't match the tool's schema (HTTP 400, `tool_use_failed`: e.g. a number sent as a word), `complete` repairs it once, as `complete_json` does: the same call again with the rejection added as a last user message; a second rejection is `LLMUnavailable`. When a `gpt-oss` reply comes back empty with `finish_reason == "length"` (reasoning used up `max_tokens`), the call is retried once with double the `max_tokens`.

**Reasoning controls.** Groq takes `reasoning_effort` per model family and rejects a value the family doesn't know, so there is one setting each: `GROQ_REASONING_EFFORT` (`low|medium|high`) for `openai/gpt-oss-*`, and `GROQ_QWEN_REASONING_EFFORT` for `qwen/qwen3*`, which also accepts `none` — no reasoning tokens at all. `llm.py` picks the right one from the model id; everything else is untouched.

**Decisions.** With `DECISION_PROVIDER=jev`, any Jev error, timeout, 429, or unparseable body sends that call down the `llm` path. Jev is a third-party gateway, so `decide` runs `redact()` first: emails, phone numbers, and street-address lines are masked; serials and the issue text stay. Answers below `DECISION_MIN_CONFIDENCE` count as `unknown` / `other`.

**Decisions never authorize anything.** Payment, dispatch, refunds, and stock changes happen only from staff actions and from `payment.paid`, whose only automatic source is a verified bank-alert match (§7.6) (§4.1, §4.2). A decision may classify, route, or flag; code decides what happens next. Likewise the router intersects every answer with `ROLE_SERVERS[role]` in code, after the model has answered, so customer text can never reach payments, dispatch, or inventory.

---

## 5. MCP servers and tools

All servers live in `backend/mcp_servers/`, share `db.py` (async connection pool), and return compact JSON. Each is run standalone on its own port and tested with MCP Inspector before being wired into the brain.

### 5.1 `tickets` (:8101)

| Tool | Purpose |
|---|---|
| `create_ticket(customer_id, product_id, category, issue_type, title, description, source_channel, conversation_id, flags, priority)` | Creates ticket, embedding, `ticket.created` event. Returns `ticket_number`. `flags` and `priority` are optional and validated against the §8.1 vocabularies (unknown values are rejected, nothing is created); intake uses them for `unverified_product` / `ownership_mismatch` (§7.1, §6.3) and for the §4.4 urgency. |
| `find_similar_tickets(customer_id, product_id, text, limit=3)` | Open tickets for same customer/product ranked by cosine similarity. |
| `add_followup(ticket_id, message_id, channel)` | Duplicate handling: link message, `duplicate_count += 1`, raise priority, `ticket.followup` event. |
| `add_message(ticket_id, conversation_id, sender_type, body, body_original?)` | Append to timeline. |
| `update_status(ticket_id, status, note?, customer_told?)` | Status change + `ticket.updated` (with `status`, `note`, `customer_told`). `customer_told` (default false): the caller already sent the customer its own closing chat message (`/close`, a completed job), so the resolved notification (§7.9) sends only the email. |
| `get_ticket(ticket_id or ticket_number)` | Ticket + product + customer + recent timeline. |
| `search_tickets(query, filters)` | Hybrid search: filters + full-text + vector. A status a model writes that isn't in §8.1 is never matched as zero tickets: `"open"` means `open_only`, and any other unknown status is dropped and said back (`ignored_values`), as unknown filter keys are (`ignored_filters`). A query containing a serial (`decide.extract_serial`) returns that device's tickets, newest first (`ranking: "serial"`; an unknown serial is no tickets). Each result row is brief, so a list fits one tool result (§4.5): number, title, status, `open` (true unless resolved or closed), priority, issue type, channel, dates, `customer_name`, `device` (model name) and `serial_number`, plus the ranking fields; the description, AI summary and internal ids stay in `get_ticket`. |
| `count_tickets(filters?, group_by?)` | Read-only counts for the copilot: `total`, `open`, `resolved_or_closed` for the same filters as `search_tickets`, and with `group_by` (`status`, `priority`, `issue_type`, `category` or `source_channel`, a fixed list; anything else is `bad_group_by`) a `groups` list where every row has its `count` and its `open` count. |
| `record_diagnostic(ticket_id, step, result, notes)` | "What worked / what didn't": one step's outcome, `suggested_by='agent'`. |
| `record_diagnostic_results(ticket_id, results)` | The customer's answer to `/diagnose-send` (§7.1 `diagnostic_feedback`): `results` is `[{step_id, result (worked\|failed\|skipped), notes}]`, several steps at once; a step that isn't this ticket's or a result outside the three is ignored (`ignored`). `suggested_by` is unchanged. One `diagnostic_results_recorded` timeline event (actor `customer`); a ticket `awaiting_customer` goes back to `in_progress`; `ticket.updated`, so the open checklist ticks itself. |
| `set_diagnostic_plan(ticket_id, steps, suggested_by='ai')` | The whole opening plan in one call, in order (§7.1). Steps already on the ticket are skipped, so re-running intake can't duplicate it. |
| `update_summary(ticket_id, summary)` | AI summary refresh. |

### 5.2 `catalog` (:8102)

| Tool | Purpose |
|---|---|
| `lookup_serial(serial_number)` | Unit + model + color + warranty status + owner. |
| `lookup_model(model_number)` | Model specs and compatible parts. |
| `get_customer_products(customer_id)` | Registered devices. |
| `link_product_to_customer(product_id, customer_id)` | Register ownership on first contact. |
| `get_service_price(service_code, model_id)` | Part price + labour fee. |

### 5.3 `knowledge` (:8103)

| Tool | Purpose |
|---|---|
| `get_playbook(issue_type, category, model_id?)` | Ordered diagnostic steps (e.g. battery not charging → check adapter, BIOS battery health, charging IC, replace battery). |
| `suggest_next_steps(ticket_id)` | Playbook steps minus steps already tried, marked worked/failed. |
| `search_kb(query)` | Semantic search over playbooks. |

### 5.4 `messaging` (:8104)

| Tool | Purpose |
|---|---|
| `send_reply(conversation_id, text)` | Routes to the conversation's own channel. The single way the brain talks to customers. |
| `send_email(to, subject, template, data, ticket_id?, attachments?)` | Transactional email (payment link, confirmation, visit scheduled, job assigned, restock alert, ticket raised). `attachments` is an allowlist of one, `["receipt_pdf"]`, only with the `payment_confirmed` template and the payment's id in `data.payment_id`; anything else is refused. The outbox payload carries `attachments: [{type, payment_id}]`, never the PDF's bytes (§7.6 step 6). |
| `notify_staff(user_id or role, title, body, link)` | Dashboard / technician notification. |

`messaging` doesn't talk to Discord or Telegram directly. It writes to the `outbox` table, and the backend's channel dispatcher delivers it (retries included). That keeps bot connections in one process.

Whether a reply went out is read back from its `outbox` row, never from "did my own `deliver_pending()` call send it" — the loop ticks every second and often gets there first, and the agent still has to be told it was delivered (`dispatcher.outcome_of`).

The dispatcher runs one tick at a time. It commits the `attempts` bump to release the row lock before the send (holding a database connection across a network call would be worse), which leaves the row `pending` while it is being delivered; the loop and the request paths both call it, so without that one-at-a-time guarantee a reply would be claimed twice and the customer would get it twice. The request paths pass the conversation they just handled, so a customer never waits on somebody else's queue; the loop drains everything and does the retries.

### 5.5 `payments` (:8105) — see §7.6

| Tool | Purpose |
|---|---|
| `create_payment_request(ticket_id, customer_id, service_code, address_id, staff_user_id?, note?)` | Invoice + tokenized UPI payment link. From the payments page (§11.2) an admin's `staff_user_id` and `note` (what they checked) are given too, and the timeline events name that admin as the actor; `/payments` gives neither. The amount and line items are computed **in code** from `service_catalog.labour_fee` plus the price of the part compatible with the ticket's device (the cheapest, by SKU on a tie); there is no amount argument, so neither a model nor a customer can set one. Creates `invoice_number` (`INV-YYYY-NNNNN`, per Indian calendar year), `public_token` (`secrets.token_urlsafe(24)`) and `expires_at` (`PAYMENT_LINK_TTL_MINUTES`), sets the ticket `awaiting_payment`, emits `payment.link_sent`. Refuses a ticket that already has an open (pending or verifying) payment, a ticket of another customer, an address that isn't theirs, a part-based service on a ticket with no verified device, and a covered repair on a device in warranty (`free_under_warranty`: it is free, §7.6). |
| `get_payment_status(payment_id)` | Status (a pending link past `expires_at` reads as `expired`), invoice, amount, UTR, attempts left, verification, `needs_review`. |
| `cancel_payment(payment_id, staff_user_id?, note?)` | Cancel a pending, verifying, or failed payment, which expires its link; the ticket goes back to `in_progress`. A paid one is refused (that's a refund). Cancelling a verifying one warns that money may be on its way. From the payments page, the admin and their note are on the timeline. A payment is never deleted. |
| `submit_utr(token, utr)` | The customer's UTR from the pay page: exactly 12 digits; refuses an expired, paid, cancelled, or refunded payment, a UTR another payment already holds (or a bank alert already reconciled against another payment), and more than 5 well-formed attempts per link (refused ones count, so a link can't probe UTRs). Sets `verifying`, emits `payment.utr_submitted`. A UTR sent while `verifying` (or `failed`) replaces the one held — a typo corrected on the pay page: it uses an attempt, restarts the `PAYMENT_VERIFY_TIMEOUT_MINUTES` clock (`utr_submitted_at`, `needs_review` cleared), and its `payment_utr_submitted` timeline event says `corrected`, the `previous_utr` and the attempt number; the identical UTR again is `already_submitted` and uses none. A payment the verifier paid meanwhile is `already_paid` (the row is locked by both). |
| `mark_paid_manually(payment_id, staff_user_id, note)` | The admin backup after `needs_review` (§7.6): only an admin's id, with a note saying what they checked; `verified_by` is that id and the timeline event's actor is the admin. Emits `payment.paid`. |
| `list_payments(status?, needs_review?, query?, limit=50, offset=0, payment_id?)` | The payments page's list (§11.2), newest first: every payment with its ticket number, customer, service, attempts left, `needs_review` and who verified it (`verified_by_name`). `status` is one of the §8.1 statuses (a pending link past its expiry counts as `expired`; anything else is `bad_status`); `query` matches invoice, UTR, ticket number, customer name or email; `payment_id` gives one row. Read-only, but staff-only. |
| `correct_utr_manually(payment_id, staff_user_id, utr, note)` | An admin enters or corrects the UTR for the customer: exactly `submit_utr`'s path (same checks, the same five attempts, `payment.utr_submitted`), with the admin as the event's actor and their note. |
| `reject_payment(payment_id, staff_user_id, reason)` | An admin rejects a `verifying` payment the bank statement doesn't show: `failed` with the reason on the timeline (`payment_failed`, actor the admin), `payment.failed`. The customer may send another UTR while attempts remain. |
| `extend_payment(payment_id, staff_user_id, expires_in_minutes, note)` | An admin gives an unpaid link more time (5 minutes to 7 days from now). A pending or expired link becomes pending; an expired one is refused when the ticket has another open payment, and its ticket waits on payment again. Timeline event `payment_link_extended`. The customer isn't messaged. |

Amounts leave the server as two-decimal strings (`"6.90"`), never floats. Every payment is stored with `provider = 'upi_utr'` and `currency = 'INR'`, constants in `app/payments/money.py` (UPI settles in rupees only, and there is no other method). `submit_utr` and `mark_paid_manually` are in `router.MODEL_FORBIDDEN_TOOLS`: no model is ever offered them, whatever its role or a command's `allowed_tools` (`filter_tools` drops them, and `run_tool_loop` refuses them again). So are `create_payment_request` and `cancel_payment` (`router.PAYMENT_LINK_TOOLS`): billing a customer is a staff action, so neither the copilot nor a custom command can create or cancel a payment link, and saving a custom command that lists one is a 422. The one fixed use stays: the staff `/payments` pipeline calls `create_payment_request` in code (`router.BILLING_PIPELINE_TOOLS`, through `CommandTools`; the gate of a custom command is `model_driven` and refuses it). Only the payments page (`app/api/payments.py`) calls `cancel_payment`, with the admin's id. So are the payments page's own tools, `list_payments`, `correct_utr_manually`, `reject_payment` and `extend_payment` (`router.PAYMENT_ADMIN_TOOLS`): every write needs an admin's `staff_user_id` and a note, checked in the tool, and only `app/api/payments.py` calls them, in code. So are the stock and dispatch writers (`router.WORKFLOW_ONLY_TOOLS`, §5.6, §5.7).

### 5.6 `dispatch` (:8106)

| Tool | Purpose |
|---|---|
| `find_technician(city, skill, date, exclude_technician_ids?)` | The technician with the least work among those with the skill, `is_available`, fewer than `MAX_JOBS_PER_TECH_PER_DAY` non-cancelled jobs that date, and in the customer's city (§7.7). No distance, no maps. `exclude_technician_ids` leaves those out: a rejected job's hand-over passes everyone who rejected a job on the ticket, so nobody is offered the same job twice. |
| `create_job(ticket_id, technician_id, address_id, service_code, part_id, date)` | Service job for a date (no time slot; §7.7) + `job.assigned` event + a timeline event. The capacity check and the insert run under a lock, so two payments at once can't both take a technician's last place that day. |
| `update_job_status(job_id, status, notes?)` | Forward only: assigned → accepted → en_route → on_site → completed; cancelled from any state except completed. A timeline event and `job.status_changed` (`job.completed` too, when completed). |
| `reject_job(job_id, technician_id, reason)` | The assigned technician can't take the job. In one transaction with the job row locked: refused unless it is `assigned` (`job_closed`, `not_assigned`) and theirs (`not_yours`), and a reason of 3 to 300 characters (`reason_required`). The job becomes `cancelled` with notes "Rejected by <technician>: <reason>", a `job_rejected` timeline event (actor the technician; `job_id`, `technician_id`, `reason`), and the ticket's `updated_at` bumped. The part stays reserved for the ticket. Emits `job.rejected` and `ticket.updated`, **never** `job.status_changed`, so the cancellation workflow (which releases the part) doesn't run. |
| `get_jobs(technician_id)` | Technician's jobs. |
| `list_technicians(city?, skill?)` | Read-only, for the copilot: every technician (or those in a city, aliases as in §7.7, or with a skill) with city, skills, availability and open jobs. |

`create_job` refuses, with nothing written: a ticket that already has an open job (`job_exists`), a technician who is unavailable, lacks the service's `required_skill` or is full that date (`technician_full`), an address that isn't the ticket's customer's, a shipped service (`no_visit`), and a part-based service whose part this ticket hasn't reserved (`no_reservation`): a job never goes out without its part. It records the warehouse the reservation is in. `create_job`, `update_job_status` and `reject_job` are in `router.MODEL_FORBIDDEN_TOOLS` (`WORKFLOW_ONLY_TOOLS`): only the workflows and the job API call them, in code; models keep `find_technician`, `get_jobs` and `list_technicians`.

### 5.7 `inventory` (:8107)

| Tool | Purpose |
|---|---|
| `check_stock(part_id or sku or part_type + model_id)` | On hand, reserved, available per warehouse. `sku` (e.g. `BAT-AX14`, any case) finds the same part as its `part_id`; `part_id` wins when both are given; none of the three forms is `bad_request`. |
| `find_compatible_part(model_id, part_type)` | Correct part SKU for this model, with its stock per warehouse: the cheapest compatible (by SKU on a tie), the same part a payment bills (§5.5). |
| `reserve_part(part_id, warehouse_id, qty, ticket_id)` | When a repair is booked (§7.7). Refused, with nothing changed, when fewer than `qty` are available. Then the low-stock check: returns `low_stock` and `restock_due`, true when this reservation took available from above the part's `reorder_threshold` to at or below it (once per drop, §7.8). |
| `consume_part(part_id, warehouse_id, qty, job_id)` | On job completion: on hand and reserved both drop. Then the low-stock check: returns `low_stock`, and `restock_due` when available ≤ `reorder_threshold` and no restock request for the part is open. |
| `release_part(part_id, warehouse_id, qty, ticket_id, job_id?)` | On cancellation or a booking that fails: gives back what this ticket reserved. |
| `create_restock_request(part_id, warehouse_id, qty, reason)` | Low-stock report + `stock.low` event. At most one open (or ordered) request per part: while one is, it is returned and nothing new is created. The completion workflow emails `WAREHOUSE_ALERT_EMAIL` and notifies admins when one is created (§7.8). |
| `usage_report(days=7)` | Parts used, by type. |
| `list_stock(part_type?, low_only?, limit=25)` | Read-only, for the copilot's warehouse questions: totals and a per-type summary for the whole warehouse (or one part type, plurals such as "batteries" understood; anything else is `bad_part_type`), then one compact row per part (SKU, name, on hand, reserved, available, threshold, low, `restock_requested`), running-low rows first, at most 60. The whole warehouse fits one tool result (§4.5). |

`available = qty_on_hand − qty_reserved`. Every stock change is one transaction: the inventory row is locked, the change is a single guarded `UPDATE` that never takes a quantity below zero (the `CHECK (… >= 0)` columns are the backstop), and the movement is written to `inventory_movements` in the same transaction. For `reserve` / `release` / `consume`, `change` is what the movement did to the ticket's reservation (+qty, −qty, −qty; a consume also takes qty off hand), so their sum per ticket, part and warehouse is what the ticket still holds: a ticket can only release or consume what it reserved. `reserve_part`, `consume_part`, `release_part` and `create_restock_request` are in `router.MODEL_FORBIDDEN_TOOLS` (`WORKFLOW_ONLY_TOOLS`); models keep `check_stock`, `find_compatible_part`, `usage_report` and `list_stock`.

---

## 6. Channels

### 6.1 Adapter pattern

Every channel implements the same interface, so the brain never knows or cares where a message came from.

```python
class ChannelAdapter(Protocol):
    channel: Literal["discord", "telegram", "email", "web"]
    async def start(self) -> None: ...                       # connect / begin polling
    async def send(self, thread_id: str, text: str, meta: dict) -> str: ...  # returns external message id
    async def typing(self, thread_id: str) -> None: ...

@dataclass
class InboundMessage:
    channel: str
    external_user_id: str       # discord user id, telegram user id, email address, web session id
    external_thread_id: str     # DM channel / thread id, telegram chat id, email thread root Message-ID, web session id
    display_name: str | None
    text: str
    attachments: list[dict]
    external_message_id: str
    raw_meta: dict              # subject, In-Reply-To, etc.
```

Inbound path: adapter → `InboundMessage` → `identity.resolve()` (finds/creates customer + conversation) → intake pipeline.
Outbound path: `messaging.send_reply(conversation_id)` → `outbox` row → dispatcher looks up `conversations.channel` → that adapter's `send()`.

**This is what guarantees "reply on the same platform":** the reply target is the conversation's channel, never chosen by the model.

### 6.2 Per-channel details

| Channel | Library | Connection | Thread key | Notes |
|---|---|---|---|---|
| Discord | `discord.py` | Gateway websocket, started as an asyncio task in FastAPI lifespan | DM: the DM channel id. In `#support` itself: `<channel id>:<user id>`, one conversation per person, and the reply @mentions them in the channel (only them: `allowed_mentions`). In a thread under `#support`: the thread id, and replies stay in it. The bot never opens a thread | Enable **Message Content Intent** in the Developer Portal. Bot permissions: View Channels, Send Messages, Send Messages in Threads, Read Message History. A bot @mention at the start of a message is dropped before intake. Use `async with channel.typing()`. |
| Telegram | `python-telegram-bot` | Long polling inside the same event loop: `await app.initialize(); await app.start(); await app.updater.start_polling()` (don't use `run_polling()`, it blocks the loop) | `chat_id` | `send_chat_action("typing")`. |
| Email | `imap-tools` (run in thread) + `aiosmtplib` | Poll INBOX for UNSEEN every `EMAIL_POLL_SECONDS`, **except** bank alerts (below) | Root `Message-ID` via `In-Reply-To`/`References`; ticket number `[SR-2026-00042]` in subject as backup | Strip quoted history with `email-reply-parser`. Reply with `In-Reply-To` + `References` so it stays in the same Gmail thread. HTML templates with Jinja2: `reply.html` for replies; a `messaging.send_email` mail renders its own `<template>.html` + `.txt` with its data and keeps its subject. |

**Bank alerts are not customer mail.** Every mail whose subject contains `BANK_ALERT_SUBJECT` belongs to the UPI verifier (§7.6), whoever sent it: the email channel's IMAP search excludes them (`NOT SUBJECT`), a second check in code skips any that slip through, and neither marks them seen. So a forwarded bank SMS never becomes a ticket, and the verifier never reads a customer's mail. The channel fetches with `BODY.PEEK` and flags only the mail it took as seen.

**`EMAIL_REDIRECT_TO`** (development only): every outgoing email, replies and transactional mail alike, goes to that one inbox instead, with the real recipient in the subject (`[to riya.sharma@example.com] Invoice …`).
| Web | FastAPI WebSocket `/ws/chat/{session_id}` | Browser widget | web session id | Short pre-chat form (name + email) links the web session to a customer. |

### 6.3 Identity across channels

- `customer_identities (channel, external_user_id)` maps each platform account to one customer.
- Email and web give an email address immediately. Discord/Telegram customers get linked when they share an email (e.g. during `/payments`) or when they quote a serial registered to a customer **and** give that customer's email (§7.1). The linked customer's tickets then show one unified timeline across all channels.
- Linking by serial is one-way and narrow. Only a **placeholder** customer is folded into the owner: no email, no tickets, and nothing but the one channel account on it (`identity.is_anonymous_customer`), and only when the email it gives matches the owner's (`identity.customer_email`, compared ignoring case). The serial alone never links anyone: a placeholder quoting someone else's serial is asked for its email first, and a different email (or none) keeps the ticket, the email and the confirmation on the person who wrote, flagged `ownership_mismatch`. A customer who already has an email or any history is never merged either; that case is flagged `ownership_mismatch` too. (Linking on the serial alone once merged a Discord user into another customer and sent their confirmation to that customer's address.)
- If a serial is registered to a different customer, the ticket is still created but flagged `ownership_mismatch` for the agent.
- The channel layer (`channels/identity.py`) owns the inbound `messages` row and `conversations.context`, the intake state machine's slot-filling state (§7.1). There is no §5 tool for either, and the brain reaches the database only through the hub (§4.1). A stored inbound message starts with `ticket_id` null: which ticket it belongs to is decided after intake runs, by the new ticket or by §7.2's `add_followup(message_id)`.

---

## 7. Core flows

### 7.1 New issue (intake)

```
customer message
  → resolve identity + conversation
  → conversation.context.awaiting set?  ── yes → slot-filling handler (serial, payment details, diagnostic feedback)
  → extract_serial + decide.classify_intake (+ complete_json on MODEL_FAST when needed, §4.4) → {intent, category, issue_type, serial?, model?}
  → intent is new_issue and no serial?
       → ask for serial (include "find it on the sticker under the laptop, or run `wmic bios get serialnumber` on Windows"),
         and for the email address in the same message when the customer has none on file (Discord, Telegram)
       → context.awaiting = "serial_number"; stop
  → catalog.lookup_serial
       not found → ask again once; after 2 misses create ticket flagged unverified_product
  → tickets.find_similar_tickets(customer, product, text)
       match → §7.2 duplicate flow; stop
  → still no email (none on file, none typed)?
       → ask for it, context.awaiting = "email" (the device kept in context.pending); after 2 replies without one,
         the ticket is raised anyway, with no confirmation
  → tickets.create_ticket
  → knowledge.get_playbook → MODEL_FAST writes ticket summary + first diagnostic plan (stored in diagnostic_steps)
  → messaging.send_reply: ticket number, what happens next, 1–2 safe self-help tips for software issues only,
    and "I've also emailed a confirmation to k***@example.com" (masked)
  → the "ticket raised" email, ticket_created.html, to the customer (§4.1); not on the email channel, where the reply is that email
  → events: ticket.created → dashboard updates live
```

If the customer gives everything in one message ("My Aurora 14, serial AX14-7F3K92, battery won't charge", plus an email address when none is on file), the ticket is created on that first message with no questions asked.

**The customer's email** is read from their words by code (`intake.extract_email`: a regex, then `email-validator`), never by a model, in whichever order it and the serial arrive. Asked only when the customer has none on file: the web widget's pre-chat form and the email channel give one up front, so those chats go exactly as before. It is asked after the serial is looked up: a serial registered to someone else is only linked to its owner when this email matches the owner's (§6.3), otherwise the ticket and the confirmation stay with the person who wrote. The confirmation goes to the address on file when there is one: a chat message never changes a customer's email (§6.3). Otherwise to the typed one, which is saved to the customer only when they have none and no other customer has it.

**The `diagnostic_feedback` slot** (opened by `/diagnose-send`, §7.5), like `payment_details`: `context.diagnostic_request = {ticket_id, ticket_number, step_ids in order, steps (the texts sent), requested_by, requested_at}`.
- One `MODEL_FAST` `complete_json` (`prompts/diagnostic_feedback_extract.md`) reads the reply as `{steps [{step_number, result, note}], problem_fixed}`. Code (`intake.feedback_results`) keeps only step numbers from the request, only `worked` / `failed` / `skipped`, the first mention of each step, and a note only when the customer wrote those words (cut to 200 characters). Anything not mentioned stays pending; a model can invent nothing.
- The results go to `tickets.record_diagnostic_results` in one call; the ticket goes back to `in_progress` and the slot closes, with a fixed reply naming what was noted. `problem_fixed` is only a note for the agent (`tickets.add_message`, sender `system`): nothing resolves or closes a ticket automatically.
- Three replies with nothing usable close the slot ("an agent will follow up"). A `diagnostic_request` older than 24 hours is dropped and the message handled as usual. No model: the §15 reply, and the slot stays open. Intake still reaches no payments, dispatch or inventory tool (§4.1).

Every unit has its serial (`products.serial_number`, `UNIQUE NOT NULL`, format `<model number>-<6 characters>`, e.g. `AX14-7F3K92`), so a serial identifies exactly one device. The reply names the device the catalog found, serial included ("for your Aurora 14 (serial AX14-7F3K92)"), so a customer who typed the wrong one sees it at once; a serial the catalog doesn't know is asked for again before any ticket, as above.

### 7.2 Duplicate detection

1. Candidates: open tickets (`status not in resolved, closed`) for the same customer **and** same product, created in the last 30 days.
2. Score: cosine similarity between the new message embedding and the ticket embedding, plus same `issue_type` from intake.
3. `similarity ≥ DUPLICATE_SIMILARITY_THRESHOLD` (0.82) or (same issue_type and ≥ 0.70) → duplicate. Borderline (0.60–0.70) → one `decide.is_duplicate` check (duplicate when p ≥ 0.5). The thresholds stay in code and settings, never in a prompt.
4. On duplicate: `tickets.add_followup` → message added to that ticket's timeline, `duplicate_count + 1`, priority raised one level every 2 follow-ups (max `urgent`), `ticket_events` gets "Customer followed up via Telegram".
5. Customer reply: "This is already being handled under SR-2026-00042. We've raised its priority."

Works across channels: a complaint on Discord and a later email about the same battery land on the same ticket once the identities are linked.

That example is also why intake runs this check on **two** of the §4.4 intents, not just `new_issue`: a customer chasing a problem writes `follow_up`, and such a message rarely repeats the serial. So the candidate search is narrowed to one product only when this message told us which product it is; otherwise it is scoped by customer alone, over their open tickets. When nothing matches and the message did name a device, it is a new issue after all and a ticket is created.

A message with no serial is checked against the customer's open tickets *before* the serial is asked for. If the conversation already has an open ticket, or the customer has exactly one, a `follow_up` or `provide_info` goes straight onto it (the steps above), and a `new_issue` does when the scoring says it is that ticket. With several open tickets, the best match above the thresholds wins; otherwise intake lists the open ticket numbers and asks which one is meant (a reply naming a ticket number picks it). The serial is asked for only when there is no open ticket, or the message does not score as the one open ticket.

### 7.3 Agent reply with AI polish

1. Agent types a rough note in the ticket composer: "battery dead need replace can u confirm".
2. Clicking **Polish** (or sending with polish on) calls the Writer (`MODEL_FAST`):
   - System rules: keep every fact, number, date, and commitment exactly; add no new promises or facts; polite, clear, short; match the customer's language; no greeting fluff beyond one line.
3. The dashboard shows the polished text **as a preview** next to the original. Agent can edit, then sends. `writer.py` also compares the numbers in the rewrite against the note's and returns a `warning` when the rewrite mentions one the note did not — a price or a total the model worked out for itself is the dangerous kind. The rewrite is still shown: the agent is the guard (step 4), and discarding it would lose good work to a false alarm.
4. Stored: `messages.body` (sent text) and `messages.body_original` (agent's note). The customer receives it on their channel.

Human stays in control: nothing polished is sent without the agent seeing it.

### 7.4 Natural-language search

Search bar (⌘K) query, e.g. "open battery tickets from telegram this week that got escalated":

1. `MODEL_FAST` (`complete_json`) converts it to filters: `{status: [open…], issue_type: battery, source_channel: telegram, created_after: …, min_duplicate_count: 1, text: "battery"}`.
2. `tickets.search_tickets` runs SQL filters + `ts_rank` full-text + pgvector similarity, merged with reciprocal-rank fusion.
3. Results appear as ticket cards, with a one-line "why this matched".

Built in `app/brain/search.py`. The model returns `{status, open_only, priority, issue_type, category, source_channel, created_within_days, min_duplicate_count, text}`; code keeps only values in the §8.1 / §4.4 vocabularies, caps the days at 365, and turns them into `created_after`. A query naming a ticket number (`SR-2026-00042`) goes straight to `tickets.get_ticket` with no model call, and one naming a device serial (`AX14-7F3K92`, any case) straight to `tickets.search_tickets` for that device's tickets (`ranking: "serial"`, why "Device serial …"); inbox rows and the palette show each ticket's serial next to the device; no model reachable means a plain text search of the words, and the response says so (`notice`). Search may call only `tickets.search_tickets` and `tickets.get_ticket` (`search.SEARCH_TOOLS`, checked in code), and writes one `ai_runs` row (`role=copilot`, `trigger=search`). The "why" line is written in code from what really matched (full text, similarity %, the filters). The UI is the top bar's ⌘K palette and the inbox's search box (`/inbox?q=…`); a search runs on Enter, never per keystroke.

### 7.5 Suggested buttons and slash commands

**Suggested buttons:** when a ticket opens or gets a new message, `decide` picks 3–4 relevant actions (one yes/no question per command, the most likely kept) from the command registry (built-in + the agent's custom commands) and returns them as chips, e.g. `Run battery diagnostics`, `Ask for photos`, `/payments battery replacement`, `Summarize`. Clicking a chip runs that command. Cached per ticket until the next message.

Built in `app/brain/suggestions.py`. Code decides which commands are offered from the ticket's state (e.g. `/payments` only with a verified device, out of warranty, and no open payment or job; `/schedule` only in warranty; `/diagnose` only when nothing on the checklist is still pending, `/diagnose-send` only when at least one step is; nothing that changes a closed ticket) and the words `/ask` asks for (by issue type); one `decide` call (one `noul` per offered command, at most 5 custom ones) ranks them, the yeses are kept most likely first, filled to 3 from the code's order, at most 4. `/escalate` and `/close` chips fill the composer for the agent's words instead of running. The cache key is the ticket, the agent, and its latest message, status, priority, open payment and job, and pending steps. No model reachable: the code's order alone (`source: "rules"`). A chip runs through `POST /api/tickets/{id}/commands` like a typed command, so the same gates apply.

**Built-in commands**

| Command | What it does |
|---|---|
| `/payments [service]` | §7.6 end-to-end payment flow; with no words it picks the service from the ticket |
| `/diagnose` | Next diagnostic steps from the playbook, excluding what's been tried: `knowledge.suggest_next_steps`, then `tickets.set_diagnostic_plan(suggested_by='playbook')` adds them to the checklist. Sends nothing. No model |
| `/diagnose-send` | Send the customer the checklist's pending steps (`/diagnose`'s playbook step first when the checklist is empty; nothing pending: says so and stops). One `messaging.send_reply` on the conversation they last wrote on: a fixed template (`commands.DIAGNOSTICS_ASK`) with their first name, the ticket number, the steps numbered in stored text verbatim, and "reply with what worked, what didn't, or which you skipped". Then `context.awaiting = diagnostic_feedback` (§7.1), `tickets.update_status(awaiting_customer)` and a `diagnostics_sent` timeline event. Steps stay pending until the customer answers. Refused while that conversation's `/payments` details slot is open. No model |
| `/summary` | Refresh the AI summary of the whole cross-channel conversation: `tickets.get_ticket`, one `MODEL_FAST` call, `tickets.update_summary`. No model reachable: an error, and the summary is unchanged |
| `/ask <what>` | Ask the customer for specific info (photos, OS version, error code), phrased professionally by `MODEL_FAST`, then `messaging.send_reply` and `awaiting_customer`. A fixed template is sent instead when no model answers or the wording mentions a number the agent didn't write (`writer.invented_numbers`) |
| `/schedule [service]` | Dispatch without payment (warranty repairs): `/payments`' service and warranty steps; a repair that isn't free under warranty is refused, in code. With the customer's address and phone on file it starts the booking chain (§7.7) at once; otherwise it asks for the details exactly like `/payments` |
| `/parts [service]` | Stock + compatible part for this ticket's model: the service from the words or the ticket (as `/payments`), then `inventory.find_compatible_part`. Read-only |
| `/escalate <reason>` | Raise priority one level (max `urgent`; a `priority_raised` timeline event and `ticket.updated`), then `messaging.notify_staff(role=admin)` |
| `/close <note>` | Resolve + send closing message (a fixed template). Refused while the ticket has an open technician job or an open payment |

Words in `<…>` are required and in `[…]` optional. Each built-in is a fixed pipeline whose tools are an allowlist checked in code (`commands.BUILTINS[...].tools`, through `CommandTools`), and none of them is in `router.MODEL_FORBIDDEN_TOOLS` except `/payments`' own `payments.create_payment_request`. Each run writes one `ai_runs` row (`role=copilot`, `trigger=/<name>`).

**Custom commands** (per agent, stored in `slash_commands`):

```yaml
name: warranty-check
description: Check warranty and tell the customer if repair is free
prompt_template: |
  Check the warranty for ticket {{ticket.number}} (serial {{product.serial}}).
  If in warranty, tell the customer the repair is free and ask for a preferred visit date.
  If not, tell them the repair cost for {{ticket.issue_type}} and ask whether to proceed.
allowed_tools: [catalog__lookup_serial, catalog__get_service_price, messaging__send_reply, tickets__add_message]
```

Available variables: `{{ticket.*}}`, `{{customer.*}}`, `{{product.*}}`, `{{agent.name}}`, `{{args}}` (text after the command). Runs through the Copilot tool loop with only `allowed_tools`. A command editor page lets agents create, test, and edit them.

- The variables are exactly `commands.TEMPLATE_VARIABLES`: `ticket.number`, `.title`, `.status`, `.priority`, `.issue_type`, `.category`, `.summary`, `.description`; `customer.name`, `.email`, `.phone`; `product.serial`, `.model`, `.model_number`, `.color`, `.warranty_until`; `agent.name`; `args`. Rendering is plain substitution of those values, never eval or a format string; a template using any other variable is refused when saved (422).
- `allowed_tools` must be copilot tools the MCP servers offer (checked against the hub's registered names) and never one of `router.MODEL_FORBIDDEN_TOOLS` (422 otherwise; that includes the payment-link tools `payments.create_payment_request` and `payments.cancel_payment`), at most 12. At run time they are intersected with the role again (`filter_tools`), every call is checked against the list before the hub is reached, and `run_tool_loop` refuses any tool it didn't offer.
- Per agent: `slash_commands.owner_id` is the creator; each agent lists, runs, edits (`PATCH /api/commands/{id}`) and deletes (`DELETE /api/commands/{id}`) only their own (another's is a 404). A built-in's name can't be taken (409), nor a name the agent already has (409). The built-ins are not rows in `slash_commands`; they come from code (`commands.BUILTINS`).
- A run writes the tool loop's `ai_runs` row (`role=copilot`, `trigger=/<name>`), and each tool call is a `step` of the command's SSE stream.

### 7.6 `/payments` end-to-end

Payment is direct UPI to the company's own account, verified against the bank's own credit SMS. The method comes from the team's open-source UPI gateway (Google Apps Script + Sheets); here it runs on our backend, MCP tools, and Postgres. There is no payment gateway, webhook, or merchant onboarding.

```mermaid
sequenceDiagram
  participant A as Agent (dashboard)
  participant B as commands.py (/payments)
  participant C as Customer (their channel)
  participant I as Intake (payment_details slot)
  participant P as payments MCP
  participant G as Pay page /pay/[token]
  participant H as Company phone
  participant V as UPI verifier
  participant W as Workflow engine

  A->>B: /payments battery replacement (POST /api/tickets/{id}/commands, SSE)
  B->>B: tickets.get_ticket, catalog.lookup_serial (warranty), catalog.get_service_price
  B->>C: messaging.send_reply — "please send full name, email, phone, service address"
  Note over B,I: context.awaiting = payment_details, plus the agent's payment_request
  C->>I: replies, maybe over several messages
  I->>I: complete_json reads them; code validates every field; asks only for what's missing
  I->>B: all four in → finish_payment_details (intake itself calls no payments tool)
  B->>P: create_payment_request(ticket, customer, service, address) — amount computed in code
  P-->>B: invoice INV-YYYY-NNNNN, token, expires in PAYMENT_LINK_TTL_MINUTES
  B->>C: send_reply with {FRONTEND_URL}/pay/{token} + send_email payment_link.html (the invoice)
  C->>G: scans the UPI QR, pays, enters the 12-digit UTR
  G->>P: POST /api/pay/{token}/utr → payments.submit_utr → verifying
  H->>V: the bank's credit SMS, forwarded to the project Gmail (subject BANK_ALERT_SUBJECT, BANK_SECRET)
  V->>V: sender allowlist, SPF/DKIM, secret → UTR + amount → bank_alerts row
  V->>V: verifying + same UTR + same amount to the paisa → paid, in one transaction
  V->>W: payment.paid
  W->>C: payment_confirmed.html receipt + send_reply "payment received"
  W->>A: dashboard updates live (timeline, Agent Activity)
```

**1. Asking for the details** (`app/brain/commands.py`)

- Agents and admins only: `POST /api/tickets/{id}/commands {"name": "payments", "args": "battery replacement"}`, with progress streamed as SSE (§10). The words are optional.
- **With words**, they are matched to a `service_catalog` row in code: the code typed as is, or `commands.SERVICE_PATTERNS`, the words people use for each repair (screen / display / flicker, overheating / fan, keys / keyboard, corrupted / SSD, won't boot / OS, …), plus "replace" and "upgrade" for the `*_REPLACE` / `*_UPGRADE` services. Only a tie or no match costs one `decide` choice, and the answer must be a catalog code.
- **With no words**, the ticket picks it. The candidates are the services that fit the device, worked out in code: a part compatible with its model (`catalog.lookup_model`), or, for a service with no part, a visit to a laptop or desktop (`OS_REINSTALL`); `UPI_TEST` is only ever picked by name. A keyword match over the title (×3), the AI summary (×2) and the customer's message, plus the §4.4 issue type (battery, display, keyboard, overheating, os), picks a clear winner in code. A tie or no match costs one `decide` choice among the candidates, which reads the ticket, the device and what was tried. Still unsure (an answer below `DECISION_MIN_CONFIDENCE`, a code outside the candidates, or no model): the command stops and tells the agent to add the service, e.g. `/payments display replacement`.
- **Warranty decides the price, in code** (`money.WARRANTY_COVERED_SERVICES`). In warranty, replacing a failed part is free (battery, CMOS, display, keyboard, fan, SSD replacement, charger, ear cushions), and so is `OS_REINSTALL`. `RAM_UPGRADE` and `SSD_UPGRADE` stay paid (a warranty covers what was sold), and `UPI_TEST` is never free. Out of warranty, the computed price applies. `payments.create_payment_request` refuses a free one too (§5.5).
- A free repair: no invoice. The customer is told it's free under warranty and asked for the same details (a visit needs the address); the conversation's `payment_request` carries `free: true`, set here in code from the warranty, never from customer text. When the details are in, the booking starts at once (step 3).
- A ticket that already has an open technician job is refused before anything is sent.
- The SSE steps (`ticket`, `device`, `service`, `warranty`, `price`, …) and the ticket timeline (the `status_changed` note) say which service was picked and why ("from the ticket: "battery" in the title, issue type battery"), the warranty result, and the price or "free under warranty". A model only ever picks a catalog code; the price and the warranty decision stay in code.
- The agent hears every blocker before the customer is asked anything: no verified device, no compatible part, `UPI_ID`/`UPI_PAYEE_NAME` not set, or an open payment already on the ticket.
- The ask goes to the conversation the customer last wrote on about this ticket. It sets `context.awaiting = payment_details` and `context.payment_request = {ticket_id, customer_id, service_code, requested_by, requested_at}`, and moves the ticket to `awaiting_customer`. One `ai_runs` row and an `agent.tool_called` per tool, as in §4.3.

**2. Collecting them** (intake's `payment_details` slot, §7.1)

- `MODEL_FAST` `complete_json` reads each reply into `{full_name, email, phone, address{line1, line2, city, state, postal_code}}`. `app/payments/details.py` keeps a value only if its format passes in code (email syntax; a 10-digit Indian mobile, with +91/91/0 prefixes allowed; a 6-digit PIN code) **and** the customer actually wrote it, so a model can't supply one. Only what's still missing, or was invalid, is asked for again. The address stays text (line1 such as "45, 3rd Main Road, 5th Cross", city, state, PIN); there are no coordinates.
- The customer may also paste a Google Maps or Apple Maps link. It is read from their own text in code, kept only if it is https on `maps.app.goo.gl`, `goo.gl/maps`, `google.com/maps`, `maps.google.com` or `maps.apple.com` (no login or port parts), and saved as `addresses.location_url`. It is optional and never asked for, and a link is never an address line.
- Three replies with no details close the slot ("an agent will follow up"). A `payment_request` older than 24 hours is dropped and the message handled as usual. No model available: the §15 reply, and the slot stays open.
- Intake never calls a payments tool (§4.1). When all four are in, it hands them to `commands.finish_payment_details`. The ticket, customer, and service come from the agent's `payment_request`; customer text supplies contact details only, never a service or an amount.

**3. The link and the invoice**

- **A free warranty repair** has no link: `finish_payment_details` saves the details, replies "free under warranty, I'm booking it now", and starts the booking chain (`workflows.start_warranty_booking`, §7.7) in the background, authorized by the staff member who ran `/payments` (`requested_by`), never by customer text. The chain checks the warranty again, in code, before booking.
- `finish_payment_details` saves the service address (an identical one is reused; it becomes the default; a maps link the customer pasted is kept on it) and the customer's name and phone. Their email is filled in only when the record has none: changing an existing email from a chat message would hand the account to whoever wrote it (§6.3). The invoice then goes to the email on file, and the customer is told which address (masked).
- `payments.create_payment_request` computes the amount (§5.5). Then `send_reply` on the customer's channel: `{FRONTEND_URL}/pay/{token}`, how to pay, and the expiry. Then `send_email` with `payment_link.html`: invoice number, date, ticket number, problem summary (the ticket's title; the AI summary is written for agents and stays off customer documents), device and serial, customer name, email, phone, service address, line items, total, link expiry.
- On any failure the customer still gets an answer ("an agent will send your payment link here shortly"), the agent who ran `/payments` gets a notification with the reason, and the slot closes.
- Links point at `FRONTEND_URL`, so on the demo machine they open on localhost. To open one on a phone, run a tunnel to the web server's port 3000 and set `FRONTEND_URL` to the tunnel's URL (README): the web server proxies `/api/pay/*` to the backend (`web/next.config.ts`), so one tunnel serves the page and its API.

**4. Paying, and the UTR** (`app/api/pay.py`, public)

- No login: the token is the only key. Every route is rate-limited per token and per client IP (in memory, sliding one-minute windows: invoice 30/60, UTR 10/20, status 120/120), and a request with a malformed token is counted against the IP and refused before the database, so scanning for tokens runs into the IP limit. The page reaches these routes through the web server's proxy, so the backend sees 127.0.0.1 unless the request carries `X-Forwarded-For` (uvicorn trusts it from 127.0.0.1 and uses its rightmost address, which a tunnel sets to the visitor's). The budgets assume the worst case, every phone on one address: three pages polling every 3 s use half the status limits, on one link or three.
- `GET /api/pay/{token}`: the invoice above, plus `upi = {upi_id, payee_name, amount ("6.90"), uri}` with `uri = upi://pay?pa=<UPI_ID>&pn=<UPI_PAYEE_NAME>&am=6.90&cu=INR&tn=<ticket number>` for the QR code (null when UPI isn't configured), the status, and a message for the customer. Internal fields (ids, `verified_by`, `needs_review`) are not exposed.
- `POST /api/pay/{token}/utr {utr}`: exactly 12 digits, checked here first (a malformed UTR is not an attempt), then `payments.submit_utr` (§5.5). Refusals map to HTTP: expired 410, paid/cancelled/used UTR 409, too many attempts 429, server down 503. Once stored, `match_for_utr` runs at once, because the bank alert may already be in; the response says `verifying`, `paid`, or `failed`. If that immediate match fails, the verifier's `match_waiting` sweep catches the pair on its next tick.
- `GET /api/pay/{token}/status`: what the page polls.
- **The page** (`web/src/app/pay/[token]/`, public, mobile-first from 360 px, light and dark): company, invoice number, ticket number, date, device and serial, line items, and the total in large type. The QR encodes `upi.uri` exactly as the API sends it (never rebuilt in the browser; `qrcode.react`), dark on white with a 4-module quiet zone in both themes. Under it: a "Pay with UPI app" link to `upi.uri`, the UPI ID and the exact amount with copy buttons, and what to do if an app refuses the link. The UTR form keeps digits only, needs exactly 12, shows the attempts left, and shows the server's message for 409 / 410 / 422 / 429 / 503. While `verifying` it polls `/status` every 3 s, one request at a time, pausing while the tab is hidden and waiting out a 429's `Retry-After`; a pending link turns to expired at `expires_at` without a reload. One screen per state: pending, verifying ("checking with the bank", the submitted UTR, and **Entered it wrong? Correct it**, which opens the same form with the attempts left; at none left it says to contact support), paid (check mark, UTR, time, "receipt sent to your email"), failed (the reason, and the form again while attempts are left), expired, cancelled, an unknown link (404), and `upi` null ("online payment isn't set up yet").

**5. Verification against the bank alert** (`app/payments/upi_verifier.py`, started in the lifespan)

- The company phone forwards every credit SMS from the bank to `EMAIL_ADDRESS`, with subject `BANK_ALERT_SUBJECT` and `BANK_SECRET` at the end of the body (iOS Shortcuts or an Android SMS forwarder; §13.1).
- Every `BANK_POLL_SECONDS` the verifier reads UNSEEN mail with that subject over IMAP (in a worker thread, reusing the email channel's login; never the event loop). It runs only when `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`, `BANK_ALERT_SUBJECT`, `BANK_ALERT_FROM`, and `BANK_SECRET` are all set. The partition with the email channel is the subject: every mail with it is the verifier's, whoever sent it (§6.2).
- **Is it genuine?** Checked in order; the first failure is stored as `reject_reason`:
  1. The From address is exactly one of `BANK_ALERT_FROM` (`sender_not_allowed`).
  2. If Gmail's `Authentication-Results` header is present (the topmost one, which Gmail itself adds), SPF, DKIM, or DMARC passed **for the sender's own domain** (`spf_dkim_failed`). A pass for another domain doesn't count: an attacker's server passes SPF for its own domain while forging From.
  3. The body contains `BANK_SECRET` (`missing_secret`). With no `BANK_SECRET` set, nothing is accepted.
- **Reading it** (`upi_verifier.sms_text`, then `parse_credit_alert`): only the credit statement, the credited amount and the UTR are read; every other word (footers, "Sent via SMS Forwarder", balances, promos, other numbers) is ignored. First the mail becomes the SMS: text/plain preferred, an HTML-only mail stripped to text, a body still in quoted-printable decoded, the forwarder's own leading header lines (`From:`, `Sent:`, a forwarded-message block, a `Message:` label) dropped, invisible characters and the passcode removed.
  - **Is it a credit?** It must contain a credit statement (credited / received / deposited); none at all is `debit_alert` (or `not_a_credit_alert`). Failed, declined, reversed, pending, "will be credited" and collect requests are refused wherever they appear (`failed_or_reversed`, `not_completed`). The verb nearest the amount decides the direction: "debited for Rs 6,199.00; JOHN credited" is `debit_alert`, while a "sent", "paid" or "Dr" elsewhere in the text changes nothing.
  - **The amount**: a (Rs|INR|₹) amount, never a balance (`Bal`, `Avl Bal`, `Available balance`, `Balance after transaction`, `Bal is`); of several, the one nearest the credit statement; a two-decimal number is the fallback.
  - **The UTR**: the 12 digits next to a UTR / RRN / Ref / UPI keyword (a UPI path such as `UPI/P2A/<utr>/NAME` counts). Other 12-digit numbers (an account number, a phone number) don't matter, but two *different* tagged numbers are `ambiguous_utr`, never a guess: an admin verifies that payment by hand. With no tagged number, one standalone 12-digit number is the fallback.
  - Unit tests (`tests/test_upi_parser.py`) cover 11 credit formats across 10 banks, debits and non-credits, and whole forwarded mails (a forwarder footer, a balance and "Dr" after the credit, two 12-digit numbers, HTML only, the forwarder's header lines, quoted-printable), plus every SMS in `tests/fixtures/bank_sms/` (its README says how to add a real one).
- **Storing it:** one `bank_alerts` row per mail (`gmail_message_id` UNIQUE, so the same mail twice is one row), with the sender, UTR, amount, the body's SHA-256, `parsed_ok`, and `reject_reason`. The SMS text is never stored or logged. A mail is marked seen only after its row is stored.
- **Matching** (one transaction, alert row then payment row locked): a `verifying` payment with the same UTR **and** the same amount to the paisa → `paid`, `verified_by = 'bank_alert'`, `verified_at`, the alert linked, a `payment_paid` timeline event; `payment.paid` is published after the commit. Running it twice, or two alerts for one payment, pays once.
  - No payment holds the UTR yet → the alert is kept. `POST /api/pay/{token}/utr` runs `match_for_utr` right after storing the UTR, so the alert and the UTR can arrive in either order.
  - Amount differs → `failed` and `needs_review`, the alert keeps the reason (`amount_mismatch: expected 6.90, bank alert says 6.00`), a `payment_failed` timeline event, and `payment.failed`. The customer may submit a new UTR (attempts permitting); staff can see the mismatch.
  - The payment is cancelled, expired, or already paid → the alert is recorded against it and not applied.
- **Sweeps, every tick:** a waiting alert whose UTR a `verifying` payment now holds is matched (`match_waiting`). A pending link past `expires_at` becomes `expired`, and its ticket stops waiting on payment; the customer is told on their channel, once (`workflows.tell_customer_link_expired`, role automation, only `messaging.send_reply`, a fixed text, `ai_runs` trigger `payment.expired`): the link expired before a payment arrived, to reply with the UTR if they already paid, or to reply for a new link. Never "nothing was charged": they may have paid without sending the UTR. A UTR `verifying` for `PAYMENT_VERIFY_TIMEOUT_MINUTES` with no alert gets `needs_review`, a timeline event, and a `messaging.notify_staff(role=admin)`, once. The admin checks the bank statement and uses `payments.mark_paid_manually` (a staff action, logged with their id, §5.5) — the **Mark as paid** button on the ticket's payment card, `POST /api/payments/{id}/mark-paid` (§10) — or cancels.
- **The payments page** (`/payments`, §11.2) is the manual path beside all this, never instead of it: every write goes through the same payments tools (§5.5), and a pasted bank SMS through the same reader and matcher (`upi_verifier.ingest_manual`). That SMS skips the sender, SPF/DKIM and secret checks because the admin is signed in; its `bank_alerts` row has sender `admin:<staff id>` and message id `manual-<uuid>`, and a match pays with `verified_by` = that admin and the admin as the event's actor, never `bank_alert`, whenever it matches. So the only *automatic* path to `payment.paid` stays the forwarded mail. The admin's note (and the SMS's sender and subject, if given) goes on the timeline event when the SMS matches at once; the SMS text is never stored.
- At startup the verifier republishes `payment.paid` for any payment paid in the last day whose receipt never went out, or whose service needs a booking that was never claimed, in case of a crash between the commit and the publish (or between the receipt and the booking); each half of the workflow is claimed once.

**6. After `payment.paid`** (`app/brain/workflows.py`, registered on the bus in the lifespan)

- A fixed chain under the automation role (§4.2): the payment is re-read from the database and nothing is sent unless it really is `paid`. The receipt is claimed once (a `payment_confirmed` timeline event, written under an advisory lock only if there is none yet), so a republished or repeated event never sends a second receipt.
- `messaging.send_email` with `payment_confirmed.html` (the invoice marked PAID, with the UTR and the date) and `attachments=["receipt_pdf"]`, then `messaging.send_reply` on the customer's own channel (it names the UTR only when there is one: an admin's mark-paid has none), then `tickets.update_status(in_progress)`. A failed step is recorded and the rest still run.
- **The PDF receipt** (`app/payments/receipt_pdf.py`, `build_receipt_pdf(invoice, settings)`, ReportLab, A4, selectable text). `email_channel.send` renders it while it builds the mail, from the payment re-read with `load_invoice`, and adds it as `Receipt-<invoice_number>.pdf` beside the HTML and text bodies. It is attached only when that payment is `paid` and the mail is going to the payment's own customer email (checked before `EMAIL_REDIRECT_TO`), so no caller can send a receipt to anyone else. If the payment isn't paid, the recipient doesn't match, or rendering fails, the email still goes without the PDF, the failure is logged, and a `note` timeline event says why (written after the send, so a retried delivery notes it once): a receipt is never withheld. The body's "Your receipt is attached as a PDF" line shows only when it is. One receipt per payment still holds, because the email is claimed once (above). The PDF prints only real data and leaves out anything missing: the logo (`app/assets/logo.png`, from `web/src/app/icon.svg`) and `COMPANY_NAME`, then `COMPANY_ADDRESS`, `COMPANY_PHONE`, `COMPANY_EMAIL`, `COMPANY_GSTIN` when set; "Payment receipt" with a PAID mark; invoice number, paid date and time in IST, ticket number; billed to (name, email, phone); the service address; the device model, serial and its warranty on the day it was paid; the service, the reason (the ticket's title) and the reported issue (`tickets.description`, never the AI summary); the line items and total; UPI, the UTR and how it was verified (the bank alert, or the admin's name); and the footer "computer-generated receipt, no signature needed". No technician or visit date: neither is known yet. DejaVu Sans is bundled (`app/assets/fonts/`, with its licence) for the rupee sign.
- Then the booking (§7.7), claimed once per payment (a `job_requested` timeline event under an advisory lock), so a repeated `payment.paid` never books or reserves twice. `UPI_TEST` (no part, no visit) stops after the receipt.
- Each step publishes `agent.tool_called` (Agent Activity) and the run writes one `ai_runs` row (`role=automation`, `trigger=payment.paid`). `payment.paid` itself reaches every dashboard over `/ws/staff`; the status changes add `ticket.updated`.

### 7.7 Dispatch and technician flow

1. `find_technician`: technicians with the required skill, `is_available`, fewer than `MAX_JOBS_PER_TECH_PER_DAY` (4) non-cancelled jobs on the date, and in the customer's city (`staff_users.city`, matched in code with aliases: Bangalore/Bengaluru, Bombay/Mumbai, New Delhi/Noida/Gurugram/Gurgaon/Delhi). Among them, the least work: fewest open jobs, then fewest jobs that date, then by name. No distance, no geocoding, no maps.
2. Date: the next working day (Monday to Saturday, IST), with the first technician who has capacity that day, trying up to 6 working days ahead. A visit is booked for a **date only**, never a time slot: the technician phones the customer to agree the time, and the customer is told to expect that call.
3. Technician portal shows the job: customer name, phone, the address as text with a location link (the customer's own maps link if they pasted one, otherwise Google Maps and Apple Maps search links built from the address text), device, serial, issue summary, part to carry, what was already tried, and whether it is paid or free under warranty.
4. Technician taps status: Accepted → On the way → Arrived → Completed (with a note). While a job is only assigned, **Reject** sits beside Accept and asks why (3 to 300 characters); an accepted job is committed, and only an admin can cancel it. Each status goes to the ticket timeline; "On the way" and "Completed" send a customer message on their channel.
5. On Completed: `inventory.consume_part` → ticket `resolved` → customer gets a closing message.

**The booking chain** (`app/brain/workflows.py`, role automation, §4.2). It runs after the receipt on `payment.paid`, and for a free warranty repair as soon as the customer's details are in (no invoice; authorized by the staff member who ran `/payments`). Claimed once per payment, or per `/payments` request, with a `job_requested` timeline event.

- **Visit services:** `inventory.find_compatible_part` → `inventory.reserve_part` at the central warehouse → `dispatch.find_technician` for the next working day, then the next, up to 6 working days → `dispatch.create_job` (a technician who filled up meanwhile is skipped for the next one) → `messaging.send_email` `job_assigned.html` to the technician (customer name and phone, the address as text, the location link(s), device and serial, the issue, the part SKU and where it is reserved, what was already tried, paid or free under warranty, and `FRONTEND_URL/jobs/<id>`) → `messaging.notify_staff` to the technician → `messaging.send_email` `visit_scheduled.html` to the customer (the date, the technician's first name, "they'll call you to agree the time") → `messaging.send_reply` on the customer's channel → `tickets.update_status(scheduled)`.
- **Shipped services** (`CHARGER_REPLACE`, `EAR_CUSHION_REPLACE`): reserve the part, `messaging.notify_staff(role=admin)` to ship it, and tell the customer it's on its way; the ticket stays `in_progress`. `UPI_TEST` books nothing.
- **No booking possible** (no compatible part, no stock, no technician with the skill in the customer's city within 6 working days, or no address): no job. Anything reserved is released, admins are notified with the reason, the customer is told an agent will confirm the visit date, and the ticket stays `in_progress`. A job is never left without a reserved part, and a reservation never without its job.
- Each step publishes `agent.tool_called`, and the run writes one `ai_runs` row (`trigger` `payment.paid` or `warranty_repair`). Every text is a fixed template: there is no model call anywhere in this chain.

**A rejected job's hand-over** (`workflows.on_job_rejected`, on `job.rejected`, role automation). Claimed once per job with the cancel path's own claim (`job_closed`, `<job>:cancelled`), so a replayed event does nothing and the cancellation workflow can never release that job's part too. The part stays reserved. It reuses the booking chain's technician search and `create_job` loop: the same city and skill, without everyone who rejected a job on this ticket, starting on the job's own date (or the next working day if that has passed) for `BOOKING_DAYS` working days, with the same per-day capacity. Found: `create_job` (same address, service and part) → `job_assigned` email and `notify_staff` to the new technician → the customer told on their channel who is coming and when (`VISIT_REPLY`; the `visit_scheduled` email only when there is no chat) → `tickets.update_status(scheduled)` with "Job rejected by X (reason); reassigned to Y on <date>" → `notify_staff(role=admin, type=job_rejected)` for information. Nobody has room: `inventory.release_part` → ticket `in_progress` with a note → the customer told an agent will confirm the date (`NOT_BOOKED_REPLY`) → admins notified (`job_rejected`) with the reason and "rebook or contact the customer". One `ai_runs` row (trigger `job.rejected`) and `agent.tool_called` per step, like the other chains.

**The job API** (§10, `app/api/jobs.py`): a technician reads and moves only their own jobs (403 otherwise), and may reject one only while it is assigned; agents and admins can read any job; an admin may cancel one and nothing else. A status change goes through `dispatch.update_job_status`; its `job.status_changed` / `job.completed` events drive the chains below. `GET /api/tickets/{id}` carries the same job view.

### 7.8 Inventory and restock

- On booking: `reserve_part` (`qty_reserved + 1`). On completion: `consume_part` (`qty_on_hand − 1`, `qty_reserved − 1`). On cancel, or a booking that fails: `release_part`.
- **The low-stock alert.** The threshold is each part's own `inventory.reorder_threshold` (per part and warehouse; the seed sets BAT-AX14 to 4 on hand with threshold 3, every other part 5). When a booking's `reserve_part` takes available from above the threshold to at or below it (`restock_due`), the booking chain, once the booking stands (a visit scheduled or a part shipping; a failed booking gave the part back), runs `create_restock_request(qty = reorder_qty)` → `stock.low` event (when the request is new) → `restock_alert.html` to `WAREHOUSE_ALERT_EMAIL` (an `outbox` row) + `messaging.notify_staff(role=admin, type=stock_low, link=/inventory)`. It fires **once per drop**: further bookings while stock is low don't repeat it (4 → 3 alerts, 3 → 2 doesn't), and after stock went back above the threshold the next drop alerts again, on the request that is still open (there is at most one open request per part). The alert authorizes nothing: no payment, dispatch, refund or stock change.
- After every consume: if `qty_on_hand − qty_reserved ≤ reorder_threshold` and no open restock request exists → the same request, email and notification (a backstop; a consume doesn't change available, so the booking normally reported the drop already).
- **Completed** (`job.completed`, claimed once per job): `inventory.consume_part` → the low-stock check above → `tickets.update_status(resolved)` → a closing message on the customer's channel. **On the way** (`job.status_changed`, `en_route`): a message on the customer's channel. **Cancelled**: `inventory.release_part` → ticket back to `in_progress` → admins notified.
- Every movement is logged in `inventory_movements`, which powers the inventory page ("used this week", "running low").

---

### 7.9 Telling the customer a ticket is resolved or deleted

When a ticket is resolved, by any path (the ticket page's status menu, `/close`, a completed job, the copilot calling `tickets.update_status`), or deleted (`DELETE /api/tickets/{id}`), the customer hears it twice at once: an email and a message on their own channel (Discord, Telegram, web chat). Fixed text, no model; role automation, `app/brain/workflows.py`.

```
ticket.updated with status "resolved"  (workflows.on_ticket_resolved)
  → the ticket re-read: resolved, and resolved within the last 10 minutes (a later event on a long-resolved
    ticket is not a new resolution)
  → claimed once per resolution: a resolution_notified timeline event keyed by resolved_at (resolved again after
    a reopen is announced again)
  → at the same time: messaging.send_email ticket_resolved + messaging.send_reply on the conversation the customer
    last wrote on about it; the reply is skipped when the event says customer_told (§5.1)

DELETE /api/tickets/{id}  (workflows.notify_ticket_deleted, after the delete commits)
  → the ticket, customer and conversation read before the delete; that conversation is kept
  → at the same time: messaging.send_email ticket_deleted + messaging.send_reply
```

- On the email channel, the chat message is itself an email in the customer's thread: no second email.
- No email to an automated address (no-reply, postmaster, mailer-daemon: `email_channel.AUTOMATED_LOCAL_PART`), nor a reply on the email channel to one: it would only bounce back into the support inbox.
- No email on file: the chat message alone. No conversation: the email alone.
- A failed notice never undoes the delete or the status change; it is in the ai_runs row.
- The bulk clean-up (`delete_ticket_rows` from a script) tells nobody.

## 8. Database

**PostgreSQL 16 + pgvector.** One Supabase project shared by the team (use the **direct** connection on port 5432; if you must use the pooler, set asyncpg `statement_cache_size=0`). Fallback: local Docker `pgvector/pgvector:pg16`. Same schema either way.

On a network without IPv6, use the Supabase **Session pooler** (`aws-0-<region>.pooler.supabase.com`, port 5432) instead: the direct host `db.<ref>.supabase.co` resolves to an AAAA record only, so it is unreachable over IPv4.

Conventions: UUID primary keys, `timestamptz`, enums as `TEXT + CHECK` (easy to change mid-hackathon), `jsonb` for flexible specs/context.

### 8.1 Schema (`db/schema.sql`)

```sql
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
  context             JSONB NOT NULL DEFAULT '{}',  -- {"awaiting":"serial_number"|"email","pending":{...}} or {"awaiting":"payment_details","collected":{...}}
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
```

### 8.2 Seed data (`db/seed/seed.py` + `db/seed/data/*.json`, idempotent, also used by "Reset demo")

| Data | Amount | Notes |
|---|---|---|
| Product models | ~25 | 12 laptops, 6 desktops, 7 headphones across 3–4 fictional brands (avoids trademark issues on stage). Use a public laptop-specs CSV for realistic specs if you have one. |
| Product units | ~600 | Several units per model (same model number, unique serials, different colors/configs). Serial format e.g. `AX14-7F3K92`. |
| Customers | ~40 | 4 "demo story" customers with known serials, one per channel. |
| Parts + compatibility | ~47 | Batteries per laptop model, CMOS batteries, RAM sticks, SSDs, chargers, keyboards, displays, fans, ear cushions. Every demo-story device has a part for every service that fits its category. **Test prices:** each part is its real price ÷ 1000, to the paisa (₹5,400 → ₹5.40). |
| Service catalog | 12 | BATTERY_REPLACE, CMOS_REPLACE, DISPLAY_REPLACE, KEYBOARD_REPLACE, FAN_REPLACE (overheating), SSD_REPLACE (failed or corrupted drive), SSD_UPGRADE, RAM_UPGRADE, CHARGER_REPLACE (shipped), OS_REINSTALL (corrupted OS or won't boot, no part), EAR_CUSHION_REPLACE (headphones, shipped), and UPI_TEST: "UPI test payment", labour ₹1.00, no part, no visit, never free under warranty, so `/payments upi test` exercises a real UPI payment end to end for one rupee. Labour is ₹1.00–₹2.00, so every service with any compatible part costs between ₹1 (UPI's minimum) and ₹20 (`tests/test_seed_catalog.py`). |
| Warehouses | 1 | "Central warehouse", Bengaluru. BAT-AX14 has 4 on hand and a reorder threshold of 3 there (`operations.json` overrides may set `reorder_threshold`), so booking the demo battery replacement fires the low-stock alert live and a second booking doesn't repeat it (§7.8). |
| Technicians | 6 | Two each in Bengaluru, Mumbai and Delhi (`staff_users.city`); each city has a technician for every visit skill. |
| Agents / admin | 3 | One login per teammate. |
| Playbooks | ~15 | Battery not charging, no display, overheating, won't boot, BSOD, slow performance, Wi-Fi drops, audio crackle, one earcup dead, keyboard keys, etc. |
| Historical tickets | ~60 | Resolved and open, so search and the inbox look real. |

---

## 9. Events and realtime

In-process async event bus (`app/core/events.py`). Every event is (1) handled by workflows and (2) pushed to connected dashboard clients over `/ws/staff`.

| Event | Emitted by | Handled by |
|---|---|---|
| `message.received` | channel adapters | intake pipeline; dashboard timeline |
| `message.sent` | outbox dispatcher | dashboard timeline |
| `ticket.created` / `ticket.updated` / `ticket.followup` | tickets MCP | dashboard inbox, suggestions refresh |
| `agent.tool_called` | agent runtime (`runtime.py`, tool loops) and workflows (fixed chains) | Agent Activity panel |
| `payment.link_sent` / `payment.utr_submitted` | payments MCP | dashboard (payment badge, timeline) |
| `payment.paid` / `payment.failed` | UPI verifier (`app/payments/upi_verifier.py`, a matched bank alert, or an admin's pasted bank SMS) · payments MCP (`mark_paid_manually`; `reject_payment` for `payment.failed`) | **post-payment workflow** (`payment.paid`); dashboard |
| `job.assigned` / `job.status_changed` / `job.completed` | dispatch MCP | technician portal; dashboard; `job.status_changed` → the on-the-way message and the **cancellation workflow**; `job.completed` → the **completion workflow** (§7.8) |
| `job.rejected` | dispatch MCP (`reject_job`) | the **rejection workflow** (§7.7: another technician, the part still held); technician portal; dashboard |
| `stock.low` | inventory MCP | dashboard (the completion workflow sends the restock email and the admin notification, §7.8) |
| `notification.created` | messaging MCP | bell icon |

The MCP servers are separate processes, so they report events by calling `POST /internal/events` on the backend (shared secret header `X-Internal-Key`).

---

## 10. Backend API

| Method & path | Purpose |
|---|---|
| `GET /api/health` | Liveness + DB check: `{status, db}`, 503 when the DB is unreachable |
| `POST /api/auth/login` · `GET /api/me` | JWT auth |
| `GET /api/tickets` | Inbox with filters (status, priority, channel, category, assignee), sorted by priority then updated |
| `GET /api/tickets/{id}` | Ticket + customer + product + summary + diagnostics + payment + job |
| `GET /api/tickets/{id}/timeline` | Messages + events merged, across all channels |
| `PATCH /api/tickets/{id}` | Status, priority, assignee |
| `DELETE /api/tickets/{id}` | Delete a ticket for good (204). Agents and admins. In one transaction, with the ticket row locked: its messages and their outbox rows, payments and the bank alerts matched to them, closed jobs, `ai_runs`, the notifications linking to it, and (by cascade) its timeline and diagnostics. Stock movements stay, unlinked, so the inventory history still adds up. Its conversations are unlinked with `context` cleared, and deleted when left empty. Customers, products and addresses are never touched. Refused (409) while a job is open (cancel it first, which releases the part) or a payment is `verifying`. Publishes `ticket.updated` (`reason: ticket_deleted`). Not an MCP tool: no model can delete a ticket. Once it is gone, the customer is told by email and on their channel (§7.9); the conversation they are told on is kept |
| `POST /api/tickets/{id}/polish` | Writer preview: `{text}` → `{polished}` |
| `POST /api/tickets/{id}/messages` | Send reply `{text, original?, internal_note?}` |
| `GET /api/tickets/{id}/suggestions` | Suggested action chips (§7.5): `{ticket_id, chips [{name, args, label, needs_args}], source (ai \| rules), cached}`. Agents and admins |
| `PATCH /api/tickets/{id}/diagnostics/{step_id}` | Mark worked / failed / skipped |
| `POST /api/tickets/{id}/commands` | Run slash command `{name, args}`: a §7.5 built-in or one of the signed-in agent's own custom commands (SSE stream of progress: `step`, then `done` or `error`). Agents and admins only; any other name is a 404 |
| `POST /api/payments/{id}/mark-paid` | Admins only (403 for any other role): `{note}` (required, what was checked) → `payments.mark_paid_manually` with the signed-in admin's id → `{payment_id, ticket_id, invoice_number, status, paid_at, verified_by}`. No such payment 404; already paid, cancelled or refunded 409; no note 422; payments server down 503. The ticket page's payment card offers it to admins while the payment is verifying, failed, or needs review (§7.6) |
| `GET /api/payments` | The payments page's list (§11.2), agents and admins (403 for other roles): `?status=&needs_review=&q=&limit=&offset=` → `payments.list_payments` → `{payments [PaymentRow], total, limit, offset, services [{code, name}]}`. A row: invoice, ticket, customer, service, amount, status, UTR, attempts left, `needs_review`, `verified_by` and `verified_by_label` ("bank alert" or the admin's name), created, paid, expiry |
| `GET /api/payments/{id}` | The drawer, agents and admins: the row plus line items, the ticket timeline events about this payment (with the actor's name) and its bank alerts (matched to it, or with its UTR). 404 for none |
| `POST /api/payments` | Admins: "New payment request" `{ticket_id or ticket_number, service_code, note}` for a ticket whose customer has an address on file (else 409 telling the admin to run `/payments`) → `payments.create_payment_request` with the admin's id and note (the amount computed in code, §5.5) → the link on the customer's channel and the invoice email, the same words as `/payments` → 201 `{payment, told_customer_on, emailed_to}`. Refusals: open payment, free under warranty, UPI not set 409; unknown service, no note 422 |
| `PATCH /api/payments/{id}` | Admins: `{utr?, expires_in_minutes?, note}` → `payments.extend_payment` and/or `payments.correct_utr_manually` (then an immediate `match_for_utr`). 422 for nothing to change, no note or not 12 digits; 409 for paid, cancelled, used UTR, no attempts left or not extendable |
| `POST /api/payments/{id}/reject` | Admins: `{note}` (the reason) → `payments.reject_payment`; 409 unless verifying |
| `POST /api/payments/{id}/cancel` | Admins: `{note}` → `payments.cancel_payment` with the admin's id; 409 for paid, refunded or already closed. Nothing is ever deleted |
| `GET /api/bank-alerts` | Agents and admins: every `bank_alerts` row, newest first (at most 200): sender (or the admin who pasted it), UTR, amount, `parsed_ok`, `reject_reason`, and the payment and ticket it matched. Never the SMS text |
| `POST /api/bank-alerts` | Admins: "Add bank SMS" `{sms, sender?, subject?, note}` → `upi_verifier.ingest_manual` (§7.6) → 201 `{bank_alert_id, parsed_ok, reject_reason, utr, amount, match, payment_id, invoice_number}` |
| `POST /api/search` | Natural-language ticket search (§7.4): `{query}` → `{query, interpretation {filters, text, summary, ai}, results [{ticket (an inbox row), why, matched_by}], ranking (rrf \| recency \| ticket_number \| serial), notice}`. Agents and admins; 503 when the tickets server is down |
| `POST /api/copilot` | Dashboard copilot chat (SSE stream, §4.3): `{message, history? [{role, content}], ticket_id?}` → events `servers`, `tool` (each call as it runs), `delta`, then `done {model, tool_calls, hit_limit}` or `error`. Agents and admins. Nothing is stored but the `ai_runs` row; the page keeps the chat and sends the last 8 turns |
| `GET/POST/PATCH/DELETE /api/commands` | Manage custom slash commands. `GET` lists the built-ins, then the signed-in agent's own commands (the composer's `/` menu), with the tools a custom command may list and the template variables. Agents and admins only |
| `GET /api/jobs/mine` | Technician portal: the signed-in technician's jobs, today and upcoming, plus any still open from an earlier day (a job they rejected leaves the list). Each job carries what the portal shows (§7.7): customer name, phone, the address as text with its maps links, device and serial, the issue, the part and where it is reserved, what was tried, `billing` (`paid` / `warranty`) with the invoice, status and notes |
| `GET /api/jobs/{id}` | One job, for its own technician, agents, or admins (403 for anyone else) |
| `POST /api/jobs/{id}/reject` | `{reason}` (3 to 300 characters) → `dispatch.reject_job`. Only the job's own technician (403 for another technician, an agent, a warehouse user or an admin); 409 unless the job is still assigned (also after a concurrent Accept); 422 for a missing or short reason; 404 for no such job; 503 when dispatch is down. Returns `{ok, status: "rejected"}` at once; the hand-over runs in the background (§7.7) |
| `PATCH /api/jobs/{id}` | `{status, notes?}` → `dispatch.update_job_status`. A technician moves only their own job, forward only (403 for another's; 409 for a backward move or a closed job); an admin may cancel and nothing else; other roles 403. Dispatch down 503 |
| `GET /api/inventory` · `GET /api/restock-requests` | Inventory page (§7.8), warehouse staff and admins, read-only: stock per part and warehouse (`on_hand`, `reserved`, `available`, `low`, the open restock request), what each ticket still holds (the sum of its reserve / release / consume movements), parts consumed in the last 7 days by type; and the restock requests, open and ordered first. No route changes a restock request's status |
| `GET /api/pay/{token}` | Public: the invoice, `upi = {upi_id, payee_name, amount, uri}` (`upi://pay?pa=…&pn=…&am=…&cu=INR&tn=<ticket number>`), status (§7.6). Rate-limited per token and IP |
| `POST /api/pay/{token}/utr` | Public: `{utr}`, exactly 12 digits → `payments.submit_utr`, then an immediate match against bank alerts already in → `{ok, status, message, utr_attempts_left}`. 404 / 409 / 410 / 422 / 429 / 503 as in §7.6 |
| `GET /api/pay/{token}/status` | Public: what the pay page polls `{status, message, utr_submitted, utr_attempts_left, paid_at}` |
| `POST /api/chat/session` · `WS /ws/chat/{session_id}` | Website chat widget |
| `WS /ws/staff?token=` | Dashboard realtime |
| `POST /internal/events` | Events from MCP servers |
| `POST /api/dev/reset-demo` | Wipe and re-seed the demo data in one transaction (the same code as `db/seed/seed.py`; embeddings cached in `backend/.cache/seed_embeddings.json`). Dev only, admin login required. Returns `{ok, seconds, phases, rows, demo_customers}` and publishes `ticket.updated` (`reason: demo_reset`) so open dashboards refetch |
| `POST /api/dev/simulate` | Inject a fake inbound message on any channel (backup if a platform is down on stage). Dev only, staff login required; a channel whose adapter isn't running is delivered to a simulated sink that is logged and returned in the response |
| `GET /api/dev/channels` | Which channel adapters are connected in this process (§15 checklist). Dev only |
| `POST /api/dev/simulate-bank-alert` | `{utr, amount, include_secret?, sender?}`: builds a realistic forwarded bank credit SMS (with `BANK_SECRET` unless `include_secret` is false) and runs it through the **same** verifier checks, parser, and matcher as a mail from the inbox (§7.6), so a match sets off the real `payment.paid` workflow. Dev only, staff login required. Returns `{bank_alert_id, parsed_ok, reject_reason, utr, amount, match, payment_id, invoice_number, sender, sms}` with the secret masked |

---

## 11. Frontend

### 11.1 Stack

Next.js (App Router) + TypeScript, Tailwind v4, shadcn/ui (Radix primitives), `next-themes` (light/dark/system), TanStack Query, `cmdk` (⌘K search and `/` command menu), Framer Motion (only where noted), `lucide-react`, Sonner toasts, `openapi-typescript` for API types, `react-markdown` + `remark-gfm` (pinned) to render the copilot's Markdown answers and tables (`components/markdown.tsx`, no raw HTML). No map library: an address is text plus maps links (§7.7).

### 11.2 Pages

| Route | Who | What |
|---|---|---|
| `/` | Public | The welcome screen (`components/welcome/welcome.tsx`), opening on **Customer**: the ServiceMesh logo, "Support that keeps up with your devices.", a sample ticket stub, and a Customer \| Agent switch. Customer: no account; **Start a chat** → `/support`, plus the other channels. Agent: the staff sign-in form, as on `/login` |
| `/login` | Staff | The same welcome screen opening on **Agent**: work email, password (show / hide), **Keep me signed in** (kept: the token in `localStorage`; not kept: `sessionStorage`, gone when the tab closes), **Sign in to dashboard**. Redirects agents → `/inbox`, technicians → `/jobs`, warehouse → `/inventory`. Email and password is the only sign-in: no Google or other provider, no sign-up, no password reset (an admin sets passwords). Escape returns to `/` |
| `/support` | Customer | In the welcome screen's frame: the website chat (§11.4) beside where to find a serial number and the other channels. Public, no account; **Home** or Escape returns to `/`. It calls only `POST /api/chat/session` and `WS /ws/chat/{session_id}`, whose messages run the customer intake pipeline, so payments, dispatch and inventory are out of reach in code (§4.1) |
| `/inbox` | Agent | Search, filters, and the tickets as a grid of passes (§11.3): 3 a row beside the Any Questions sidebar on a laptop, 4 with it closed, 5 on a wide monitor |
| `/tickets/[id]` | Agent | Full ticket: summary, unified timeline, composer with `/` commands and Polish, right rail (customer, device, diagnostics, payment, job, Agent Activity) |
| Any Questions | Agent, admin | The copilot chat with tool activity (§4.3), as a persistent right-hand sidebar in the staff layout (`components/any-questions/`), beside every staff page. Resizable by dragging its left edge (or the arrow keys on it; double-click resets), and toggled by the top-bar button or **Ctrl + .** (⌘ . on a Mac). Ctrl + W can't be used: browsers keep it for closing the tab. Open or closed and the width are remembered per browser; first visit opens it on a screen 1280 px or wider. Below 1024 px it is a sheet over the page. The chat survives moving between pages, not a reload. `/copilot` opens it over the inbox |
| `/commands` | Agent | Create / edit / test custom slash commands |
| `/inventory` | Warehouse, admin | Stock by warehouse, low-stock list, restock requests, usage this week |
| `/payments` | Agent (read-only), admin | Every payment, by hand (§7.6): filters (status, needs review, text), newest first, live from `/ws/staff`; a drawer with line items, the payment's timeline events, its bank alerts and the ticket link; a Bank alerts tab. Admins only: New payment request, Add bank SMS, and in the drawer Mark as paid, Correct UTR, Extend link, Reject and Cancel, each with a note kept with their name. Other roles are refused. In the top bar for agents and admins |
| `/jobs` · `/jobs/[id]` | Technician | Today's jobs, job detail with the address and its maps links, status buttons (mobile-first); while a job is assigned, Reject beside Accept, with a reason; the jobs as passes coloured by status, and the job page headed by one |
| `/pay/[token]` | Customer | Checkout page (§7.6 step 4): invoice, UPI QR, UTR form, live status. Its `/api/pay/*` calls are relative and proxied to the backend by the web server |

### 11.3 Design direction: soft lavender glass, deep indigo, the ticket stub; light and dark

Tokens live in `web/src/styles/tokens.css`. The direction comes from the user's sign-in mockup and runs through every page.

**Principles**

- A soft lavender field behind everything, frosted white frames and white panels on it, deep indigo for the one primary action on a screen.
- One bold element: the **ticket**, as a printed pass. The welcome screen keeps its sample stub (`ticket-stub.tsx`: a gradient top, a tear line, a stub). Inside the app, inbox cards, technician jobs and the ticket and job page headers are **passes** (`ticket/pass.tsx`): the whole card in one colour, the "# SERVICEMESH" line and a date, the reference number large, a perforated tear with punched notches, a lighter lower half with labelled fields, and the person's initials. Gradients appear nowhere else.
- Sentence case everywhere, except the printed parts of a stub or pass (the SERVICEMESH line and date, the pill, the small field labels), which follow the printed-ticket look of the mockups.

**Color tokens**

| Token | Light | Dark | Use |
|---|---|---|---|
| `field` | `#ECE8FA → #E3E7FB → #F4E9F2` | `#15102B → #0F1230 → #1C1027` | The page background (a fixed gradient on `body`) |
| `canvas` | `#F1EFF9` | `#120F24` | Flat stand-in for the field: chips, tracks, notches |
| `surface` / `surface-raised` | `#FFFFFF` / `#FFFFFF` + shadow | `#1B1733` / `#241F42` | Panels, cards, popovers |
| `frost` | white 55% | `#241F42` 55% | Frames, top bar, the Any Questions sidebar (with backdrop blur) |
| `ink` / `ink-secondary` | `#1E1A33` / `#6F6B86` | `#F2F0FA` / `#A9A4C4` | Text |
| `hairline` | `#E5E2EF` | `#2E2850` | Dividers, field borders |
| `accent` / `on-accent` | `#2C1B64` / white | `#B3A6FF` / `#17122E` | Primary buttons, the active tab and nav pill, and the text on them |
| `violet` | `#7461E8` | `#9C8CFF` | Focus rings, links, highlights |
| `success` / `warning` / `danger` | `#15803D` / `#B45309` / `#E11D48` | `#34D399` / `#FBBF24` / `#FB7185` | Status, flags; all pass 4.5:1 as text on `surface` |

The welcome stub's gradients (`--prio-*`): urgent `#E8435A → #F58A86`, high `#EE6A45 → #F6AA78`, medium `#5B4BDB → #9B8AF2`, low `#1F9CB8 → #6CD0BE`. The pass colours (`--pass-*`, both themes): red `#E23E4F → #F48B95`, orange `#EF6418 → #FBAB68`, yellow `#F3BB22 → #FBE184`, green `#1F9E58 → #73D69A`, violet `#5B4BDB → #9B8AF2`, slate `#5F6B80 → #A8B2C3`. A ticket's priority picks its pass: urgent red, high orange, medium yellow, low green. A job's status does: assigned yellow, accepted violet, on the way orange, arrived red, completed green, cancelled slate. The top of a pass carries white words (dark on yellow); its lower half is a lighter tint with ink words, so every detail reads on every colour. The logo is four dots on a white tile: `#5B4BDB`, `#8F7CF2`, `#F0507A`, `#F2646B` (also `app/icon.svg`).

**Type**

- Plus Jakarta Sans for everything, self-hosted at build time by `next/font`. Titles bold with tight letter-spacing (−0.03em to −0.04em on display sizes).
- Scale: 34 / 28 / 22 / 17 (body) / 15 / 13.
- `font-variant-numeric: tabular-nums` for ticket numbers, amounts, stock counts, times.

**Shape and depth**

- Radius hierarchy: 40px outer frames, 32px page panels, 22px cards, 14px fields and buttons, fully rounded pills, tabs and chips.
- Resting cards: a hairline ring and a faint indigo shadow. A hovered ticket stub lifts, with a glow in its own priority colour.
- Top bar: frosted (`frost` + `backdrop-filter: blur(20px)`), the active page as a filled indigo pill.

**Motion**

- One entrance per page: the welcome screen's sample stub settles into place; the inbox stubs rise in, staggered. A ticket that arrives or changes over `/ws/staff` pulses its ring.
- Answering the person: the Customer | Agent pill slides, the card lift on hover, chip press, the sidebar sliding open and closed, the resize handle; tool calls appear one by one in Any Questions and the Agent Activity rail.
- Respect `prefers-reduced-motion`.

**Ticket card** (`components/ticket/ticket-card.tsx`, a pass)

```
┌──────────────────────────────┐
│ # SERVICEMESH         OCT 09 │  the priority's colour, the whole card
│ [URGENT] (device)            │
│ Aurora 14                    │
│ SR-2026-00042                │
◖- - - - - - - - - - - - - - -◗  the tear line
│ Battery not charging         │  title, two lines at most
│ Riya Sharma — Aurora 14, …   │  customer and device
│ CHANNEL   TYPE      STATUS   │
│ Discord   Hardware  Awaiting │
│ [channels] [flags] [+2]      │
│ (RS) Riya Sharma      2m ago │
└──────────────────────────────┘
```

**Any Questions** (`components/any-questions/chat.tsx`): suggested questions as icon chips, always at the top; the conversation in one bordered panel, answers in white bubbles beside a round assistant avatar, the person's questions in indigo on the right; a large question box with a round send button, and "Enter to send / Shift + Enter for a new line" under it.

Channel glyphs stack to show the conversation crossed platforms; `+2` shows follow-ups merged by duplicate detection.

**Ticket detail**

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ServiceMesh      Inbox   Copilot   Inventory   Commands     [⌘K Search]  ◯ │
├──────────────────────────────────────────────────┬───────────────────────────┤
│ Battery not charging                              │ Customer                  │
│ SR-2026-00042        High        Awaiting payment │   Riya Sharma             │
│                                                   │   Discord, Email          │
│ Summary                                           │ Device                    │
│   Aurora 14 won't charge past 0%. Adapter swap    │   Aurora 14, Silver       │
│   didn't help. Battery health reads 0 cycles…     │   AX14-7F3K92, out of wty │
│                                                   │ Diagnostics               │
│ Timeline                                          │   ✓ Tried another adapter │
│   [discord] Riya    Battery stuck at 0%           │   ✗ BIOS battery reset    │
│   [ai]              Asked for serial              │   ○ Battery health test   │
│   [mail]  Riya      Any update?        follow-up  │ Payment                   │
│   [agent] Arjun     Confirmed replacement…        │   Link sent, 58 min left  │
│                                                   │ Agent activity            │
│ [Run diagnostics] [/payments battery] [Summarize] │   ✓ catalog price         │
│ ┌───────────────────────────────────────────────┐ │   ✓ stock check           │
│ │ Reply, or type / for commands                 │ │   ✓ payment link          │
│ │                              [Polish]  [Send] │ │                           │
│ └───────────────────────────────────────────────┘ │                           │
└──────────────────────────────────────────────────┴───────────────────────────┘
```

### 11.4 Website chat widget

A panel on `/support` (`components/chat-widget/chat-panel.tsx`): short pre-chat form (name, email; no account), then chat with typing indicator, message status (sending, delivered once the server echoes it), and the ticket number pinned at the top once created. The session id is kept in the browser (localStorage, best effort), so a reload resumes the conversation; a dropped socket reconnects with backoff, and a session the server no longer knows (close code 1008) starts a new chat. Never silence (§15): intake already turns a model outage into the fallback reply, the socket route sends `FALLBACK_REPLY_NO_TICKET` if intake fails for any other reason and keeps the socket open, and the page shows the same words if no reply has come 45 s after the server confirmed a message. Same visual tokens as the dashboard.

---

## 12. Repository structure

```
servicemesh/
├── ARCHITECTURE.md
├── CLAUDE.md                       # "read ARCHITECTURE.md first; follow names exactly"
├── README.md                       # setup, run, demo script, screenshots
├── Makefile                        # make db-reset, make seed, make mcp, make api, make web, make types, make llm-check
├── docker-compose.yml              # optional local Postgres (pgvector/pgvector:pg16)
├── db/
│   ├── schema.sql
│   ├── apply_schema.py             # drop app tables, apply schema.sql, verify tables (make db-reset)
│   └── seed/  (seed.py, data/*.json)
├── backend/
│   ├── pyproject.toml              # uv
│   ├── .env.example                # §13.2 — copy to backend/.env (backend + MCP servers read it)
│   ├── app/
│   │   ├── main.py                 # FastAPI app, lifespan starts bots, email poller, outbox dispatcher, MCP hub, UPI verifier
│   │   ├── core/                   # config.py (pydantic-settings), db.py, security.py, events.py, logging.py
│   │   ├── models/                 # SQLAlchemy models (mirror schema.sql)
│   │   ├── schemas/                # Pydantic request/response models (source of frontend types)
│   │   ├── api/                    # health, auth, tickets, search, copilot, commands, jobs, inventory, payments, pay, ws, dev, internal
│   │   ├── brain/
│   │   │   ├── llm.py              # the only module that talks to a text model: Groq → Ollama fallback (§4.6)
│   │   │   ├── decide.py           # typed decisions (Jev or LLM), redact, extract_serial, intake presets
│   │   │   ├── router.py           # ROLE_SERVERS, pick_servers, filter_tools, compact_tools
│   │   │   ├── mcp_hub.py          # MCP client: connect, list_tools, call_tool
│   │   │   ├── runtime.py          # OpenAI-format tool loop + ai_runs logging
│   │   │   ├── intake.py           # guided intake state machine
│   │   │   ├── writer.py           # polish
│   │   │   ├── search.py           # NL → filters
│   │   │   ├── suggestions.py
│   │   │   ├── commands.py         # slash command templating + execution
│   │   │   ├── workflows.py        # payment.paid, job.completed, stock.low chains
│   │   │   ├── embeddings.py       # fastembed
│   │   │   └── prompts/            # *.md system prompts
│   │   ├── channels/               # base.py, discord_bot.py, telegram_bot.py, email_channel.py, web_chat.py, dispatcher.py, identity.py
│   │   ├── payments/               # UPI (§7.6): money.py, details.py (booking details), invoice.py, receipt_pdf.py (the PDF receipt), upi_verifier.py (bank alerts → payment.paid)
│   │   ├── assets/                 # logo.png (from web/src/app/icon.svg) and fonts/ (DejaVu Sans + its licence) for the PDF receipt
│   │   └── templates/email/        # payment_link, payment_confirmed, job_assigned, visit_scheduled, restock_alert, ticket_created, ticket_resolved, ticket_deleted (.html + .txt)
│   ├── mcp_servers/
│   │   ├── common/                 # db.py, events.py (POST /internal/events)
│   │   ├── tickets_server.py       # :8101
│   │   ├── catalog_server.py       # :8102
│   │   ├── knowledge_server.py     # :8103
│   │   ├── messaging_server.py     # :8104
│   │   ├── payments_server.py      # :8105
│   │   ├── dispatch_server.py      # :8106
│   │   ├── inventory_server.py     # :8107
│   │   └── run_all.py
│   └── tests/                      # smoke tests: each MCP tool, intake happy path, UPI bank-alert parsing and matching
└── web/
    ├── package.json                # pnpm
    ├── .env.local.example          # §13.3 — copy to web/.env.local
    ├── src/app/                    # routes from §11.2
    ├── src/components/             # ui/ (shadcn), ticket/, timeline/, composer/, activity-rail/, chat-widget/
    ├── src/lib/                    # api client, ws client, generated api-types.ts
    └── src/styles/tokens.css       # color + type tokens from §11.3
```

---

## 13. Environment variables and keys

### 13.1 Keys you need to get (all free)

| Key | Where | Steps |
|---|---|---|
| `GROQ_API_KEY` | console.groq.com | API Keys → Create API Key. Free, no card. Limits are per organization, so teammates can share one org or use their own. Check them with `make llm-check`. |
| `JEV_API_KEY` (optional) | The free gateway's dashboard (BeatAPI free route) | Create a key there, and copy the gateway's base URL, path, and model id into `JEV_BASE_URL`, `JEV_PATH`, `JEV_MODEL`. Only needed with `DECISION_PROVIDER=jev`. |
| Ollama (optional, no key) | ollama.com, on the 16GB laptop | Install it, run `ollama pull qwen2.5:7b`, set the environment variable `OLLAMA_HOST=0.0.0.0` and restart Ollama, and allow inbound TCP port 11434 in Windows Firewall. Teammates then set `OLLAMA_BASE_URL=http://<that-laptop-ip>:11434/v1`. |
| `DISCORD_BOT_TOKEN`, `DISCORD_APPLICATION_ID` | discord.com/developers/applications | New Application → Bot → Reset Token. Enable **Message Content Intent**. OAuth2 → URL Generator → scopes `bot`, `applications.commands`; permissions Send Messages, Read Message History, Create Public Threads, Send Messages in Threads, Attach Files → invite to your test server. |
| `DISCORD_GUILD_ID`, `DISCORD_SUPPORT_CHANNEL_ID` | Discord app | User Settings → Advanced → Developer Mode on, then right-click server / channel → Copy ID. |
| `TELEGRAM_BOT_TOKEN` | Telegram, chat with @BotFather | `/newbot` → name → username ending in `bot` → copy token. Optional: `/setdescription`, `/setuserpic`. |
| `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD` | A **new** Gmail account for the project | Turn on 2-Step Verification → search "App passwords" in the Google account → create one → 16-character password. IMAP is on by default in current Gmail. |
| `DATABASE_URL` | supabase.com (optional) | New project → Project Settings → Database → connection string (direct, port 5432). Enable the `vector` extension under Database → Extensions. |
| `UPI_ID`, `UPI_PAYEE_NAME` | The company's UPI app (merchant or personal account) | The UPI ID (VPA, e.g. `aurora-devices@okaxis`) the money should reach, and the name UPI apps show for it. Use an account whose bank sends an SMS for every credit. |
| `BANK_SECRET`, `BANK_ALERT_FROM` | You choose them | `BANK_SECRET`: a random passcode (`uv run python -c "import secrets; print(secrets.token_urlsafe(12))"`). `BANK_ALERT_FROM`: the email address the phone forwards from. Then set up the phone that receives the bank's SMS (§7.6): forward each credit SMS to `EMAIL_ADDRESS` with subject `BANK_ALERT_SUBJECT` and `BANK_SECRET` at the end of the body. |

Nothing needs a paid key: the LLMs are free tiers (§4.6), embeddings are local, there are no maps or geocoding (an address is text plus maps links), payments are UPI to the company's own account, verified against its bank's SMS.

### 13.2 `backend/.env.example`

```bash
# ---------- App ----------
APP_ENV=development
APP_NAME=ServiceMesh
COMPANY_NAME="Aurora Devices"
# Printed on the PDF receipt (§7.6) under the company name; any left empty is left off the PDF.
COMPANY_ADDRESS=
COMPANY_PHONE=
COMPANY_EMAIL=
COMPANY_GSTIN=
FRONTEND_URL=http://localhost:3000
BACKEND_URL=http://127.0.0.1:8000
CORS_ORIGINS=http://localhost:3000
JWT_SECRET=                         # openssl rand -hex 32
JWT_EXPIRE_MINUTES=720
INTERNAL_API_KEY=                   # openssl rand -hex 32 — MCP servers → /internal/events
# Shared demo password for every seeded staff login (db/seed). Seeding refuses to run if empty.
SEED_STAFF_PASSWORD=

# ---------- Database ----------
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/servicemesh
# Supabase: postgresql+asyncpg://postgres:<PASSWORD>@db.<PROJECT_REF>.supabase.co:5432/postgres

# ---------- LLM (free tiers only) ----------
# Text + tool loops: groq | ollama
LLM_PROVIDER=groq
# Used on 429 / timeout / connection error: ollama, or empty for none
LLM_FALLBACK_PROVIDER=ollama
GROQ_API_KEY=
GROQ_BASE_URL=https://api.groq.com/openai/v1
MODEL_FAST=qwen/qwen3.8-27b
MODEL_SMART=openai/gpt-oss-20b
# Reasoning controls, per model family (Groq rejects the wrong value for a family).
# openai/gpt-oss-*: low | medium | high, or empty
GROQ_REASONING_EFFORT=low
# qwen/qwen3*: none (no reasoning tokens) | default | low | medium | high, or empty
GROQ_QWEN_REASONING_EFFORT=none
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_TIMEOUT_SECONDS=60
LLM_MAX_TOKENS_FAST=300
LLM_MAX_TOKENS_SMART=800
AI_MAX_TOOL_ITERATIONS=6
AI_TIMEOUT_SECONDS=15

# ---------- Decisions (Jev, optional) ----------
# jev | llm  (llm = MODEL_FAST in JSON mode; the default, needs no Jev key)
DECISION_PROVIDER=llm
JEV_API_KEY=
# Fill these from the gateway dashboard (BeatAPI free route). The defaults point at TypeSafe's direct API, which is paid.
JEV_BASE_URL=https://api.typesafe.ai
JEV_PATH=/v1/systemone
JEV_MODEL=jev-latest
JEV_TIMEOUT_SECONDS=3
DECISION_MIN_CONFIDENCE=0.6

# ---------- Embeddings / search ----------
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
EMBEDDING_DIM=384
DUPLICATE_SIMILARITY_THRESHOLD=0.82
DUPLICATE_LOOKBACK_DAYS=30

# ---------- Channel switches (only the demo host sets these true) ----------
ENABLE_DISCORD=true
ENABLE_TELEGRAM=true
ENABLE_EMAIL=true

# ---------- Discord ----------
DISCORD_BOT_TOKEN=
DISCORD_APPLICATION_ID=
DISCORD_GUILD_ID=
DISCORD_SUPPORT_CHANNEL_ID=

# ---------- Telegram ----------
TELEGRAM_BOT_TOKEN=

# ---------- Email (Gmail) ----------
EMAIL_ADDRESS=
EMAIL_APP_PASSWORD=
EMAIL_FROM_NAME="Aurora Support"
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
EMAIL_POLL_SECONDS=10
# Development only: every outgoing email goes here instead, with the real recipient in the subject.
# Empty sends to the real recipient. Ignored unless APP_ENV=development.
EMAIL_REDIRECT_TO=

# ---------- Payments (UPI QR + UTR, verified against bank credit alerts, §7.6) ----------
# The provider (upi_utr) and currency (INR) are fixed in code. The link is FRONTEND_URL/pay/<token>.
PAYMENT_LINK_TTL_MINUTES=60
# The UPI ID (VPA) customers pay, e.g. aurora-devices@okaxis, and the name UPI apps show for it.
UPI_ID=
UPI_PAYEE_NAME=
# Subject of the bank SMS the phone forwards to EMAIL_ADDRESS. Every mail with it goes to the verifier, never to intake.
BANK_ALERT_SUBJECT=UPI-Verify
# Comma-separated allowlist of the address(es) the phone forwards from.
BANK_ALERT_FROM=
# A random passcode the phone appends to every forwarded alert; an alert without it is rejected.
BANK_SECRET=
BANK_POLL_SECONDS=10
# How long a submitted UTR waits for its bank alert before an admin is asked to review it.
PAYMENT_VERIFY_TIMEOUT_MINUTES=15

# ---------- Operations ----------
WAREHOUSE_ALERT_EMAIL=              # where restock alerts go (a teammate's inbox for the demo)
MAX_JOBS_PER_TECH_PER_DAY=4

# ---------- MCP servers ----------
# 127.0.0.1, not localhost: see the loopback note in 4.5. Keep the IP.
MCP_TICKETS_URL=http://127.0.0.1:8101/mcp
MCP_CATALOG_URL=http://127.0.0.1:8102/mcp
MCP_KNOWLEDGE_URL=http://127.0.0.1:8103/mcp
MCP_MESSAGING_URL=http://127.0.0.1:8104/mcp
MCP_PAYMENTS_URL=http://127.0.0.1:8105/mcp
MCP_DISPATCH_URL=http://127.0.0.1:8106/mcp
MCP_INVENTORY_URL=http://127.0.0.1:8107/mcp
```

### 13.3 `web/.env.local.example`

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_WS_URL=ws://localhost:8000
NEXT_PUBLIC_COMPANY_NAME="Aurora Devices"
```

`NEXT_PUBLIC_API_URL` is also where the web server proxies `/api/pay/*` (`web/next.config.ts`, §7.6); the server fetches it itself, so `localhost` there becomes `127.0.0.1` (§4.5).

`.env` and `.env.local` go in `.gitignore`. The public repo only ever contains the `.example` files. Run a secret scan before every push (`git diff --cached | grep -iE "gsk_|sk-|api_key|token"` at minimum).

---

## 14. Build plan (24 hours, 3 people, 4–5 Claude sessions)

### 14.1 Roles

| Person | Owns |
|---|---|
| **P1 — Brain & backend** | FastAPI core, DB + seed, MCP hub, runtime, intake, writer, search, commands, workflows |
| **P2 — MCP servers & channels** | All 7 MCP servers, Discord, Telegram, Email, web chat socket, outbox dispatcher, email templates |
| **P3 — Frontend & design** | Design tokens, inbox, ticket detail, composer, activity rail, chat widget, technician portal, checkout, inventory, commands page |

Build with **Claude Code** in the repo so each session can read `ARCHITECTURE.md` and run the code. Give each session one clear goal; your Pro usage resets over time, so spread heavy sessions across the 24 hours.

### 14.2 Phases and checkpoints

Every checkpoint must be a working demo on the demo host **and pushed to the public repo**, since one of the three evaluations is online (repo + video).

**Phase 1 (hours 0–6) → Checkpoint 1: "A message becomes a ticket"**
- Session 1: repo scaffold, `schema.sql`, seed, FastAPI skeleton + auth, Next.js skeleton + tokens + login, `.env.example`, Makefile.
- Session 2: tickets / catalog / knowledge / messaging MCP servers, MCP hub, intake pipeline (serial ask + create), web chat + Telegram adapters, basic inbox list.
- Demo: message on Telegram or web chat → AI asks for serial → ticket with AI summary appears live in the inbox → reply arrives on Telegram.

**Phase 2 (hours 6–12) → Checkpoint 2: "Every channel, one inbox"**
- Session 3: Discord + Email adapters, outbox dispatcher, duplicate detection, ticket detail page, unified timeline, composer with Polish preview, realtime updates, diagnostics panel.
- Demo: same customer complains on Discord then emails → one ticket with a +1 follow-up and raised priority; agent's rough note goes out polished on the customer's own channel.

**Phase 3 (hours 12–18) → Checkpoint 3: "Nothing manual"**
- Session 4: payments / dispatch / inventory MCP servers, `/payments` command, pay page + UPI verifier, post-payment workflow, technician portal, restock alert, Agent Activity rail.
- Demo: `/payments battery replacement` → customer gives details → pays → technician notified, part reserved, customer gets chat + email confirmation → technician completes → stock drops → restock email fires.

**Phase 4 (hours 18–24) → Final**
- Session 5: natural-language search, suggested chips, custom slash commands page, copilot page, inventory page, empty/error states, dark mode pass, README with setup + screenshots, demo video, reset-demo polish.

**Cut order if behind** (last in, first out): copilot page → inventory page UI (keep the email alert) → custom command editor (keep built-in commands).

### 14.3 Contracts to lock in the first 2 hours

So three people can work in parallel without breaking each other's code:

1. `db/schema.sql` (§8.1) — merged first.
2. MCP tool names and argument shapes (§5) — P2 builds, P1 consumes.
3. Pydantic response schemas → `make types` regenerates `web/src/lib/api-types.ts` — P1 builds, P3 consumes.
4. Event names (§9) and WebSocket message shape `{type, data, ts}`.
5. Until real endpoints exist, P3 uses mock JSON matching the Pydantic schemas.

---

## 15. Demo safety checklist

- [ ] `POST /api/dev/reset-demo` restores the demo story in under 5 seconds.
- [ ] Demo story scripted: 4 customers, 1 per channel, with known serials.
- [ ] `POST /api/dev/simulate` can inject any channel's message if a platform or venue Wi-Fi misbehaves.
- [ ] LLM calls have timeouts and a friendly fallback reply ("We've received your message and created a ticket; an agent will follow up.") so the customer never gets silence.
- [ ] Bank-alert matching is idempotent (one row per mail, a payment pays once); workflows are safe to re-run.
- [ ] `POST /api/dev/simulate-bank-alert` stands in for the bank's SMS if the phone or the SMS is slow on stage; it uses the real verifier.
- [ ] `EMAIL_REDIRECT_TO` set on the demo host, so every invoice and receipt lands in the team's inbox (with the real recipient in the subject).
- [ ] Only the demo host runs the bots (`ENABLE_*` flags).
- [ ] Embedding model pre-downloaded; `pnpm build` tested once; no console errors in the dashboard.
- [ ] Recorded backup video of the full flow.
- [ ] Groq limits checked with `make llm-check`; `ai_runs` shows which provider served each call.
- [ ] No secrets in git history.

---

## 16. Open items

1. ~~**Payment design**~~ — resolved 2026-10-02: UPI QR + UTR, verified against the bank's forwarded credit SMS (§7.6), replacing the ServicePay mock. The `/pay/[token]` page (§11.2) and the post-payment dispatch, stock and technician backend (§7.7, §7.8, job API) are built, and so are the technician portal (`/jobs`, `/jobs/[id]`), `/payments` from the composer's `/` menu, and the Agent Activity rail.
2. **Company identity** — final name, logo, brands, and currency for seed data and emails.
3. **Phase 4 built 2026-10-04:** the remaining built-in commands, custom commands and `/commands`, natural-language search (⌘K and the inbox), suggested chips, `/inventory`, one loading / error / empty pattern on every staff page, and `/copilot` (chat in the page only, not stored). Not built: marking a restock request ordered or received (no route), a copilot linked to the ticket page (`ticket_id` is accepted by the API but no page sends it).
4. **Evaluation order** — which checkpoint is online, so the repo README and video are ready for that one.
5. **Jev free route** — confirm the gateway base URL, model id, and rate limits before setting `DECISION_PROVIDER=jev`; send only seeded demo data through it.