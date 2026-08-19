"""Phase 3: authentication, session rotation, lockout and password policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.api.v1.auth import REFRESH_COOKIE
from app.core.permissions import Role
from tests.conftest import TEST_PASSWORD

API = "/api/v1"
pytestmark = pytest.mark.security


class TestLogin:
    async def test_valid_credentials_return_a_token_and_the_user(self, client, make_user):
        user = await make_user(Role.SALES)

        response = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        assert response.status_code == 200

        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"]
        assert body["user"]["email"] == user.email
        assert body["user"]["role"] == "SALES"
        assert "account:read" in body["user"]["permissions"]

    async def test_refresh_token_is_httponly_and_never_in_the_body(self, client, make_user):
        user = await make_user()

        response = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )

        assert "refresh_token" not in response.json()
        cookie = next(c for c in response.headers.get_list("set-cookie") if REFRESH_COOKIE in c)
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie.lower() or "samesite=lax" in cookie.lower()

    async def test_wrong_password_is_rejected(self, client, make_user):
        user = await make_user()

        response = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": "Wrong-Password-123!"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    async def test_unknown_email_gives_the_same_answer_as_a_wrong_password(self, client, make_user):
        """User enumeration: both paths must be indistinguishable to a caller."""
        user = await make_user()

        wrong_password = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": "Wrong-Password-123!"}
        )
        unknown_user = await client.post(
            f"{API}/auth/login",
            json={"email": "nobody-here@test.glimmora.ai", "password": "Wrong-Password-123!"},
        )

        assert wrong_password.status_code == unknown_user.status_code == 401
        assert wrong_password.json()["error"]["message"] == unknown_user.json()["error"]["message"]

    async def test_deactivated_account_cannot_sign_in(self, client, make_user):
        user = await make_user(is_active=False)

        response = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        assert response.status_code == 401
        assert "deactivated" in response.json()["error"]["message"].lower()

    async def test_email_is_case_insensitive(self, client, make_user):
        user = await make_user(email="Mixed.Case@test.glimmora.ai")

        response = await client.post(
            f"{API}/auth/login", json={"email": user.email.upper(), "password": TEST_PASSWORD}
        )
        assert response.status_code == 200


class TestLockout:
    async def test_account_locks_after_repeated_failures(self, client, make_user):
        user = await make_user()

        for _ in range(5):
            failed = await client.post(
                f"{API}/auth/login", json={"email": user.email, "password": "Nope-Nope-123!"}
            )
            assert failed.status_code == 401

        # Even the correct password is refused while the lockout window is open.
        response = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        assert response.status_code == 401
        assert "too many failed attempts" in response.json()["error"]["message"].lower()

    async def test_unknown_emails_are_throttled_too(self, client):
        """Otherwise the lockout response itself reveals which addresses exist."""
        email = "probe-target@test.glimmora.ai"

        for _ in range(5):
            await client.post(
                f"{API}/auth/login", json={"email": email, "password": "Nope-123456!"}
            )

        response = await client.post(
            f"{API}/auth/login", json={"email": email, "password": "Nope-123456!"}
        )
        assert "too many failed attempts" in response.json()["error"]["message"].lower()


class TestSessionRotation:
    async def test_refresh_rotates_the_token(self, client, make_user):
        user = await make_user()
        login = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        first_cookie = client.cookies.get(REFRESH_COOKIE)

        response = await client.post(f"{API}/auth/refresh")
        assert response.status_code == 200
        assert response.json()["access_token"] != login.json()["access_token"]
        assert client.cookies.get(REFRESH_COOKIE) != first_cookie

    async def test_replaying_a_rotated_token_revokes_the_whole_family(self, client, make_user):
        """A stolen cookie must not outlive its first reuse."""
        user = await make_user()
        await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        stolen = client.cookies.get(REFRESH_COOKIE)

        await client.post(f"{API}/auth/refresh")  # legitimate rotation

        client.cookies.set(REFRESH_COOKIE, stolen, path=f"{API}/auth")
        replay = await client.post(f"{API}/auth/refresh")
        assert replay.status_code == 401

        # The attacker's replay also killed the legitimate session.
        client.cookies.clear()
        await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )

    async def test_refresh_without_a_cookie_is_rejected(self, client):
        response = await client.post(f"{API}/auth/refresh")
        assert response.status_code == 401

    async def test_logout_revokes_the_session(self, client, make_user):
        user = await make_user()
        await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )

        assert (await client.post(f"{API}/auth/logout")).status_code == 204
        assert (await client.post(f"{API}/auth/refresh")).status_code == 401


class TestCurrentUser:
    async def test_me_returns_the_permission_set(self, as_role):
        client, user = await as_role(Role.HR_RESOURCING)

        response = await client.get(f"{API}/auth/me")
        assert response.status_code == 200

        body = response.json()
        assert body["email"] == user.email
        assert "resource.cost:view" in body["permissions"]
        assert "billing.rate:view" not in body["permissions"]

    async def test_me_requires_a_token(self, client):
        response = await client.get(f"{API}/auth/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    async def test_a_garbage_token_is_rejected(self, client):
        response = await client.get(
            f"{API}/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"}
        )
        assert response.status_code == 401

    async def test_an_expired_token_is_rejected(self, client, make_user):
        import jwt

        from app.core.config import settings

        user = await make_user()
        expired = jwt.encode(
            {
                "sub": str(user.id),
                "role": user.role.value,
                "jti": "expired",
                "typ": "access",
                "exp": int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()),
            },
            settings.JWT_SECRET,
            algorithm=settings.JWT_ALGORITHM,
        )

        response = await client.get(
            f"{API}/auth/me", headers={"Authorization": f"Bearer {expired}"}
        )
        assert response.status_code == 401
        assert "expired" in response.json()["error"]["message"].lower()

    async def test_a_token_signed_with_the_wrong_key_is_rejected(self, client, make_user):
        import jwt

        user = await make_user()
        forged = jwt.encode(
            {
                "sub": str(user.id),
                "role": "ADMIN",
                "jti": "forged",
                "typ": "access",
                "exp": int((datetime.now(UTC) + timedelta(minutes=30)).timestamp()),
            },
            "a-different-key-of-at-least-thirty-two-bytes-long",
            algorithm="HS256",
        )

        response = await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {forged}"})
        assert response.status_code == 401

    async def test_a_role_change_invalidates_existing_tokens(self, client, make_user, session):
        """A demotion must take effect at once, not when the token expires."""
        from sqlalchemy import select

        from app.models.identity import User

        user = await make_user(Role.ADMIN)
        login = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        token = login.json()["access_token"]

        stored = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
        stored.role = Role.SALES
        await session.commit()

        response = await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


class TestPasswordPolicy:
    @pytest.mark.parametrize(
        "weak",
        ["short", "password123", "abcdefghijklmnop", "123456789012345"],
    )
    async def test_weak_passwords_are_refused(self, as_role, weak):
        client, _ = await as_role(Role.ADMIN)

        response = await client.post(
            f"{API}/auth/change-password",
            json={"current_password": TEST_PASSWORD, "new_password": weak},
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"]

    async def test_changing_password_requires_the_current_one(self, as_role):
        client, _ = await as_role(Role.ADMIN)

        response = await client.post(
            f"{API}/auth/change-password",
            json={
                "current_password": "Not-The-Password-1!",
                "new_password": "Another-Good-Pass-9!",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["field"] == "current_password"

    async def test_new_password_must_differ(self, as_role):
        client, _ = await as_role(Role.ADMIN)

        response = await client.post(
            f"{API}/auth/change-password",
            json={"current_password": TEST_PASSWORD, "new_password": TEST_PASSWORD},
        )
        assert response.status_code == 422

    async def test_successful_change_revokes_every_session(self, client, make_user):
        user = await make_user()
        await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        token = (
            await client.post(
                f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
            )
        ).json()["access_token"]

        response = await client.post(
            f"{API}/auth/change-password",
            headers={"Authorization": f"Bearer {token}"},
            json={"current_password": TEST_PASSWORD, "new_password": "Brand-New-Secret-42!"},
        )
        assert response.status_code == 204
        assert (await client.post(f"{API}/auth/refresh")).status_code == 401

    async def test_forced_password_change_blocks_business_endpoints(self, client, make_user):
        """The user can still reach /auth, but nothing else."""
        user = await make_user(Role.ADMIN, must_change_password=True)
        login = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        assert (await client.get(f"{API}/auth/me", headers=headers)).status_code == 200

        blocked = await client.get(f"{API}/users", headers=headers)
        assert blocked.status_code == 403
        assert "change your password" in blocked.json()["error"]["message"].lower()


class TestRateLimiting:
    async def test_login_is_rate_limited(self, client, monkeypatch):
        from app.core import rate_limit as rate_limit_module
        from app.core.config import settings

        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 3)
        rate_limit_module.reset_limiter()

        payload = {"email": "rate-limit@test.glimmora.ai", "password": "Whatever-123456!"}
        statuses = [
            (await client.post(f"{API}/auth/login", json=payload)).status_code for _ in range(5)
        ]

        assert 429 in statuses
        limited = await client.post(f"{API}/auth/login", json=payload)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert limited.headers["Retry-After"]
