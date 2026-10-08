# ServiceMesh commands (CLAUDE.md "Commands", ARCHITECTURE.md §3 Processes).
# On Windows without make, run the line under a target in Git Bash.

.PHONY: db-reset seed mcp api web types llm-check

# Drops the app tables in DATABASE_URL, applies db/schema.sql and verifies them. Destroys data.
db-reset:
	cd backend && uv run python ../db/apply_schema.py

# Wipes rows and re-seeds the database in DATABASE_URL (§8.2).
seed:
	cd backend && uv run python ../db/seed/seed.py

# All seven MCP servers, 8101-8107.
mcp:
	cd backend && uv run python -m mcp_servers.run_all

# FastAPI on :8000, single worker (OpenAPI docs at /docs).
api:
	cd backend && uv run uvicorn app.main:app --port 8000

# Next.js on :3000.
web:
	cd web && pnpm dev

# Regenerates web/src/lib/api-types.ts from the running API's OpenAPI schema (needs make api).
types:
	cd web && pnpm exec openapi-typescript http://127.0.0.1:8000/openapi.json -o src/lib/api-types.ts

# One tiny request per LLM provider: latency and Groq's remaining limits.
llm-check:
	cd backend && uv run python -m app.brain.llm
