"""Phase 3: RBAC matrix, field redaction and the audit trail.

This is the SECURITY.md section 9 checklist expressed as tests. Every later
phase extends the matrix here rather than inventing its own checks.
"""

from __future__ import annotations

import pytest

from app.core.permissions import (
    ALL_PERMISSIONS,
    FIELD_PERMISSIONS,
    ROLE_PERMISSIONS,
    Permission,
    Role,
)
from app.core.redaction import FIELD_PERMISSION_MAP, redact, restricted_keys

API = "/api/v1"
pytestmark = pytest.mark.security

#: Endpoints that must be reachable without a token. Nothing else may be.
PUBLIC_ENDPOINTS = {
    ("POST", f"{API}/auth/login"),
    ("POST", f"{API}/auth/refresh"),
    ("POST", f"{API}/auth/logout"),
    ("GET", f"{API}/system/health"),
    ("GET", f"{API}/system/config"),
    # Interactive docs, served only when ENABLE_DOCS is on and never in
    # production — see test_docs_are_disabled_in_production below.
    ("GET", f"{API}/docs"),
    ("GET", f"{API}/openapi.json"),
}


class TestPermissionPolicy:
    def test_admin_holds_every_permission(self):
        assert ROLE_PERMISSIONS[Role.ADMIN] == ALL_PERMISSIONS

    def test_every_role_is_in_the_matrix(self):
        assert set(ROLE_PERMISSIONS) == set(Role)

    def test_sales_cannot_see_consultant_cost(self):
        """Sales negotiates the client price; Resourcing negotiates the cost."""
        assert Permission.FIELD_RESOURCE_COST not in ROLE_PERMISSIONS[Role.SALES]
        assert Permission.FIELD_MARGIN in ROLE_PERMISSIONS[Role.SALES]
        assert Permission.FIELD_BILLING_RATE in ROLE_PERMISSIONS[Role.SALES]

    def test_resourcing_cannot_see_client_rates_or_margin(self):
        granted = ROLE_PERMISSIONS[Role.HR_RESOURCING]
        assert Permission.FIELD_RESOURCE_COST in granted
        assert Permission.FIELD_BILLING_RATE not in granted
        assert Permission.FIELD_MARGIN not in granted

    def test_management_sees_both_sides_but_administers_nothing(self):
        granted = ROLE_PERMISSIONS[Role.MANAGEMENT]
        assert Permission.FIELD_RESOURCE_COST in granted
        assert Permission.FIELD_MARGIN in granted
        assert Permission.USER_CREATE not in granted
        assert Permission.USER_UPDATE not in granted
        assert Permission.SCORING_CONFIG_EDIT not in granted

    def test_management_may_view_but_not_download_personal_documents(self):
        granted = ROLE_PERMISSIONS[Role.MANAGEMENT]
        assert Permission.FIELD_DOCUMENT_PERSONAL_VIEW in granted
        assert Permission.FIELD_DOCUMENT_PERSONAL_DOWNLOAD not in granted

    def test_only_admin_edits_scoring_rules(self):
        for role in Role:
            expected = role is Role.ADMIN
            assert (Permission.SCORING_CONFIG_EDIT in ROLE_PERMISSIONS[role]) is expected

    def test_audit_is_restricted_to_admin_and_management(self):
        allowed = {role for role in Role if Permission.AUDIT_VIEW in ROLE_PERMISSIONS[role]}
        assert allowed == {Role.ADMIN, Role.MANAGEMENT}

    def test_no_role_other_than_admin_can_administer_users(self):
        admin_only = {
            Permission.USER_CREATE,
            Permission.USER_UPDATE,
            Permission.USER_DEACTIVATE,
        }
        for role in Role:
            if role is Role.ADMIN:
                continue
            assert not (admin_only & ROLE_PERMISSIONS[role])


class TestAuthenticationRequired:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", f"{API}/auth/me"),
            ("GET", f"{API}/users"),
            ("POST", f"{API}/users"),
            ("GET", f"{API}/roles"),
            ("GET", f"{API}/audit"),
            ("GET", f"{API}/audit/actions"),
        ],
    )
    async def test_endpoint_rejects_anonymous_callers(self, client, method, path):
        response = await client.request(method, path, json={})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    async def test_only_the_documented_endpoints_are_public(self, app):
        """Guards against a later phase forgetting a dependency."""
        unguarded: list[tuple[str, str]] = []

        for route in app.routes:
            path = getattr(route, "path", None)
            if not path or not path.startswith(API) or "{" in path:
                continue
            has_guard = bool(getattr(route, "dependant", None) and route.dependant.dependencies)
            for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
                if (method, path) in PUBLIC_ENDPOINTS:
                    continue
                if not has_guard:
                    unguarded.append((method, path))

        assert unguarded == [], f"Endpoints without a dependency guard: {unguarded}"

    def test_docs_are_disabled_in_production(self, monkeypatch):
        from app.core.config import AppEnv, settings

        monkeypatch.setattr(settings, "APP_ENV", AppEnv.PRODUCTION)
        assert settings.docs_url is None
        assert settings.openapi_url is None


class TestRoleAccessMatrix:
    """403 for every role that should not reach an endpoint."""

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (Role.ADMIN, 200),
            (Role.MANAGEMENT, 200),
            (Role.SALES, 403),
            (Role.HR_RESOURCING, 403),
        ],
    )
    async def test_list_users(self, as_role, role, expected):
        client, _ = await as_role(role)
        assert (await client.get(f"{API}/users")).status_code == expected

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (Role.ADMIN, 201),
            (Role.MANAGEMENT, 403),
            (Role.SALES, 403),
            (Role.HR_RESOURCING, 403),
        ],
    )
    async def test_create_user(self, as_role, role, expected):
        client, _ = await as_role(role)
        response = await client.post(
            f"{API}/users",
            json={
                "email": f"new-{role.value.lower()}@test.glimmora.ai",
                "full_name": "New Person",
                "role": "SALES",
                "password": "A-Strong-Password-2026!",
            },
        )
        assert response.status_code == expected

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (Role.ADMIN, 200),
            (Role.MANAGEMENT, 200),
            (Role.SALES, 403),
            (Role.HR_RESOURCING, 403),
        ],
    )
    async def test_audit_log(self, as_role, role, expected):
        client, _ = await as_role(role)
        assert (await client.get(f"{API}/audit")).status_code == expected

    @pytest.mark.parametrize("role", list(Role))
    async def test_role_catalogue_is_readable_by_everyone(self, as_role, role):
        """Users must be able to see why something is hidden from them."""
        client, _ = await as_role(role)
        assert (await client.get(f"{API}/roles")).status_code == 200

    async def test_forbidden_response_never_explains_the_missing_permission(self, as_role):
        client, _ = await as_role(Role.SALES)
        body = (await client.get(f"{API}/users")).json()

        assert body["error"]["message"] == "You do not have permission to do that."
        assert "user:read" not in str(body)


class TestFieldRedaction:
    def test_restricted_keys_are_removed_not_nulled(self):
        payload = {"bill_rate": "450.00", "cost_rate": "300.00", "role": "SAP Consultant"}

        result = redact(payload, ROLE_PERMISSIONS[Role.SALES])

        assert "cost_rate" not in result, "a null still reveals that the field exists"
        assert result["bill_rate"] == "450.00"
        assert result["role"] == "SAP Consultant"

    def test_redaction_reaches_nested_structures(self):
        payload = {
            "deployments": [
                {"id": "1", "bill_rate": "450.00", "cost_rate": "300.00"},
                {"id": "2", "bill_rate": "500.00", "cost_rate": "320.00"},
            ],
            "summary": {"margin_percent": 33.3, "headcount": 2},
        }

        result = redact(payload, ROLE_PERMISSIONS[Role.HR_RESOURCING])

        assert all("bill_rate" not in row for row in result["deployments"])
        assert all("cost_rate" in row for row in result["deployments"])
        assert "margin_percent" not in result["summary"]
        assert result["summary"]["headcount"] == 2

    @pytest.mark.parametrize("role", list(Role))
    def test_every_role_gets_a_consistent_view(self, role):
        payload = dict.fromkeys(FIELD_PERMISSION_MAP, "value")
        result = redact(payload, ROLE_PERMISSIONS[role])

        assert set(result) == set(FIELD_PERMISSION_MAP) - restricted_keys(ROLE_PERMISSIONS[role])

    def test_admin_sees_everything(self):
        payload = dict.fromkeys(FIELD_PERMISSION_MAP, "value")
        assert redact(payload, ROLE_PERMISSIONS[Role.ADMIN]) == payload

    def test_every_mapped_field_uses_a_real_field_permission(self):
        for key, permission in FIELD_PERMISSION_MAP.items():
            assert permission in FIELD_PERMISSIONS, f"{key} maps to a non-field permission"


class TestUserAdministration:
    async def test_admin_can_create_and_then_sign_in_as_the_new_user(self, as_role, client):
        admin_client, _ = await as_role(Role.ADMIN)

        created = await admin_client.post(
            f"{API}/users",
            json={
                "email": "brand-new@test.glimmora.ai",
                "full_name": "Brand New",
                "role": "SALES",
                "password": "Another-Strong-Pass-9!",
                "must_change_password": False,
            },
        )
        assert created.status_code == 201
        assert created.json()["role"] == "SALES"

        del admin_client.headers["Authorization"]
        login = await admin_client.post(
            f"{API}/auth/login",
            json={"email": "brand-new@test.glimmora.ai", "password": "Another-Strong-Pass-9!"},
        )
        assert login.status_code == 200

    async def test_duplicate_email_is_a_conflict(self, as_role):
        client, admin = await as_role(Role.ADMIN)

        response = await client.post(
            f"{API}/users",
            json={
                "email": admin.email,
                "full_name": "Impostor",
                "role": "SALES",
                "password": "Another-Strong-Pass-9!",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"

    async def test_weak_password_is_refused_at_creation(self, as_role):
        client, _ = await as_role(Role.ADMIN)

        response = await client.post(
            f"{API}/users",
            json={
                "email": "weak@test.glimmora.ai",
                "full_name": "Weak Password",
                "role": "SALES",
                "password": "password123",
            },
        )
        assert response.status_code == 422

    async def test_admin_cannot_deactivate_themselves(self, as_role):
        client, admin = await as_role(Role.ADMIN)

        response = await client.post(f"{API}/users/{admin.id}/deactivate")
        assert response.status_code == 422
        assert "your own account" in response.json()["error"]["message"].lower()

    async def test_the_last_active_admin_cannot_be_demoted(self, client, make_user, session):
        """Otherwise the platform can lock every administrator out permanently."""
        from sqlalchemy import delete

        from app.models.identity import User

        # Start from a database with no other admins.
        await session.execute(delete(User).where(User.role == Role.ADMIN))
        await session.commit()

        sole_admin = await make_user(Role.ADMIN)
        victim = await make_user(Role.SALES)

        login = await client.post(
            f"{API}/auth/login", json={"email": sole_admin.email, "password": "Glimmora-Test-2026!"}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        # Demoting someone else is fine.
        assert (
            await client.patch(f"{API}/users/{victim.id}", json={"role": "HR_RESOURCING"})
        ).status_code == 200

        # Demoting the only remaining admin is not.
        response = await client.patch(f"{API}/users/{sole_admin.id}", json={"role": "SALES"})
        assert response.status_code == 403
        assert "only active administrator" in response.json()["error"]["message"].lower()

    async def test_deactivation_revokes_the_users_sessions(self, client, make_user, as_role):
        target = await make_user(Role.SALES)
        target_login = await client.post(
            f"{API}/auth/login", json={"email": target.email, "password": "Glimmora-Test-2026!"}
        )
        target_token = target_login.json()["access_token"]
        client.cookies.clear()

        admin_client, _ = await as_role(Role.ADMIN)
        assert (await admin_client.post(f"{API}/users/{target.id}/deactivate")).status_code == 200

        response = await client.get(
            f"{API}/auth/me", headers={"Authorization": f"Bearer {target_token}"}
        )
        assert response.status_code == 401


class TestAuditTrail:
    async def test_successful_login_is_audited(self, as_role):
        client, user = await as_role(Role.ADMIN)

        logs = (await client.get(f"{API}/audit", params={"action": "LOGIN"})).json()
        assert logs["total"] >= 1
        assert any(user.email in entry["summary"] for entry in logs["items"])

    async def test_failed_login_is_audited_even_though_the_request_failed(
        self, client, as_role, make_user
    ):
        victim = await make_user(Role.SALES)
        await client.post(
            f"{API}/auth/login", json={"email": victim.email, "password": "Wrong-Password-1!"}
        )
        client.cookies.clear()

        admin_client, _ = await as_role(Role.ADMIN)
        logs = (await admin_client.get(f"{API}/audit", params={"action": "LOGIN_FAILED"})).json()

        assert any(victim.email in entry["summary"] for entry in logs["items"])

    async def test_user_creation_is_audited(self, as_role):
        client, admin = await as_role(Role.ADMIN)

        await client.post(
            f"{API}/users",
            json={
                "email": "audited-create@test.glimmora.ai",
                "full_name": "Audited Create",
                "role": "SALES",
                "password": "Another-Strong-Pass-9!",
            },
        )

        logs = (await client.get(f"{API}/audit", params={"action": "USER_CREATED"})).json()
        entry = next(e for e in logs["items"] if "audited-create@test.glimmora.ai" in e["summary"])
        assert entry["actor_email"] == admin.email
        assert entry["entity_type"] == "user"
        assert entry["request_id"]

    async def test_role_change_is_audited_with_a_before_and_after(self, as_role, make_user):
        client, _ = await as_role(Role.ADMIN)
        target = await make_user(Role.SALES)

        await client.patch(f"{API}/users/{target.id}", json={"role": "HR_RESOURCING"})

        logs = (await client.get(f"{API}/audit", params={"action": "PERMISSION_CHANGED"})).json()
        entry = next(e for e in logs["items"] if str(target.id) == e["entity_id"])
        assert entry["changes"]["role"] == {"from": "SALES", "to": "HR_RESOURCING"}

    async def test_audit_never_records_a_password_or_token(self, as_role):
        client, _ = await as_role(Role.ADMIN)

        await client.post(
            f"{API}/users",
            json={
                "email": "no-secrets@test.glimmora.ai",
                "full_name": "No Secrets",
                "role": "SALES",
                "password": "Very-Secret-Password-1!",
            },
        )

        serialized = str((await client.get(f"{API}/audit", params={"page_size": 100})).json())
        assert "Very-Secret-Password-1!" not in serialized
        assert "hashed_password" not in serialized

    async def test_audit_log_has_no_write_endpoint(self, app):
        """Append-only is a property of the API surface, not just of intent."""
        mutating = [
            (method, route.path)
            for route in app.routes
            for method in getattr(route, "methods", set())
            if getattr(route, "path", "").startswith(f"{API}/audit")
            and method in {"POST", "PATCH", "PUT", "DELETE"}
        ]
        assert mutating == []

    def test_the_audited_action_catalogue_matches_the_security_document(self):
        from app.models.identity import AuditAction

        required = {
            "LOGIN",
            "LOGIN_FAILED",
            "LOGOUT",
            "PASSWORD_CHANGED",
            "USER_CREATED",
            "USER_UPDATED",
            "USER_DEACTIVATED",
            "PERMISSION_CHANGED",
            "DOCUMENT_DOWNLOADED",
            "SCORING_CONFIG_CHANGED",
            "CV_SUBMITTED",
            "DEPLOYMENT_CREATED",
            "BILLING_CONFIRMED",
        }
        assert required <= {action.value for action in AuditAction}
