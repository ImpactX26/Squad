"""Request and response models for slash commands (ARCHITECTURE.md §7.5, §10)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

CommandArgs = Literal["none", "optional", "required"]

# Custom command names: lowercase letters, digits and dashes, without the slash (§8.1).
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"


class CommandOut(BaseModel):
    """A built-in (from code, §7.5) or one of the signed-in agent's custom commands (slash_commands)."""

    id: uuid.UUID | None = Field(description="null for a built-in")
    name: str
    usage: str = Field(description='"/ask <what>": how to type it')
    description: str
    args: CommandArgs = Field(description="Whether words after the name are needed")
    example: str | None = Field(description="Words an agent might add, e.g. display replacement")
    is_builtin: bool
    prompt_template: str | None = Field(description="Custom commands only")
    allowed_tools: list[str] = Field(description="Custom commands: the only tools the model is offered")
    created_at: datetime | None


class CommandToolOut(BaseModel):
    """A tool a custom command may list: the copilot's tools, minus the ones no model is offered."""

    name: str
    server: str
    description: str


class CommandListResponse(BaseModel):
    commands: list[CommandOut]
    tools: list[CommandToolOut]
    unreachable_servers: list[str] = Field(
        description="MCP servers whose tools couldn't be listed just now, so they aren't in tools")
    variables: dict[str, str] = Field(description="The {{variables}} a template may use, and what each holds")


class CommandCreate(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, description="Without the slash: lowercase letters, digits, dashes")
    description: str = Field(min_length=1, max_length=200)
    prompt_template: str = Field(min_length=1, max_length=4000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=12)


class CommandPatch(BaseModel):
    name: str | None = Field(default=None, pattern=NAME_PATTERN)
    description: str | None = Field(default=None, min_length=1, max_length=200)
    prompt_template: str | None = Field(default=None, min_length=1, max_length=4000)
    allowed_tools: list[str] | None = Field(default=None, max_length=12)


class SuggestionChip(BaseModel):
    """One suggested action (§7.5): clicking it runs /name with args, or fills the composer."""

    name: str
    args: str
    label: str
    needs_args: bool = Field(description="True: put \"/name \" in the composer for the agent's words instead of running")


class SuggestionsResponse(BaseModel):
    ticket_id: uuid.UUID
    chips: list[SuggestionChip]
    source: Literal["ai", "rules"] = Field(description="ai: ranked by the decision model; rules: the code's order")
    cached: bool