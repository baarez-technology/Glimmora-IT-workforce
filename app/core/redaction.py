"""Field-level redaction (AD-6, SECURITY.md section 4, layer 3).

Endpoint-level permissions answer "may you call this?". They cannot answer "may
you see the cost rate inside this response?" — which matters because Sales and
Resourcing both legitimately read a deployment but must see different columns of
it.

A restricted key is **removed**, not nulled or masked. A `null` still leaks that
the field exists and was populated; absence leaks nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.core.permissions import Permission

#: Response key -> permission required to see it.
#: Keys are matched by name at any depth, so a nested `deployment.cost_rate` is
#: covered by the same single entry.
FIELD_PERMISSION_MAP: dict[str, Permission] = {
    # --- consultant cost (Sales must not see this) ---------------------
    "cost_rate": Permission.FIELD_RESOURCE_COST,
    "cost_currency": Permission.FIELD_RESOURCE_COST,
    "cost_unit": Permission.FIELD_RESOURCE_COST,
    "cost_amount": Permission.FIELD_RESOURCE_COST,
    "monthly_cost": Permission.FIELD_RESOURCE_COST,
    "total_cost": Permission.FIELD_RESOURCE_COST,
    "expected_cost_amount": Permission.FIELD_RESOURCE_COST,
    "expected_cost_currency": Permission.FIELD_RESOURCE_COST,
    "expected_cost_unit": Permission.FIELD_RESOURCE_COST,
    "visa_cost": Permission.FIELD_RESOURCE_COST,
    "insurance_cost": Permission.FIELD_RESOURCE_COST,
    "other_cost": Permission.FIELD_RESOURCE_COST,
    # --- client price (Resourcing must not see this) -------------------
    "bill_rate": Permission.FIELD_BILLING_RATE,
    "bill_currency": Permission.FIELD_BILLING_RATE,
    "bill_unit": Permission.FIELD_BILLING_RATE,
    "proposed_bill_rate": Permission.FIELD_BILLING_RATE,
    "proposed_bill_currency": Permission.FIELD_BILLING_RATE,
    "proposed_bill_unit": Permission.FIELD_BILLING_RATE,
    "target_billing_amount": Permission.FIELD_BILLING_RATE,
    "target_billing_currency": Permission.FIELD_BILLING_RATE,
    "target_billing_unit": Permission.FIELD_BILLING_RATE,
    "revenue_amount": Permission.FIELD_BILLING_RATE,
    "monthly_revenue": Permission.FIELD_BILLING_RATE,
    "expected_monthly_revenue": Permission.FIELD_BILLING_RATE,
    # --- profitability --------------------------------------------------
    "margin_percent": Permission.FIELD_MARGIN,
    "expected_margin_percent": Permission.FIELD_MARGIN,
    "gross_profit": Permission.FIELD_MARGIN,
    "total_profit": Permission.FIELD_MARGIN,
    # --- deal size ------------------------------------------------------
    "contract_value": Permission.FIELD_CONTRACT_VALUE,
    "pipeline_value": Permission.FIELD_CONTRACT_VALUE,
    # --- personal identifiers on documents ------------------------------
    "reference_number": Permission.FIELD_DOCUMENT_PERSONAL_VIEW,
    "passport_number": Permission.FIELD_DOCUMENT_PERSONAL_VIEW,
    "national_id": Permission.FIELD_DOCUMENT_PERSONAL_VIEW,
}


def redact(value: Any, permissions: Iterable[Permission], *, _depth: int = 0) -> Any:
    """Return `value` with keys the caller may not see removed entirely."""
    granted = permissions if isinstance(permissions, (set, frozenset)) else set(permissions)

    if _depth > 12:
        return value

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            required = FIELD_PERMISSION_MAP.get(key)
            if required is not None and required not in granted:
                continue
            result[key] = redact(item, granted, _depth=_depth + 1)
        return result

    if isinstance(value, list):
        return [redact(item, granted, _depth=_depth + 1) for item in value]

    if isinstance(value, tuple):
        return tuple(redact(item, granted, _depth=_depth + 1) for item in value)

    return value


def restricted_keys(permissions: Iterable[Permission]) -> set[str]:
    """Keys the caller cannot see — used by tests and by the Admin roles screen."""
    granted = set(permissions)
    return {key for key, required in FIELD_PERMISSION_MAP.items() if required not in granted}


__all__ = ["FIELD_PERMISSION_MAP", "redact", "restricted_keys"]
