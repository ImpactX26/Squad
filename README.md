# ServiceMesh — Companion for Company

AI-assisted, omnichannel after-sales support for a laptop / PC / headphones company. The design, names, and folder structure are fixed in [ARCHITECTURE.md](ARCHITECTURE.md); read it before writing code.

## Status

Built (Phase 1, session 1 of [§14.2](ARCHITECTURE.md#142-phases-and-checkpoints)):

- `db/schema.sql` (§8.1) and an idempotent seed (§8.2): 25 models, 600 units, 40 customers, 47 parts (test prices), 12 services, one central warehouse, 16 playbooks, 60 historical tickets with embeddings.
- `db/apply_schema.py` applies the schema and verifies every table, extension, and sequence exists.
- FastAPI skeleton with config from `backend/.env`, SQLAlchemy models mirroring the schema, and:
  `GET /api/health`, `POST /api/auth/login`, `GET /api/me`, `WS /ws/staff?token=` (live §9 events), and `POST /internal/events` (`X-Internal-Key`).
- Next.js app with the §11.3 design tokens, a light / dark / system toggle, shadcn/ui, a `/login` that redirects by role, and the staff shell (translucent top bar, role-based nav, user menu) on `/inbox`.
- The brain's provider layer ([§4.6](ARCHITECTURE.md#46-providers-and-fallback)), free tiers only: `llm.py` (Groq with Ollama fallback, JSON mode, streaming, `make llm-check`), `decide.py` (typed decisions through Jev or the LLM, redaction, serial extraction, intake presets), `router.py` (per-role MCP server routing and tool trimming), and `runtime.py` (the OpenAI-format tool loop with `ai_runs` logging). Covered by tests with mocked HTTP; not yet run against live Groq.

Since then: all 7 MCP servers, intake on every channel, duplicate detection, the inbox and ticket page, UPI payments (below), and the dispatch and stock backend: the booking chain after `payment.paid` (or a free warranty repair), the technician job API, completion, cancellation and the restock alert; on the web, `/payments` from the ticket composer's "/" menu, the Agent Activity rail, and the technician portal (`/jobs`, `/jobs/[id]`, mobile-first). Also built: all eight slash commands and the agents' own custom commands (`/commands`), natural-language search (⌘K and the inbox), suggested action chips on the ticket page, the inventory page (`/inventory`, with its low-stock alert and restock requests), and the copilot (`/copilot`). Added 2026-10-06: the home page with two role choices and the redesigned login, the customers' website chat on `/support`, copilot answers to general questions (ticket counts, the warehouse, technicians) as Markdown tables, search by device serial, the low-stock alert when a booking takes a part to its threshold, and a chat message when a payment link expires unpaid. Not built yet: copilot chat history (the chat lives in the page and is lost on reload), marking restock requests ordered or received, and a warehouse login in the seed (the admin login sees `/inventory`).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for you)
- Node 24 and pnpm 12.8.1 (`npm install -g pnpm@12.8.1`)
- A Supabase project with the `vector` extension enabled, or Docker for `docker-compose.yml`
- `make` is optional; on Windows, run the line under each Makefile target in Git Bash

## Setup

```bash
# Backend
cd backend
uv sync
cp .env.example .env
```

Edit `backend/.env`:

| Key | Value |
|---|---|
| `JWT_SECRET`, `INTERNAL_API_KEY` | Each a fresh random value: `uv run python -c "import secrets; print(secrets.token_hex(32))"` |
| `UPI_ID`, `UPI_PAYEE_NAME`, `BANK_ALERT_FROM`, `BANK_SECRET` | Only for taking real UPI payments; see [§7.6](ARCHITECTURE.md#76-payments-end-to-end) and [§13.1](ARCHITECTURE.md#131-keys-you-need-to-get-all-free). The rest of the app runs with them empty. |
| `SEED_STAFF_PASSWORD` | The shared demo password for all seeded staff logins |
| `DATABASE_URL` | Supabase → Connect → **Direct connection** (port 5432), with the scheme changed to `postgresql+asyncpg://`. The direct host is IPv6-only on the free tier; if your network has no IPv6, use the **Session pooler** string (also port 5432) instead. |
| `GROQ_API_KEY` | **Required for AI features.** console.groq.com → API Keys → Create API Key (free, no card). Check it with `make llm-check`. |
| `JEV_*` | Optional, only for `DECISION_PROVIDER=jev`: key, base URL, path, and model id from the free gateway's dashboard. The default `DECISION_PROVIDER=llm` needs none of them. |
| `OLLAMA_*` | Optional fallback when Groq rate-limits. On the 16GB laptop: install Ollama, `ollama pull qwen2.5:7b`, set `OLLAMA_HOST=0.0.0.0`, allow port 11434 in Windows Firewall. Everyone else sets `OLLAMA_BASE_URL=http://<that-laptop-ip>:11434/v1`. Leave `LLM_FALLBACK_PROVIDER` empty to run without it. |

The app calls free tiers only (Groq, optional Jev, optional Ollama); see [§4.6](ARCHITECTURE.md#46-providers-and-fallback).

Don't leave the inline `# comment` text after an empty value: `KEY=   # comment` makes the comment the value. The backend refuses to start if any value begins with `#`.

Keep `BACKEND_URL` and every `MCP_*_URL` on `127.0.0.1`, not `localhost`: on a network without IPv6 the name resolves to `::1` first and each connection stalls before falling back to IPv4 ([§4.5](ARCHITECTURE.md#45-speed-and-free-tier-limits)). `web/.env.local` keeps `localhost`, because those `NEXT_PUBLIC_*` URLs are fetched by the browser, not by the Next.js server.

> **After pulling the switch to free LLM providers:** copy the `# ---------- LLM (free tiers only) ----------` and `# ---------- Decisions (Jev, optional) ----------` blocks from `backend/.env.example` into your `backend/.env` (replacing the old Anthropic block), then set `GROQ_API_KEY`. Settings requires every key, so the backend and the tests won't start until the new keys are there. A leftover `ANTHROPIC_API_KEY` line is ignored.

```bash
# Database (from the repo root) — drops all tables, applies the schema and verifies it, then seeds
make db-reset
make seed

# Web
cd web
pnpm install
cp .env.local.example .env.local
```

## Run

```bash
make api    # http://localhost:8000  (OpenAPI docs at /docs)
make web    # http://localhost:3000
```

Open http://localhost:3000: the home page offers two roles. **Go as customer** opens `/support`, the website chat (a name and an email, no account). **Login as service agent** opens `/login`. Escape on either page goes back home. Run `make mcp` too, or the chat can only answer with the fallback reply.

Sign in at http://localhost:3000/login with `SEED_STAFF_PASSWORD` and one of:

| Email | Role |
|---|---|
| `arjun.mehta@staff.example.com` | agent |
| `neha.kapoor@staff.example.com` | agent |
| `kabir.rao@staff.example.com` | admin |
| `ravi.kumar@staff.example.com` (and 5 more in `db/seed/data/operations.json`) | technician |

## The ticket page (Checkpoint 2)

`make api`, `make mcp` and `make web` (three terminals), then sign in. The inbox rows follow the §11.3 ticket row and open `/tickets/[id]`: title bar, AI summary, one timeline across every channel, the composer (Polish shows your note and the rewrite side by side; nothing polished is sent unseen), and a right rail with customer, device, diagnostics, payment and job. Payment fills in once `/payments` has run; job stays empty until dispatch is built. Diagnostic buttons save through `PATCH /api/tickets/{id}/diagnostics/{step_id}`. Copilot, Commands and Inventory show as inert text in the top bar until their pages exist.

To replay the story without a Discord bot: `POST /api/dev/simulate` with Riya on `discord` (serial `AX14-7F3K92`), then on `email` with `riya.sharma@example.com`. Both land on one ticket with `+1`.

## Payments (UPI QR + UTR, Checkpoint 3 part 1)

The customer pays the company's UPI ID from a QR code, submits the 12-digit UTR, and the backend matches it against the bank's credit SMS, which the company phone forwards into the project Gmail ([§7.6](ARCHITECTURE.md#76-payments-end-to-end)). No gateway, no webhook.

Built: the payments MCP server (:8105), `/payments` (`POST /api/tickets/{id}/commands`, SSE), intake's booking-details slot, the invoice and receipt emails, the public pay API (`/api/pay/{token}`, `/utr`, `/status`), the UPI verifier, the `payment.paid` receipt workflow, the customer's `/pay/[token]` page (invoice, UPI QR, UTR form, live status), and an admin's **Mark as paid** on the ticket's payment card (`POST /api/payments/{id}/mark-paid`). After `payment.paid` the booking runs (next section).

Replay it without a bank, a phone, or a bot (API on :8000, MCP servers running, a staff token in `$T`):

```bash
# 1. Riya reports the battery on Discord -> a ticket
curl -s -X POST localhost:8000/api/dev/simulate -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"channel":"discord","external_user_id":"riya-discord","text":"My Aurora 14 (AX14-7F3K92) battery is stuck at 0%"}'
# 2. The agent runs /payments on that ticket (streams step events, then done). With no words
#    ("args":"") it picks the service from the ticket; "args":"battery replacement" names it.
curl -sN -X POST localhost:8000/api/tickets/<ticket_id>/commands -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" -d '{"name":"payments","args":""}'
# 3. Riya sends her details, over as many messages as she likes (same simulate call, new text)
# 4. The last one returns "payment": {"pay_url": ".../pay/<token>", ...}; then:
curl -s localhost:8000/api/pay/<token>
curl -s -X POST localhost:8000/api/pay/<token>/utr -H "Content-Type: application/json" -d '{"utr":"427512345678"}'
curl -s -X POST localhost:8000/api/dev/simulate-bank-alert -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" -d '{"utr":"427512345678","amount":"6.90"}'
curl -s localhost:8000/api/pay/<token>/status      # paid
```

The seed uses **test prices**: every part is its real price ÷ 1000 and labour is ₹1.00–₹2.00, so every invoice is between ₹1 (UPI's minimum) and ₹20. Riya's battery replacement is ₹6.90 (battery ₹5.40 + labour ₹1.50). For the smallest real payment, run `/payments upi test`: the seeded `UPI_TEST` service ("UPI test payment") is a ₹1.00 labour-only invoice with no part, and a warranty never makes it free.

Warranty decides the price: on a device in warranty, a failed part and an OS reinstall are free (no invoice), while RAM and SSD upgrades stay paid.

`UPI_ID`, `UPI_PAYEE_NAME`, `BANK_ALERT_FROM` and `BANK_SECRET` must be set for steps 2 and 4 (any well-formed values work for the replay). Set `EMAIL_REDIRECT_TO` so the invoice and receipt land in your own inbox. Re-seed afterwards (`POST /api/dev/reset-demo`).

Open the `pay_url` in a browser to see the page: it shows the QR while pending, "Checking with the bank" after the UTR, and turns to "Payment received" on its own (it polls every 3 s) once the bank alert matches.

### Paying from a real phone

The pay page calls `/api/pay/*` on the web server, which proxies them to the API (`web/next.config.ts`), so one tunnel to port 3000 is all a phone needs. Use the ₹1 `UPI_TEST` service.

1. Install the tunnel once (PowerShell): `winget install --id Cloudflare.cloudflared`, then open a new terminal.
2. In `backend/.env` set `UPI_ID` (the VPA that should receive the money) and `UPI_PAYEE_NAME` (the name UPI apps show for it). For automatic verification also set `BANK_ALERT_FROM` and `BANK_SECRET` and do the forwarding setup below; without it, a submitted UTR waits, gets "needs review" after `PAYMENT_VERIFY_TIMEOUT_MINUTES`, and an admin confirms it with **Mark as paid**.
3. Start `make mcp`, `make web`, then the tunnel: `cloudflared tunnel --url http://localhost:3000`. It prints `https://<random-words>.trycloudflare.com`.
4. Set `FRONTEND_URL` in `backend/.env` to that URL (no trailing slash) and start, or restart, `make api` (settings are read at startup). Restart `make mcp` too if you want its tool results to show the same link.
5. On the dashboard (on the laptop, `http://localhost:3000`, signed in as an agent or admin), open a ticket with a verified device, type `/payments upi test` in the composer and press Enter; the steps stream in under the composer and end with "Asked the customer for their details". Send the customer details from the customer's channel (or `POST /api/dev/simulate`). The chat reply has `https://<random-words>.trycloudflare.com/pay/<token>` for ₹1.00.
6. Open that link on your phone, or on the laptop and scan the QR with your phone. Pay ₹1 **from a different bank account** than the one behind `UPI_ID` (a payment to yourself may never produce a credit SMS).
7. In your UPI app's payment details, copy the 12-digit UTR (UPI Ref No.) and submit it on the page. It shows "Checking with the bank" and turns to "Payment received" when the forwarded credit SMS matches, or stays checking until an admin marks it paid.

The tunnel URL changes every time `cloudflared` restarts; update `FRONTEND_URL` and restart the API each time. Anyone with the link can open it while the tunnel runs.

### Forwarding the bank's credit SMS (done by hand, once)

The verifier reads these mails from `EMAIL_ADDRESS` (§7.6 step 5). On the phone that receives the bank's SMS for the `UPI_ID` account:

1. Pick `BANK_SECRET` (`uv run python -c "import secrets; print(secrets.token_urlsafe(12))"`) and the address the phone will send from; put that address in `BANK_ALERT_FROM`.
2. Android: install an SMS forwarder app that can email (e.g. "SMS Forwarder" style apps), add a rule for the bank's sender ID (or messages containing "credited"), send to `EMAIL_ADDRESS` with subject exactly `BANK_ALERT_SUBJECT` (`UPI-Verify`) and the SMS text followed by `BANK_SECRET` in the body. iPhone: a Shortcuts personal automation on "Message received from <bank>" that sends an email with that subject and body (the Mail app must have the `BANK_ALERT_FROM` account).
3. Make sure the sending account's mail passes SPF/DKIM (Gmail, Outlook and iCloud do); the verifier refuses alerts whose `From` fails them.
4. Test it: forward one real credit SMS, then check the API log for `bank alert` lines, or `POST /api/dev/simulate-bank-alert` with `include_secret: false` to see what a refused one looks like.

## Dispatch and stock (Checkpoint 3 part 2)

After the receipt, `payment.paid` books the repair ([§7.7](ARCHITECTURE.md#77-dispatch-and-technician-flow)): the part is reserved at the central warehouse, the technician with the least work in the customer's city gets the next working day (Monday to Saturday), both are emailed, and the customer is told on their channel. A free warranty repair (`/payments` on an in-warranty device) books the same way as soon as the customer's details are in, with no invoice. Shipped services (charger, ear cushions) are reserved and the admins are asked to ship them. With no stock or no technician within 6 working days there is no job: the admins are told why and the customer is told an agent will confirm the date. There are no maps: the address is text, plus the customer's own Google / Apple Maps link if they pasted one, or search links built from the text.

The technician signs in (they land on `/jobs`), opens the job, calls the customer from it, and taps the one big button: Accept → On the way → Arrived → Complete (with a note). The same through the job API, with a technician's token in `$TECH`:

```bash
curl -s localhost:8000/api/jobs/mine -H "Authorization: Bearer $TECH"
curl -s -X PATCH localhost:8000/api/jobs/<job_id> -H "Authorization: Bearer $TECH" -H "Content-Type: application/json" \
  -d '{"status":"accepted"}'                       # then en_route, on_site, and completed with "notes"
```

`en_route` and `completed` message the customer; `completed` uses the part, resolves the ticket, and, when the part drops to its reorder threshold, files one restock request and emails `restock_alert` to `WAREHOUSE_ALERT_EMAIL` (set it, or the alert is only a dashboard notification). An admin may cancel a job (`{"status":"cancelled"}`), which releases its part.

## Other commands

```bash
make db-reset                   # drop all tables, re-apply db/schema.sql, verify every table exists
make seed                       # wipe all rows and re-seed (same ids every run)
# POST /api/dev/reset-demo (APP_ENV=development, admin login) runs the same fast re-seed from the running API and returns the time taken; open dashboards refetch.
make types                      # regenerate web/src/lib/api-types.ts from the FastAPI schema
make llm-check                  # one tiny request per LLM provider: latency + Groq's remaining limits (no keys printed)
cd backend && uv run pytest -rs # tests; DB tests skip with the reason if the DB is unreachable
```

`make seed` and `make db-reset` delete all data in the database they point at, including a shared team database.

## Demo story customers

Seeded with known serials and no open tickets, one per channel (`db/seed/data/customers.json`):

| Customer | Channel | Device | Serial | Warranty |
|---|---|---|---|---|
| Riya Sharma | Discord | Aurora 14, Silver | `AX14-7F3K92` | Expired → paid battery replacement |
| Aman Verma | Telegram | Vertex 15, Black | `VX15-Q8M2D5` | Active |
| Sneha Iyer | Email | Pulse ANC 700, Midnight | `PA7-3KX9TB` | Active |
| Rahul Gupta | Web chat | Lumen Book 13, Silver | `LB13-W4N7PC` | Active |

Customer emails use `example.com` so seeding never emails a real person. Point a demo customer at a teammate's inbox in your local copy if you need real email.