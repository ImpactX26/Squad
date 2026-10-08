# Progress

One entry per jury round (ARCHITECTURE.md §14.4): what was shown, what the jury said, what comes next.

## Round 1 · Block 1: "A message becomes a ticket" (`v0.1-jury1`)

### What Block 1 delivered

**P1 · Brain & backend**

- The whole schema (§8.1) with `apply_schema.py`, and the demo seed: staff logins, the four demo customers and their devices, models, parts, services and playbooks.
- FastAPI with `/api/health`, JWT login, the event bus (`/ws/staff`, `/internal/events`), `GET /api/tickets`, and `GET /api/tickets/{id}/timeline`, brought forward from Block 2 for the inbox preview.
- `llm.py` with the §4.6 fallback order and `make llm-check`; the MCP hub, with `INTAKE_TOOLS` checked before every call.
- Intake v1 (§7.1 without duplicates): the serial by regex, one `MODEL_FAST` call per message, `lookup_serial` → `create_ticket` → playbook, summary and plan → `send_reply`, and the §15 fallback reply when the model is down. `POST /api/dev/simulate` as the stage backup.

**P2 · MCP servers & channels**

- Four MCP servers, started together by `run_all`: tickets (:8101), catalog (:8102), knowledge (:8103) and messaging (:8104). Each keeps at most 2 database connections, and a refused or lost connection is a `database_unavailable` result.
- The channel base, `identity.resolve` (a web chat links to a known customer by email), and the outbox dispatcher, which delivers only on the channels its own process runs.
- The Telegram adapter, and the web chat (`POST /api/chat/session`, `WS /ws/chat/{session_id}`), run end to end: a web message with Aman's serial creates his ticket, the reply arrives on the socket, the outbox row is marked sent and `ticket.created` reaches the dashboard.

**P3 · Frontend, design & deploy**

- Next.js with the §11.3 tokens and the light/dark toggle; the home page with its animated chat hero.
- `/login`, the staff shell, and `/inbox` on `GET /api/tickets`, live over `/ws/staff`, with the ticket's conversation in the preview.
- `/support` with the chat panel: the pre-chat form, typing, delivered, and the ticket number pinned once intake creates it.
- `deploy/`: the Caddyfile, the three systemd services and `deploy.sh`.

### Not built yet

- The live deploy: the server's hostname isn't set yet, so the README still says "not deployed yet".
- Telegram end to end with the dev bot: the adapter is built; the live run waits until the bot's other poller is stopped.
- The seed's 600 units and historical tickets (§14.3 lets them follow in Block 2).
- Planned for Block 2 and later: duplicate detection and follow-ups (§7.2), `decide.py`, Discord, Email, embeddings (`create_ticket` stores a null embedding), linking a customer across channels by serial (§6.3), `ai_runs` rows, `POST /api/dev/reset-demo` and `runtime.py`.

### Jury 1 comments

### Next

Block 2, "Every channel, one inbox" (§14.3): the jury's comments first, then Discord and Email onto one ticket with duplicates, the ticket page and the polished reply.
