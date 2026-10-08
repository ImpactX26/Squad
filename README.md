# ServiceMesh

An AI-assisted, omnichannel after-sales service platform for a laptop, PC and headphones company ("Aurora Devices" in the demo), powered by seven custom MCP servers. Customers reach support from Discord, Telegram, email or the website chat. The AI "brain" understands each message, identifies the exact product by serial number, raises or updates a ticket, and always replies on the channel the customer used. Service-center agents work from one dashboard where AI helps them diagnose, reply, search and run automations with slash commands. Payment, technician dispatch, inventory and restock alerts run automatically through MCP tools.

This is the design we are building during a 24-hour hackathon. No part of it is built yet; [ARCHITECTURE.md](ARCHITECTURE.md) is the full design and [CLAUDE.md](CLAUDE.md) the working rules.

**Live URL:** not deployed yet.

## Team

| Person | Role |
|---|---|
| P1 | Brain & backend: schema, seed, FastAPI, auth, events, LLM layer, MCP hub, intake, writer, workflows, slash commands, suggestions |
| P2 | MCP servers & channels: all 7 MCP servers, Telegram, Discord, email, web-chat socket, outbox dispatcher, UPI verifier, search and the copilot tool loop |
| P3 | Frontend, design & deploy: design tokens, every page, the pay page, technician portal, Agent Activity rail, the server and `deploy.sh` |

## Setup

You need:

- [uv](https://docs.astral.sh/uv/) with Python 3.12
- Node 24 with pnpm 12.8.1

Copy the env examples and fill in your own values (never commit the copies):

```bash
cp backend/.env.example backend/.env
cp web/.env.local.example web/.env.local
```

There is no code to run yet.

## Progress by round

- **Jury 1:** in progress
