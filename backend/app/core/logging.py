"""Logging safety: keep access tokens out of the server's logs.

/ws/staff takes its JWT in the query string (§10), and uvicorn logs every request path with its
query, so without this filter each dashboard's token would land in journalctl in plain text.
"""

import logging
import re

TOKEN_IN_QUERY = re.compile(r"([?&]token=)[^&\s\"']+")
REDACTED = r"\1[redacted]"


def redact(text: str) -> str:
    return TOKEN_IN_QUERY.sub(REDACTED, text)


class RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(arg) if isinstance(arg, str) else arg for key, arg in record.args.items()}
        return True


def install_token_redaction() -> None:
    """Add the filter to uvicorn's loggers (the access log, and "uvicorn.error", which logs websockets)."""
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokens) for f in logger.filters):
            logger.addFilter(RedactTokens())
