# ServiceMesh dev commands (ARCHITECTURE.md §12). Run from the repo root.
# No make on Windows? Run the line under a target directly in Git Bash.

.PHONY: db-reset seed mcp api web types llm-check

# Drop every app table, re-apply db/schema.sql, and verify the tables exist. Destroys all data.
db-reset:
	uv run --project backend python db/apply_schema.py

# Wipe all rows and re-seed (idempotent; same ids every run). Destroys all data.
seed:
	uv run --project backend python db/seed/seed.py

# Start every MCP server that exists, one process each on 8101-8107 (ARCHITECTURE.md §3, §5). Ctrl+C stops all.
mcp:
	cd backend && uv run python -m mcp_servers.run_all

api:
	cd backend && uv run uvicorn app.main:app --port 8000

web:
	cd web && pnpm dev

# Regenerate web/src/lib/api-types.ts from the FastAPI OpenAPI schema (no running server needed).
types:
	cd backend && uv run python -c "import json; from app.main import app; print(json.dumps(app.openapi()))" | (cd ../web && pnpm exec openapi-typescript -o src/lib/api-types.ts)

# One tiny request to each configured LLM provider: latency and Groq's remaining rate limits (§4.6). Never prints keys.
llm-check:
	cd backend && uv run python -m app.brain.llm --check