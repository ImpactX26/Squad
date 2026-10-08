from typing import Any

from pydantic import BaseModel, Field

from app.core.events import EventType


class InternalEventIn(BaseModel):
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)