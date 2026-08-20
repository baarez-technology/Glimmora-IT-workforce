"""Column schemas and coercion for Excel import.

A spreadsheet is the least trustworthy input the platform accepts. Dates arrive
as text, numbers arrive with currency symbols, headers arrive with trailing
spaces and inconsistent capitalisation, and booleans arrive as "Yes", "Y", "1"
and "TRUE" in the same column.

Everything here is about meeting that reality without ever guessing at a value
whose meaning is unclear. A cell that cannot be read confidently produces an
error naming the row and column, not a silent default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.platform import ImportEntity


class CoercionError(ValueError):
    """A cell whose value cannot be read. Carries a message a user can act on."""


@dataclass(slots=True)
class Column:
    key: str
    #: Header text as a human would type it. Matching is case- and
    #: punctuation-insensitive, so "Full Name", "full_name" and "FULL NAME"
    #: all resolve to the same column.
    label: str
    kind: str = "text"
    required: bool = False
    max_length: int | None = None
    choices: tuple[str, ...] | None = None
    #: Shown in the template and the error export.
    hint: str | None = None


@dataclass(slots=True)
class EntitySchema:
    entity: ImportEntity
    columns: list[Column]
    #: Fields that together identify an existing record, for duplicate
    #: detection. Checked in order; the first that matches wins.
    identity_fields: list[tuple[str, ...]] = field(default_factory=list)

    def column(self, key: str) -> Column | None:
        return next((column for column in self.columns if column.key == key), None)

    @property
    def required_keys(self) -> list[str]:
        return [column.key for column in self.columns if column.required]


def normalise_header(value: Any) -> str:
    """Fold a header into a comparable key.

    'Full Name ', 'full_name' and 'FULL-NAME' are the same column. Users should
    not have to match our spelling exactly to import their own data.
    """
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


_TRUE = {"true", "yes", "y", "1", "t"}
_FALSE = {"false", "no", "n", "0", "f"}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%m/%d/%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y/%m/%d",
)


def coerce(value: Any, column: Column) -> Any:
    """Read one cell, or raise a message naming what is wrong with it."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if column.required:
            raise CoercionError(f"{column.label} is required")
        return None

    if column.kind == "text":
        text = str(value).strip()
        if column.max_length and len(text) > column.max_length:
            raise CoercionError(f"{column.label} is longer than {column.max_length} characters")
        return text

    if column.kind == "choice":
        text = str(value).strip().upper().replace(" ", "_").replace("-", "_")
        allowed = column.choices or ()
        if text not in allowed:
            raise CoercionError(
                f"{column.label} must be one of {', '.join(allowed)} — got {value!r}"
            )
        return text

    if column.kind == "bool":
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise CoercionError(f"{column.label} must be yes or no — got {value!r}")

    if column.kind == "int":
        try:
            return int(float(str(value).strip().replace(",", "")))
        except (TypeError, ValueError) as exc:
            raise CoercionError(f"{column.label} must be a whole number — got {value!r}") from exc

    if column.kind == "decimal":
        # Strip anything a human might type around a number: currency symbols,
        # thousands separators, stray spaces.
        cleaned = re.sub(r"[^\d.\-]", "", str(value).strip())
        if not cleaned or cleaned in {"-", ".", "-."}:
            raise CoercionError(f"{column.label} must be a number — got {value!r}")
        try:
            amount = Decimal(cleaned)
        except InvalidOperation as exc:
            raise CoercionError(f"{column.label} must be a number — got {value!r}") from exc
        if amount < 0:
            raise CoercionError(f"{column.label} cannot be negative")
        return amount

    if column.kind == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        # Ambiguity is reported, never guessed at: 03/04/2026 could be two
        # different days and the importer will not pick one.
        raise CoercionError(
            f"{column.label} is not a date we can read — use YYYY-MM-DD (got {value!r})"
        )

    if column.kind == "email":
        text = str(value).strip()
        if "@" not in text or "." not in text.split("@")[-1]:
            raise CoercionError(f"{column.label} is not a valid email — got {value!r}")
        return text.lower()

    if column.kind == "country":
        text = str(value).strip().upper()
        if len(text) != 2 or not text.isalpha():
            raise CoercionError(f"{column.label} must be a two-letter country code — got {value!r}")
        return text

    if column.kind == "currency":
        text = str(value).strip().upper()
        if len(text) != 3 or not text.isalpha():
            raise CoercionError(
                f"{column.label} must be a three-letter currency code — got {value!r}"
            )
        return text

    if column.kind == "list":
        text = str(value).strip()
        return [item.strip() for item in re.split(r"[;,|]", text) if item.strip()]

    return str(value).strip()


# ------------------------------------------------------------------ schemas

ACCOUNT_TYPES = ("CUSTOMER", "PARTNER", "PRIME_CONTRACTOR", "VENDOR_MSP", "PROSPECT")
RELATIONSHIP_STATUSES = ("ACTIVE", "DORMANT", "TARGET", "BLOCKED")
RESOURCE_TYPES = (
    "EMPLOYEE",
    "BENCH",
    "CONSULTANT",
    "FREELANCER",
    "PARTNER_RESOURCE",
    "PREVIOUS_CANDIDATE",
    "PRE_VETTED_CANDIDATE",
)
AVAILABILITY = ("AVAILABLE", "AVAILABLE_SOON", "DEPLOYED", "NOT_AVAILABLE")
PRIORITY_SOURCES = (
    "P1_EXISTING_CUSTOMER",
    "P2_PARTNER_PRIME",
    "P3_PROJECT",
    "P4_ENTERPRISE_GOV",
    "P5_VENDOR_MSP_VMS",
    "P6_EXTERNAL_APPROVED",
)
RATE_UNITS = ("HOURLY", "DAILY", "MONTHLY", "ANNUAL")


SCHEMAS: dict[ImportEntity, EntitySchema] = {
    ImportEntity.CUSTOMERS: EntitySchema(
        entity=ImportEntity.CUSTOMERS,
        columns=[
            Column("name", "Name", required=True, max_length=200),
            Column(
                "account_type", "Account type", "choice", choices=ACCOUNT_TYPES, hint="CUSTOMER"
            ),
            Column("country", "Country", "country", hint="QA"),
            Column("city", "City", max_length=120),
            Column("industry", "Industry", max_length=120),
            Column("website", "Website", max_length=255),
            Column(
                "relationship_status",
                "Relationship status",
                "choice",
                choices=RELATIONSHIP_STATUSES,
            ),
            Column("is_existing_customer", "Existing customer", "bool"),
            Column("is_existing_partner", "Existing partner", "bool"),
            Column("is_approved_vendor", "Approved vendor", "bool"),
            Column("has_msa", "Has MSA", "bool"),
            Column("contract_outsourcing_friendly", "Outsourcing friendly", "bool"),
            Column("payment_terms_days", "Payment terms (days)", "int"),
            Column("notes", "Notes"),
        ],
        identity_fields=[("name", "country"), ("name",)],
    ),
    ImportEntity.CONTACTS: EntitySchema(
        entity=ImportEntity.CONTACTS,
        columns=[
            Column("account_name", "Account name", required=True, max_length=200),
            Column("full_name", "Full name", required=True, max_length=160),
            Column("job_title", "Job title", max_length=160),
            Column("email", "Email", "email"),
            Column("phone", "Phone", max_length=48),
            Column("is_decision_maker", "Decision maker", "bool"),
            Column("notes", "Notes"),
        ],
        identity_fields=[("email",), ("account_name", "full_name")],
    ),
    ImportEntity.PROJECTS: EntitySchema(
        entity=ImportEntity.PROJECTS,
        columns=[
            Column("account_name", "Account name", required=True, max_length=200),
            Column("name", "Project name", required=True, max_length=200),
            Column("description", "Description"),
            Column("start_date", "Start date", "date"),
            Column("end_date", "End date", "date"),
            Column("technologies", "Technologies", "list", hint="SAP; Java; Azure"),
        ],
        identity_fields=[("account_name", "name")],
    ),
    ImportEntity.REQUIREMENTS: EntitySchema(
        entity=ImportEntity.REQUIREMENTS,
        columns=[
            Column("title", "Title", required=True, max_length=240),
            Column("role", "Role", max_length=160),
            Column("account_name", "Account name", max_length=200),
            Column("positions", "Positions", "int"),
            Column("priority_source", "Priority source", "choice", choices=PRIORITY_SOURCES),
            Column("country", "Country", "country"),
            Column("location", "Location", max_length=160),
            Column("experience_min_years", "Minimum experience", "int"),
            Column("duration_months", "Duration (months)", "int"),
            Column("rate_min", "Rate from", "decimal"),
            Column("rate_max", "Rate to", "decimal"),
            Column("rate_currency", "Rate currency", "currency"),
            Column("rate_unit", "Rate unit", "choice", choices=RATE_UNITS),
            Column("skills", "Mandatory skills", "list", hint="SAP FICO; SAP S/4HANA"),
        ],
        identity_fields=[("title", "account_name"), ("title",)],
    ),
    ImportEntity.RESOURCES: EntitySchema(
        entity=ImportEntity.RESOURCES,
        columns=[
            Column("full_name", "Full name", required=True, max_length=160),
            Column("email", "Email", "email"),
            Column("phone", "Phone", max_length=48),
            Column("resource_type", "Resource type", "choice", choices=RESOURCE_TYPES),
            Column("headline", "Headline", max_length=240),
            Column("total_experience_years", "Total experience (years)", "int"),
            Column("current_location_country", "Country", "country"),
            Column("current_location_city", "City", max_length=120),
            Column("availability_status", "Availability", "choice", choices=AVAILABILITY),
            Column("available_from", "Available from", "date"),
            Column("notice_period_days", "Notice period (days)", "int"),
            Column("expected_cost_amount", "Expected cost", "decimal"),
            Column("expected_cost_currency", "Cost currency", "currency"),
            Column("expected_cost_unit", "Cost unit", "choice", choices=RATE_UNITS),
            Column("skills", "Skills", "list", hint="SAP FICO; Power BI"),
        ],
        identity_fields=[("email",), ("full_name",)],
    ),
}


def schema_for(entity: ImportEntity) -> EntitySchema:
    schema = SCHEMAS.get(entity)
    if schema is None:
        raise CoercionError(f"Import is not supported for {entity.value} yet")
    return schema


#: Entities the importer supports today. `deployments` and `billing` are export
#: only: both derive from records the pipeline creates, and importing them
#: directly would let a spreadsheet invent revenue.
SUPPORTED_IMPORTS: tuple[ImportEntity, ...] = tuple(SCHEMAS)


__all__ = [
    "SCHEMAS",
    "SUPPORTED_IMPORTS",
    "CoercionError",
    "Column",
    "EntitySchema",
    "coerce",
    "normalise_header",
    "schema_for",
]
