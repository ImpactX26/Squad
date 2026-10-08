# CLAUDE.md — ServiceMesh

Read this file first, then the ARCHITECTURE.md sections your task names, then ARCHITECTURE.md §14 to see which block we are in. ARCHITECTURE.md is the contract: follow its schema, names and folder structure exactly.

## Where we are

- A 24-hour hackathon build by three people, with a jury every 5 hours (§14). Each 5-hour block adds a vertical slice through every layer and ends deployed (§17) and tagged.
- Build only your role's slice of the current block (§14.3). If the task needs something from a later block, stop and ask; never build ahead. A section of ARCHITECTURE.md describes the finished feature; your block may need only part of it.
- Write all code fresh in this repo. Don't paste in code from other projects or from files outside the repo.
- No placeholder code that pretends to work. If something isn't built, the code says so (a clear error or a "not built yet" response) and your end-of-session report says so.

## The contract

- Table names, MCP tool names and arguments, event names, API routes, env vars and folders come from ARCHITECTURE.md. Stop and ask before deviating; if the change is approved, update ARCHITECTURE.md in the same commit.
- The contracts locked first (§14.6): `db/schema.sql` (§8.1, created whole in Block 1), MCP tool signatures (§5), Pydantic response schemas (then `make types` regenerates `web/src/lib/api-types.ts`), event names (§9) and the WebSocket shape `{type, data, ts}`.
- Until a real endpoint exists, the frontend uses mock JSON shaped exactly like its Pydantic schema.

## Folders and ownership (§14.1)

| Person | Owns |
|---|---|
| P1 · Brain & backend | `db/`, `backend/app/core`, `models`, `schemas`, `api`, `brain`, `payments` (except `upi_verifier.py`) and their tests |
| P2 · MCP servers & channels | `backend/mcp_servers/`, `backend/app/channels/`, `backend/app/templates/email/`, `backend/app/payments/upi_verifier.py` and their tests; in Block 5 also `brain/search.py`, `brain/runtime.py`, `api/search.py`, `api/copilot.py` |
| P3 · Frontend, design & deploy | `web/`, `deploy/` |

Work only inside the folders of the person you are working for. A change in another person's folder is agreed with them first and committed by its owner.

## Safety rules (enforced in code, never only in prompts)

- All model calls go through `app/brain/llm.py`, `decide.py` and `router.py` (§4.6). Never add the `anthropic` package or any paid API. Free tiers only: Groq, optional Jev, optional Ollama.
- Tests never call a real model provider. Use `tests/llm_fakes.py` and the provider's MockTransport (httpx2 in §18.3).
- Customer-facing intake never reaches payments, dispatch or inventory tools (§4.1). `INTAKE_TOOLS` is checked before the hub is called.
- Decisions never authorize payment, dispatch, refunds or stock changes. Only staff actions and `payment.paid` do, and the only automatic path to `payment.paid` is a verified bank-alert match (`app/payments/upi_verifier.py`, §7.6).
- Amounts are computed in code from the catalog (§5.5); no tool takes an amount argument. Tools in `router.MODEL_FORBIDDEN_TOOLS` are never offered to a model, whatever the role or command.
- Customer-path code catches `LLMUnavailable` and sends the §15 fallback reply. A customer never gets silence.
- Replies go to the conversation's own channel through `messaging.send_reply` and the outbox (§5.4, §6.1). A model never chooses the channel.

## Engineering rules

- Python 3.12 via `uv`; Node 24 with `pnpm`. Pin exact versions; the known-good set is §18.3. Prefer boring, well-known libraries, and say why before adding a new one.
- The backend is async end to end. Blocking work (IMAP, PDF rendering) runs in a worker thread, never on the event loop.
- Backend-side URLs use `127.0.0.1`, never `localhost` (§4.5). Browser-side `NEXT_PUBLIC_*` URLs keep `localhost` on a laptop.
- `.env` comments go on their own line; `KEY=   # comment` makes the comment the value (§18.1).
- An API change updates its Pydantic schema and runs `make types` in the same commit.
- Every MCP tool gets a smoke test. Database tests skip with the reason when the database is unreachable.
- Frontend: the tokens of §11.3 only, light and dark both checked, sentence case, the shared loading / empty / error states (`components/states.tsx`). `/pay/[token]` and `/jobs` are mobile-first from 360 px. Markdown is rendered without raw HTML.
- Read §18 before touching Telegram, IMAP, Groq reasoning models, Supabase connections or the outbox.

## Commits and pushes

- The repo is https://github.com/ImpactX26/Squad (`origin`, branch `main`). After each commit: tests pass → `git pull --rebase --autostash` → `git push`. Push at least every 45 minutes. Stage your own paths by name, never `git add -A`: several sessions may share one working copy.
- One working step per commit: run it (server, test, endpoint), see it work, then commit.
- Message format `type(scope): what changed`. Types `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `chore`; scopes `db`, `api`, `brain`, `mcp`, `channels`, `payments`, `web`, `deploy`. Example: `feat(mcp): add the tickets server (:8101)`. No "wip" or "final" messages, no padding commits.
- Before each commit, scan the staged diff for secrets: `git diff --cached | grep -iE "gsk_|api_key|token|password"`. Before each push: tests pass, then `git pull --rebase`. Push at least every 45 minutes.
- Never commit `.env` or `.env.local`; only the `.example` files. Never force-push `main` or rewrite its history.

## Never touch without being asked

- The server's `backend/.env` and `web/.env.local`, and the systemd services (§17).
- The prod database: `make db-reset` and `make seed` wipe whatever `DATABASE_URL` points at. Laptops point at the dev project (§13.4).
- The prod bot tokens and prod Gmail: they run only on the server. Laptops keep `ENABLE_*=false`, except P2's with the dev bots.

## Commands

```bash
make db-reset      # drop app tables, apply db/schema.sql, verify (destroys data in DATABASE_URL)
make seed          # wipe rows and re-seed (same ids every run)
make mcp           # all MCP servers, 8101-8107
make api           # FastAPI on :8000 (OpenAPI docs at /docs)
make web           # Next.js on :3000
make types         # regenerate web/src/lib/api-types.ts from the FastAPI schema
make llm-check     # one tiny request per LLM provider: latency and Groq's remaining limits
cd backend && uv run pytest -rs
```

`make` is often missing on Windows: run the line under each Makefile target in Git Bash.

Stage backups (dev only, staff login): `POST /api/dev/simulate` injects a message on any channel; `POST /api/dev/simulate-bank-alert` runs a fake bank SMS through the real verifier; `POST /api/dev/reset-demo` restores the demo story.

## End every session with

1. What works, with proof: the command you ran and its output.
2. What is incomplete or not built yet.
3. The exact commands to run it.
4. The commits you pushed, and anything teammates must know (a contract change, a new env var, a new dependency).