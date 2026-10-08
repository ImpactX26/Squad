"""Compact JSON tool results and errors shared by the MCP servers (ARCHITECTURE.md §5)."""

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError


def _default(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # Amounts leave the server as two-decimal strings ("6.90"), never floats (§5.5).
        return f"{value:.2f}"
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=_default)


def jsonable(value: Any) -> Any:
    """The same value with UUIDs, dates and amounts as JSON-ready strings."""
    return json.loads(dumps(value))


def fail(error: str, message: str, **details: Any) -> ToolError:
    """A refusal the caller can read: the result is is_error with {error, message, ...} as JSON."""
    return ToolError(dumps({"error": error, "message": message, **details}))


def parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise fail("bad_id", f"{field} is not a valid id", field=field) from None
