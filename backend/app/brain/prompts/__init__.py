"""System prompts, one .md per prompt (ARCHITECTURE.md §12).

Kept short on purpose: Groq's free tier allows about 6-8K tokens per minute, and the system
prompt is paid for on every single call (§4.5).
"""

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache
def load(name: str) -> str:
    """The text of `app/brain/prompts/<name>.md`, read once."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()