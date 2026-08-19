"""Skill master with alias normalisation.

Shared by demand (Phase 5) and talent (Phase 6). Free-text skills extracted from
a JD or a CV are mapped onto this master before they are stored, because "K8s"
and "Kubernetes" scoring as two different skills would quietly destroy match
quality (ASSUMPTIONS.md A22).
"""

from __future__ import annotations

import re

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseEntity
from app.db.types import JSONType


class Skill(BaseEntity):
    __tablename__ = "skills"

    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    #: Lower-cased, punctuation-stripped form used for lookup.
    normalized: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    aliases: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    #: Set when a parser invented it and no human has confirmed the mapping yet.
    needs_review: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<Skill {self.name}>"


_PUNCTUATION = re.compile(r"[^a-z0-9+#./ ]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_skill(raw: str) -> str:
    """Fold a free-text skill into its lookup key.

    Deliberately conservative: it lower-cases, strips punctuation that carries no
    meaning, and collapses whitespace. It does **not** stem or fuzzy-match, so
    "SAP FI" and "SAP FICO" stay distinct — they are distinct skills.
    """
    value = raw.strip().lower()
    value = value.replace("&", " and ")
    value = _PUNCTUATION.sub(" ", value)
    value = _WHITESPACE.sub(" ", value)
    return value.strip()


__all__ = ["Skill", "normalize_skill"]
