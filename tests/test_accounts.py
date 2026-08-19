"""Phase 4: accounts, the routing graph, contacts, projects and activities."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.permissions import Role

API = "/api/v1"


def account_payload(**overrides):
    payload = {
        "name": f"Test Account {uuid.uuid4().hex[:8]}",
        "account_type": "CUSTOMER",
        "country": "QA",
        "city": "Doha",
        "industry": "Energy",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
async def sales(as_role):
    return await as_role(Role.SALES)


@pytest.fixture
async def account(sales):
    client, _ = sales
    response = await client.post(f"{API}/accounts", json=account_payload())
    assert response.status_code == 201, response.text
    return response.json()


class TestAccounts:
    async def test_create_returns_the_account_with_its_counts(self, sales):
        client, user = sales

        response = await client.post(
            f"{API}/accounts",
            json=account_payload(name="Harbour Freight QA", is_existing_customer=True),
        )
        assert response.status_code == 201

        body = response.json()
        assert body["name"] == "Harbour Freight QA"
        assert body["is_existing_customer"] is True
        assert body["contact_count"] == 0
        assert body["project_count"] == 0
        # An unowned account is an account nobody follows up.
        assert body["owner_id"] == str(user.id)

    async def test_duplicate_name_in_the_same_country_is_a_conflict(self, sales):
        client, _ = sales
        payload = account_payload(name="Duplicate Test Co")

        assert (await client.post(f"{API}/accounts", json=payload)).status_code == 201
        clash = await client.post(f"{API}/accounts", json=payload)

        assert clash.status_code == 409
        assert clash.json()["error"]["code"] == "CONFLICT"

    async def test_the_same_name_in_another_country_is_allowed(self, sales):
        """Group companies genuinely share a name across markets."""
        client, _ = sales

        assert (
            await client.post(
                f"{API}/accounts", json=account_payload(name="Global Holdings", country="QA")
            )
        ).status_code == 201
        assert (
            await client.post(
                f"{API}/accounts", json=account_payload(name="Global Holdings", country="AE")
            )
        ).status_code == 201

    async def test_filter_by_type_and_customer_flag(self, sales):
        client, _ = sales
        await client.post(
            f"{API}/accounts",
            json=account_payload(name="Filter Partner Co", account_type="PARTNER"),
        )
        await client.post(
            f"{API}/accounts",
            json=account_payload(name="Filter Customer Co", is_existing_customer=True),
        )

        partners = (await client.get(f"{API}/accounts", params={"account_type": "PARTNER"})).json()
        assert all(item["account_type"] == "PARTNER" for item in partners["items"])

        customers = (
            await client.get(f"{API}/accounts", params={"is_existing_customer": True})
        ).json()
        assert all(item["is_existing_customer"] for item in customers["items"])

    async def test_search_matches_name_and_industry(self, sales):
        client, _ = sales
        await client.post(
            f"{API}/accounts",
            json=account_payload(name="Searchable Maritime Co", industry="Maritime"),
        )

        found = (await client.get(f"{API}/accounts", params={"q": "searchable"})).json()
        assert any("Searchable Maritime" in item["name"] for item in found["items"])

    async def test_archive_removes_it_from_the_list(self, sales, account):
        client, _ = sales

        assert (await client.delete(f"{API}/accounts/{account['id']}")).status_code == 204
        assert (await client.get(f"{API}/accounts/{account['id']}")).status_code == 404

        listed = (await client.get(f"{API}/accounts", params={"page_size": 100})).json()
        assert account["id"] not in [item["id"] for item in listed["items"]]

    async def test_unknown_sort_field_is_rejected(self, sales):
        client, _ = sales
        response = await client.get(f"{API}/accounts", params={"sort": "-not_a_column"})
        assert response.status_code == 422


class TestAddressabilitySignals:
    """The Phase 9 inputs, previewed so gaps are visible before scoring exists."""

    async def test_a_bare_account_reports_every_missing_signal(self, sales, account):
        client, _ = sales
        signals = (await client.get(f"{API}/accounts/{account['id']}")).json()["addressability"]

        assert signals["signals_met"] == 0
        assert signals["signals_total"] == 5
        assert len(signals["missing"]) == 5
        assert any("decision maker" in item.lower() for item in signals["missing"])

    async def test_signals_rise_as_the_commercial_facts_are_recorded(self, sales, account):
        client, _ = sales

        await client.patch(
            f"{API}/accounts/{account['id']}",
            json={
                "is_existing_customer": True,
                "is_approved_vendor": True,
                "contract_outsourcing_friendly": True,
            },
        )
        await client.post(
            f"{API}/contacts",
            json={
                "account_id": account["id"],
                "full_name": "Decision Maker",
                "title": "CIO",
                "is_decision_maker": True,
            },
        )

        signals = (await client.get(f"{API}/accounts/{account['id']}")).json()["addressability"]

        assert signals["existing_customer"] is True
        assert signals["approved_vendor"] is True
        assert signals["decision_maker_known"] is True
        assert signals["signals_met"] == 4
        # Only the route is still missing — no partner or prime recorded.
        assert signals["missing"] == ["No partner or prime route recorded"]

    async def test_an_msa_counts_as_approved_vendor(self, sales, account):
        client, _ = sales
        await client.patch(f"{API}/accounts/{account['id']}", json={"has_msa": True})

        signals = (await client.get(f"{API}/accounts/{account['id']}")).json()["addressability"]
        assert signals["approved_vendor"] is True


class TestRoutingGraph:
    """Answers the SOW question: through which route do we approach this client?"""

    async def test_a_preferred_prime_route_satisfies_the_route_signal(self, sales):
        client, _ = sales
        end_client = (
            await client.post(f"{API}/accounts", json=account_payload(name="Gov Authority Test"))
        ).json()
        prime = (
            await client.post(
                f"{API}/accounts",
                json=account_payload(name="Prime Route Co", account_type="PRIME_CONTRACTOR"),
            )
        ).json()

        route = await client.post(
            f"{API}/accounts/{end_client['id']}/routes",
            json={
                "to_account_id": prime["id"],
                "relation_type": "SUBCONTRACTS_THROUGH",
                "is_preferred_route": True,
            },
        )
        assert route.status_code == 201
        assert route.json()["to_account_name"] == "Prime Route Co"
        assert route.json()["to_account_type"] == "PRIME_CONTRACTOR"

        signals = (await client.get(f"{API}/accounts/{end_client['id']}")).json()["addressability"]
        assert signals["partner_or_prime_route"] is True

    async def test_an_account_cannot_route_through_itself(self, sales, account):
        client, _ = sales
        response = await client.post(
            f"{API}/accounts/{account['id']}/routes",
            json={"to_account_id": account["id"], "relation_type": "PARTNER_OF"},
        )
        assert response.status_code == 422
        assert "itself" in response.json()["error"]["message"].lower()

    async def test_a_duplicate_route_is_rejected(self, sales, account):
        client, _ = sales
        partner = (
            await client.post(
                f"{API}/accounts",
                json=account_payload(name="Dup Route Partner", account_type="PARTNER"),
            )
        ).json()
        body = {"to_account_id": partner["id"], "relation_type": "PARTNER_OF"}

        assert (
            await client.post(f"{API}/accounts/{account['id']}/routes", json=body)
        ).status_code == 201
        assert (
            await client.post(f"{API}/accounts/{account['id']}/routes", json=body)
        ).status_code == 409

    async def test_only_one_route_can_be_preferred(self, sales, account):
        client, _ = sales
        first = (
            await client.post(
                f"{API}/accounts", json=account_payload(name="Route A", account_type="PARTNER")
            )
        ).json()
        second = (
            await client.post(
                f"{API}/accounts", json=account_payload(name="Route B", account_type="PARTNER")
            )
        ).json()

        await client.post(
            f"{API}/accounts/{account['id']}/routes",
            json={
                "to_account_id": first["id"],
                "relation_type": "PARTNER_OF",
                "is_preferred_route": True,
            },
        )
        await client.post(
            f"{API}/accounts/{account['id']}/routes",
            json={
                "to_account_id": second["id"],
                "relation_type": "PARTNER_OF",
                "is_preferred_route": True,
            },
        )

        routes = (await client.get(f"{API}/accounts/{account['id']}/routes")).json()
        preferred = [route for route in routes if route["is_preferred_route"]]
        assert len(preferred) == 1
        assert preferred[0]["to_account_id"] == second["id"]

    async def test_routing_to_an_unknown_account_is_a_404(self, sales, account):
        client, _ = sales
        response = await client.post(
            f"{API}/accounts/{account['id']}/routes",
            json={"to_account_id": str(uuid.uuid4()), "relation_type": "PARTNER_OF"},
        )
        assert response.status_code == 404

    async def test_a_route_can_be_removed(self, sales, account):
        client, _ = sales
        partner = (
            await client.post(
                f"{API}/accounts",
                json=account_payload(name="Removable Route", account_type="PARTNER"),
            )
        ).json()
        route = (
            await client.post(
                f"{API}/accounts/{account['id']}/routes",
                json={"to_account_id": partner["id"], "relation_type": "PARTNER_OF"},
            )
        ).json()

        assert (
            await client.delete(f"{API}/accounts/{account['id']}/routes/{route['id']}")
        ).status_code == 204
        assert (await client.get(f"{API}/accounts/{account['id']}/routes")).json() == []


class TestContacts:
    async def test_decision_makers_are_counted_on_the_account(self, sales, account):
        client, _ = sales

        await client.post(
            f"{API}/contacts",
            json={
                "account_id": account["id"],
                "full_name": "Aisha Rahman",
                "title": "CIO",
                "is_decision_maker": True,
            },
        )
        await client.post(
            f"{API}/contacts",
            json={"account_id": account["id"], "full_name": "Ravi Patel", "title": "Analyst"},
        )

        body = (await client.get(f"{API}/accounts/{account['id']}")).json()
        assert body["contact_count"] == 2
        assert body["decision_maker_count"] == 1

    async def test_only_one_primary_contact_per_account(self, sales, account):
        client, _ = sales
        for name in ("First Primary", "Second Primary"):
            await client.post(
                f"{API}/contacts",
                json={"account_id": account["id"], "full_name": name, "is_primary": True},
            )

        contacts = (
            await client.get(f"{API}/contacts", params={"account_id": account["id"]})
        ).json()
        assert sum(1 for contact in contacts["items"] if contact["is_primary"]) == 1

    async def test_email_is_normalised_to_lowercase(self, sales, account):
        client, _ = sales
        response = await client.post(
            f"{API}/contacts",
            json={
                "account_id": account["id"],
                "full_name": "Mixed Case",
                "email": "Mixed.Case@Example.COM",
            },
        )
        assert response.json()["email"] == "mixed.case@example.com"

    async def test_contact_on_an_unknown_account_is_a_404(self, sales):
        client, _ = sales
        response = await client.post(
            f"{API}/contacts", json={"account_id": str(uuid.uuid4()), "full_name": "Ghost"}
        )
        assert response.status_code == 404


class TestProjects:
    async def test_a_project_carries_its_technology_stack(self, sales, account):
        client, _ = sales
        technologies = (await client.get(f"{API}/technologies")).json()
        sap = next(tech for tech in technologies if tech["name"] == "SAP")

        response = await client.post(
            f"{API}/projects",
            json={
                "account_id": account["id"],
                "name": "S/4HANA Rollout Test",
                "status": "ACTIVE",
                "technology_ids": [sap["id"]],
            },
        )
        assert response.status_code == 201

        body = response.json()
        assert body["account_name"] == account["name"]
        assert [tech["name"] for tech in body["technologies"]] == ["SAP"]

    async def test_end_date_cannot_precede_start_date(self, sales, account):
        client, _ = sales
        response = await client.post(
            f"{API}/projects",
            json={
                "account_id": account["id"],
                "name": "Backwards Project",
                "start_date": "2026-06-01",
                "end_date": "2026-01-01",
            },
        )
        assert response.status_code == 422

    async def test_the_prime_contractor_must_be_a_prime_or_partner(self, sales, account):
        client, _ = sales
        wrong_type = (
            await client.post(
                f"{API}/accounts", json=account_payload(name="Not A Prime", account_type="CUSTOMER")
            )
        ).json()

        response = await client.post(
            f"{API}/projects",
            json={
                "account_id": account["id"],
                "name": "Wrong Prime Project",
                "prime_contractor_id": wrong_type["id"],
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["field"] == "prime_contractor_id"

    async def test_unknown_technologies_are_rejected(self, sales, account):
        client, _ = sales
        response = await client.post(
            f"{API}/projects",
            json={
                "account_id": account["id"],
                "name": "Bad Tech Project",
                "technology_ids": [str(uuid.uuid4())],
            },
        )
        assert response.status_code == 422

    async def test_technologies_can_be_replaced(self, sales, account):
        client, _ = sales
        technologies = (await client.get(f"{API}/technologies")).json()
        sap = next(t for t in technologies if t["name"] == "SAP")
        cloud = next(t for t in technologies if t["name"] == "Cloud")

        project = (
            await client.post(
                f"{API}/projects",
                json={
                    "account_id": account["id"],
                    "name": "Swap Tech Project",
                    "technology_ids": [sap["id"]],
                },
            )
        ).json()

        updated = await client.patch(
            f"{API}/projects/{project['id']}", json={"technology_ids": [cloud["id"]]}
        )
        assert [t["name"] for t in updated.json()["technologies"]] == ["Cloud"]


class TestActivityTimeline:
    async def test_an_activity_appears_on_the_account_timeline(self, sales, account):
        client, user = sales

        created = await client.post(
            f"{API}/activities",
            json={
                "activity_type": "CALL",
                "subject": "Discussed upcoming SAP requirement",
                "account_id": account["id"],
            },
        )
        assert created.status_code == 201
        assert created.json()["user_name"] == user.full_name

        timeline = (await client.get(f"{API}/accounts/{account['id']}/timeline")).json()
        assert timeline["total"] == 1
        assert timeline["items"][0]["subject"] == "Discussed upcoming SAP requirement"
        assert timeline["items"][0]["account_name"] == account["name"]

    async def test_an_activity_must_attach_to_something(self, sales):
        client, _ = sales
        response = await client.post(
            f"{API}/activities", json={"activity_type": "NOTE", "subject": "Floating note"}
        )
        assert response.status_code == 422

    async def test_activities_cannot_be_recorded_in_the_future(self, sales, account):
        client, _ = sales
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()

        response = await client.post(
            f"{API}/activities",
            json={
                "activity_type": "MEETING",
                "subject": "Time travel meeting",
                "account_id": account["id"],
                "occurred_at": tomorrow,
            },
        )
        assert response.status_code == 422
        assert "follow-up" in response.json()["error"]["details"][0]["message"].lower()

    async def test_overdue_follow_ups_are_flagged_and_listed_soonest_first(self, sales, account):
        client, _ = sales
        overdue = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        upcoming = (datetime.now(UTC) + timedelta(days=5)).isoformat()

        await client.post(
            f"{API}/activities",
            json={
                "activity_type": "TASK",
                "subject": "Overdue chase",
                "account_id": account["id"],
                "follow_up_at": overdue,
            },
        )
        await client.post(
            f"{API}/activities",
            json={
                "activity_type": "TASK",
                "subject": "Future chase",
                "account_id": account["id"],
                "follow_up_at": upcoming,
            },
        )

        follow_ups = (await client.get(f"{API}/activities/follow-ups")).json()
        subjects = [item["subject"] for item in follow_ups["items"]]
        assert subjects[0] == "Overdue chase", "the soonest follow-up must come first"

        overdue_entry = next(i for i in follow_ups["items"] if i["subject"] == "Overdue chase")
        assert overdue_entry["is_follow_up_overdue"] is True

        only_overdue = (
            await client.get(f"{API}/activities/follow-ups", params={"overdue_only": True})
        ).json()
        assert [item["subject"] for item in only_overdue["items"]] == ["Overdue chase"]

    async def test_completing_a_follow_up_removes_it_from_the_queue(self, sales, account):
        client, _ = sales
        activity = (
            await client.post(
                f"{API}/activities",
                json={
                    "activity_type": "TASK",
                    "subject": "Completable chase",
                    "account_id": account["id"],
                    "follow_up_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                },
            )
        ).json()

        completed = await client.post(f"{API}/activities/{activity['id']}/complete")
        assert completed.status_code == 200
        assert completed.json()["is_follow_up_open"] is False

        follow_ups = (await client.get(f"{API}/activities/follow-ups")).json()
        assert "Completable chase" not in [item["subject"] for item in follow_ups["items"]]

    async def test_completing_an_activity_with_no_follow_up_is_rejected(self, sales, account):
        client, _ = sales
        activity = (
            await client.post(
                f"{API}/activities",
                json={
                    "activity_type": "NOTE",
                    "subject": "No follow-up here",
                    "account_id": account["id"],
                },
            )
        ).json()

        response = await client.post(f"{API}/activities/{activity['id']}/complete")
        assert response.status_code == 422

    async def test_you_can_only_delete_your_own_activities(self, client, as_role, make_user):
        """Someone else's record of a client call is history, not your note."""
        author_client, _ = await as_role(Role.SALES)
        account_id = (await author_client.post(f"{API}/accounts", json=account_payload())).json()[
            "id"
        ]
        activity = (
            await author_client.post(
                f"{API}/activities",
                json={
                    "activity_type": "NOTE",
                    "subject": "Someone else's note",
                    "account_id": account_id,
                },
            )
        ).json()

        other_client, _ = await as_role(Role.HR_RESOURCING)
        response = await other_client.delete(f"{API}/activities/{activity['id']}")
        assert response.status_code == 422


class TestAccountsAuthorization:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (Role.ADMIN, 201),
            (Role.SALES, 201),
            (Role.MANAGEMENT, 403),
            (Role.HR_RESOURCING, 403),
        ],
    )
    async def test_create_account(self, as_role, role, expected):
        client, _ = await as_role(role)
        response = await client.post(f"{API}/accounts", json=account_payload())
        assert response.status_code == expected

    @pytest.mark.parametrize("role", list(Role))
    async def test_every_role_can_read_accounts(self, as_role, role):
        """Resourcing needs account context to judge redeployment options."""
        client, _ = await as_role(role)
        assert (await client.get(f"{API}/accounts")).status_code == 200

    @pytest.mark.parametrize(
        ("role", "expected"),
        [(Role.SALES, 201), (Role.HR_RESOURCING, 201), (Role.MANAGEMENT, 403)],
    )
    async def test_activity_logging(self, as_role, role, expected, sales, account):
        """Resourcing logs candidate conversations; Management only observes."""
        client, _ = await as_role(role)
        response = await client.post(
            f"{API}/activities",
            json={
                "activity_type": "NOTE",
                "subject": f"Note from {role.value}",
                "account_id": account["id"],
            },
        )
        assert response.status_code == expected

    async def test_anonymous_callers_are_rejected(self, client):
        for path in ("/accounts", "/contacts", "/projects", "/activities", "/technologies"):
            assert (await client.get(f"{API}{path}")).status_code == 401


class TestAccountAudit:
    async def test_creating_an_account_is_audited(self, as_role):
        admin_client, _ = await as_role(Role.ADMIN)
        await admin_client.post(f"{API}/accounts", json=account_payload(name="Audited Account Co"))

        logs = (await admin_client.get(f"{API}/audit", params={"action": "ACCOUNT_CREATED"})).json()
        assert any("Audited Account Co" in entry["summary"] for entry in logs["items"])

    async def test_changing_a_commercial_flag_is_audited_with_a_diff(self, as_role):
        client, _ = await as_role(Role.ADMIN)
        account = (
            await client.post(f"{API}/accounts", json=account_payload(name="Flag Change Co"))
        ).json()

        await client.patch(f"{API}/accounts/{account['id']}", json={"is_approved_vendor": True})

        logs = (await client.get(f"{API}/audit", params={"action": "ACCOUNT_UPDATED"})).json()
        entry = next(e for e in logs["items"] if e["entity_id"] == account["id"])
        assert entry["changes"]["is_approved_vendor"] == {"from": False, "to": True}
