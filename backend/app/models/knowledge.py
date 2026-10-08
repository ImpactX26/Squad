import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk


class KbPlaybook(Base):
    __tablename__ = "kb_playbooks"

    id: Mapped[uuid.UUID] = uuid_pk()
    issue_type: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_models.id")
    )  # null = applies to all models in category
    title: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[list] = mapped_column(JSONB, nullable=False)  # [{"step","expected","resolves_if"}]
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))
    