"""Declarative base for the SQLAlchemy models. db/schema.sql is the source of truth; models mirror it."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
