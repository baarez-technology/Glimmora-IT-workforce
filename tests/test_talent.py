"""Phase 6: the talent cloud, CV parsing and the document store.

The document tests carry the most weight here. In the Gulf an expired QID or
work permit stops a consultant working, which stops billing on a live
deployment — so expiry handling is revenue protection, not administration.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.permissions import Role

API = "/api/v1"

CV_TEXT = """Rahul Menon
Senior SAP FICO Consultant
rahul.menon@example.com | +974 5555 1234 | Doha, Qatar

PROFESSIONAL SUMMARY
SAP FICO consultant with hands-on S/4HANA implementation experience across
maritime and energy clients in the Gulf.

EXPERIENCE

Senior SAP FICO Consultant, Meridian Systems
Jan 2021 - Present
 - Led an S/4HANA finance rollout for a Qatari logistics group.

SAP FICO Consultant, Cedar Technology
Mar 2017 - Dec 2020

CERTIFICATIONS
PMP

SKILLS
SAP FICO, SAP S/4HANA, SAP MM, Power BI

Notice Period: 30 days
"""


def resource_payload(**overrides):
    payload = {
        "full_name": f"Test Candidate {uuid.uuid4().hex[:8]}",
        "resource_type": "CONSULTANT",
        "availability_status": "AVAILABLE",
        "notice_period_days": 30,
    }
    payload.update(overrides)
    return payload


def upload(name: str = "cv.txt", body: bytes | None = None):
    return {"file": (name, body or CV_TEXT.encode(), "text/plain")}


@pytest.fixture
async def resourcing(as_role):
    return await as_role(Role.HR_RESOURCING)


@pytest.fixture
async def resource(resourcing):
    client, _ = resourcing
    response = await client.post(f"{API}/resources", json=resource_payload())
    assert response.status_code == 201, response.text
    return response.json()


class TestResourceLifecycle:
    async def test_a_resource_gets_a_code_and_an_owner(self, resourcing):
        client, user = resourcing
        body = (await client.post(f"{API}/resources", json=resource_payload())).json()

        assert body["code"].startswith("GLM-")
        assert body["owner_id"] == str(user.id)
        assert body["review_status"] == "ACCEPTED"
        assert body["needs_review"] is False

    async def test_skills_resolve_onto_the_master(self, resourcing):
        client, _ = resourcing
        body = (
            await client.post(
                f"{API}/resources",
                json=resource_payload(
                    skills=[{"name": "k8s", "years": 4}, {"name": "Java", "years": 8}]
                ),
            )
        ).json()

        assert {skill["name"] for skill in body["skills"]} == {"Kubernetes", "Java"}

    async def test_ready_from_respects_the_notice_period(self, resourcing):
        """Available in 10 days with 60 days' notice means ready in 60."""
        client, _ = resourcing
        soon = (date.today() + timedelta(days=10)).isoformat()

        body = (
            await client.post(
                f"{API}/resources",
                json=resource_payload(available_from=soon, notice_period_days=60),
            )
        ).json()

        expected = (date.today() + timedelta(days=60)).isoformat()
        assert body["ready_from"] == expected

    async def test_the_seven_sow_categories_are_all_accepted(self, resourcing):
        client, _ = resourcing
        for category in (
            "EMPLOYEE",
            "BENCH",
            "CONSULTANT",
            "FREELANCER",
            "PARTNER_RESOURCE",
            "PREVIOUS_CANDIDATE",
            "PRE_VETTED_CANDIDATE",
        ):
            response = await client.post(
                f"{API}/resources", json=resource_payload(resource_type=category)
            )
            assert response.status_code == 201, category

    async def test_bench_lists_unbilled_capacity(self, resourcing):
        client, _ = resourcing
        # The suite shares one database, so the board is scoped to this test's
        # own two resources. What is under test is the filter, not pagination.
        marker = uuid.uuid4().hex[:10]
        benched = (
            await client.post(
                f"{API}/resources",
                json=resource_payload(
                    full_name=f"Bench {marker} Unbilled",
                    resource_type="BENCH",
                    availability_status="AVAILABLE",
                ),
            )
        ).json()
        deployed = (
            await client.post(
                f"{API}/resources",
                json=resource_payload(
                    full_name=f"Bench {marker} Billing",
                    resource_type="CONSULTANT",
                    availability_status="DEPLOYED",
                ),
            )
        ).json()

        bench = (
            await client.get(
                f"{API}/resources",
                params={"bench_only": True, "page_size": 100, "q": marker},
            )
        ).json()
        ids = [item["id"] for item in bench["items"]]

        assert benched["id"] in ids
        assert deployed["id"] not in ids

    async def test_archiving_removes_it_from_the_list(self, resourcing, resource):
        client, _ = resourcing
        assert (await client.delete(f"{API}/resources/{resource['id']}")).status_code == 204
        assert (await client.get(f"{API}/resources/{resource['id']}")).status_code == 404


class TestDuplicateDetection:
    async def test_an_exact_email_match_blocks_creation(self, resourcing):
        client, _ = resourcing
        email = f"dup-{uuid.uuid4().hex[:8]}@example.com"

        assert (
            await client.post(f"{API}/resources", json=resource_payload(email=email))
        ).status_code == 201

        clash = await client.post(f"{API}/resources", json=resource_payload(email=email))
        assert clash.status_code == 409
        assert "already exists" in clash.json()["error"]["message"].lower()

    async def test_the_preflight_check_reports_matches_with_confidence(self, resourcing):
        client, _ = resourcing
        email = f"preflight-{uuid.uuid4().hex[:8]}@example.com"
        await client.post(
            f"{API}/resources", json=resource_payload(email=email, phone="+974 5555 9999")
        )

        matches = (
            await client.get(f"{API}/resources/check-duplicate", params={"email": email})
        ).json()

        assert len(matches) == 1
        assert matches[0]["reason"] == "Same email address"
        assert matches[0]["confidence"] >= 0.9

    async def test_a_name_match_alone_is_reported_but_does_not_block(self, resourcing):
        """A shared name is a prompt to look, not grounds to refuse."""
        client, _ = resourcing
        name = f"Shared Name {uuid.uuid4().hex[:6]}"

        assert (
            await client.post(f"{API}/resources", json=resource_payload(full_name=name))
        ).status_code == 201
        assert (
            await client.post(f"{API}/resources", json=resource_payload(full_name=name))
        ).status_code == 201

        matches = (
            await client.get(f"{API}/resources/check-duplicate", params={"full_name": name})
        ).json()
        assert all(match["confidence"] < 0.9 for match in matches)


class TestCVParsing:
    async def test_parsing_creates_a_draft_awaiting_review(self, resourcing):
        client, _ = resourcing
        response = await client.post(f"{API}/resources/parse-cv", files=upload())
        assert response.status_code == 201

        body = response.json()
        assert body["provider"] == "null"
        assert body["overall_confidence"] > 0.5

        resource = (await client.get(f"{API}/resources/{body['resource_id']}")).json()
        assert resource["review_status"] == "PENDING_REVIEW"
        assert resource["needs_review"] is True
        assert resource["full_name"] == "Rahul Menon"
        assert resource["email"] == "rahul.menon@example.com"

    async def test_experience_years_are_computed_from_dates_not_claimed(self, resourcing):
        """A CV claiming "15+ years" must not override what its dates say."""
        client, _ = resourcing
        body = (await client.post(f"{API}/resources/parse-cv", files=upload())).json()
        resource = (await client.get(f"{API}/resources/{body['resource_id']}")).json()

        # Mar 2017 to now, merged with Jan 2021 to now.
        expected = (datetime.now(UTC).date() - date(2017, 3, 1)).days / 365
        assert resource["total_experience_years"] == pytest.approx(expected, abs=0.6)

    async def test_the_cv_file_is_stored_and_attached(self, resourcing):
        """A parse failure must never lose the document just uploaded."""
        client, _ = resourcing
        body = (await client.post(f"{API}/resources/parse-cv", files=upload())).json()

        documents = (await client.get(f"{API}/resources/{body['resource_id']}/documents")).json()
        assert any(document["doc_type"] == "CV" for document in documents)

    async def test_contact_details_always_require_confirmation(self, resourcing):
        client, _ = resourcing
        body = (await client.post(f"{API}/resources/parse-cv", files=upload())).json()

        required = set(body["confirmation_required"])
        assert {"email", "phone", "full_name", "total_experience_years"} <= required

    async def test_a_duplicate_is_surfaced_at_parse_time(self, resourcing):
        client, _ = resourcing
        await client.post(
            f"{API}/resources", json=resource_payload(email="rahul.menon@example.com")
        )

        body = (await client.post(f"{API}/resources/parse-cv", files=upload())).json()
        assert any(match["reason"] == "Same email address" for match in body["duplicates"])

    async def test_accepting_without_confirming_is_refused(self, resourcing):
        client, _ = resourcing
        body = (await client.post(f"{API}/resources/parse-cv", files=upload())).json()

        response = await client.post(
            f"{API}/resources/{body['resource_id']}/accept-parse", json={"confirmed_fields": []}
        )
        assert response.status_code == 422

    async def test_accepting_with_confirmations_makes_it_business_data(self, resourcing):
        client, _ = resourcing
        body = (await client.post(f"{API}/resources/parse-cv", files=upload())).json()

        response = await client.post(
            f"{API}/resources/{body['resource_id']}/accept-parse",
            json={
                "confirmed_fields": body["confirmation_required"],
                "updates": {"resource_type": "BENCH", "availability_status": "AVAILABLE"},
            },
        )
        assert response.status_code == 200
        assert response.json()["review_status"] == "ACCEPTED"
        assert response.json()["resource_type"] == "BENCH"

    async def test_an_unreadable_upload_gives_a_usable_message(self, resourcing):
        client, _ = resourcing
        response = await client.post(f"{API}/resources/parse-cv", files=upload("cv.txt", b"tiny"))
        assert response.status_code == 422
        assert "manually" in response.json()["error"]["message"].lower()

    async def test_parsing_is_audited(self, as_role, resourcing):
        client, _ = resourcing
        await client.post(f"{API}/resources/parse-cv", files=upload())

        admin, _ = await as_role(Role.ADMIN)
        logs = (await admin.get(f"{API}/audit", params={"action": "CV_PARSED"})).json()
        assert any("Rahul Menon" in entry["summary"] for entry in logs["items"])


class TestDocumentExpiry:
    async def _upload(self, client, resource_id, *, doc_type, expiry: date | None):
        data = {"doc_type": doc_type}
        if expiry:
            data["expiry_date"] = expiry.isoformat()
        return await client.post(
            f"{API}/resources/{resource_id}/documents",
            files={"file": ("visa.pdf", b"%PDF-1.4 fake", "application/pdf")},
            data=data,
        )

    async def test_a_work_authorisation_document_must_carry_an_expiry(self, resourcing, resource):
        client, _ = resourcing
        response = await self._upload(client, resource["id"], doc_type="QID", expiry=None)

        assert response.status_code == 422
        assert "stops billing" in response.json()["error"]["details"][0]["message"]

    @pytest.mark.parametrize(
        ("offset_days", "expected_state"),
        [(-10, "EXPIRED"), (20, "EXPIRING_SOON"), (400, "VALID")],
    )
    async def test_expiry_states(self, resourcing, resource, offset_days, expected_state):
        client, _ = resourcing
        response = await self._upload(
            client,
            resource["id"],
            doc_type="WORK_PERMIT",
            expiry=date.today() + timedelta(days=offset_days),
        )
        assert response.status_code == 201
        assert response.json()["expiry"]["state"] == expected_state

    async def test_an_expired_permit_flags_the_resource_as_blocked(self, resourcing, resource):
        client, _ = resourcing
        await self._upload(
            client, resource["id"], doc_type="QID", expiry=date.today() - timedelta(days=5)
        )

        body = (await client.get(f"{API}/resources/{resource['id']}")).json()
        assert body["blocks_deployment"] is True
        assert body["work_authorisation"]["state"] == "EXPIRED"
        # The visa status field is derived, so it cannot drift out of date.
        assert body["visa_status"] == "EXPIRED"

    async def test_the_worst_document_decides_the_overall_state(self, resourcing, resource):
        """A valid passport does not compensate for an expired work permit."""
        client, _ = resourcing
        await self._upload(
            client, resource["id"], doc_type="PASSPORT", expiry=date.today() + timedelta(days=900)
        )
        await self._upload(
            client, resource["id"], doc_type="QID", expiry=date.today() - timedelta(days=2)
        )

        body = (await client.get(f"{API}/resources/{resource['id']}")).json()
        assert body["work_authorisation"]["state"] == "EXPIRED"

    async def test_a_cv_has_no_bearing_on_work_authorisation(self, resourcing, resource):
        client, _ = resourcing
        await client.post(
            f"{API}/resources/{resource['id']}/documents",
            files={"file": ("cv.pdf", b"%PDF-1.4 fake", "application/pdf")},
            data={"doc_type": "CV"},
        )

        body = (await client.get(f"{API}/resources/{resource['id']}")).json()
        assert body["work_authorisation"]["state"] == "NOT_APPLICABLE"
        assert body["blocks_deployment"] is False

    async def test_the_expiring_board_separates_expired_from_expiring(self, resourcing, resource):
        client, _ = resourcing
        await self._upload(
            client, resource["id"], doc_type="VISA", expiry=date.today() - timedelta(days=3)
        )
        await self._upload(
            client, resource["id"], doc_type="WORK_PERMIT", expiry=date.today() + timedelta(days=15)
        )

        board = (await client.get(f"{API}/documents/expiring")).json()
        assert board["counts"]["expired"] >= 1
        assert board["counts"]["expiring_soon"] >= 1
        assert all(item["expiry"]["is_expired"] for item in board["expired"])
        assert all(not item["expiry"]["is_expired"] for item in board["expiring_soon"])


class TestDocumentSecurity:
    async def _upload_visa(self, client, resource_id):
        return await client.post(
            f"{API}/resources/{resource_id}/documents",
            files={"file": ("visa.pdf", b"%PDF-1.4 fake visa", "application/pdf")},
            data={
                "doc_type": "VISA",
                "expiry_date": (date.today() + timedelta(days=200)).isoformat(),
                "reference_number": "V-99887766",
            },
        )

    async def test_an_executable_upload_is_refused(self, resourcing, resource):
        client, _ = resourcing
        response = await client.post(
            f"{API}/resources/{resource['id']}/documents",
            files={"file": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
            data={"doc_type": "OTHER"},
        )
        assert response.status_code == 415

    async def test_a_mislabelled_file_is_refused_on_its_magic_bytes(self, resourcing, resource):
        client, _ = resourcing
        response = await client.post(
            f"{API}/resources/{resource['id']}/documents",
            files={"file": ("notes.pdf", b"this is not a pdf", "application/pdf")},
            data={"doc_type": "OTHER"},
        )
        assert response.status_code == 415

    async def test_resourcing_can_download_a_personal_document(self, resourcing, resource):
        client, _ = resourcing
        document = (await self._upload_visa(client, resource["id"])).json()

        response = await client.get(f"{API}/documents/{document['id']}/download")
        assert response.status_code == 200
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert b"fake visa" in response.content

    async def test_management_may_see_the_document_but_not_download_it(
        self, as_role, resourcing, resource
    ):
        """Oversight without extra copies of passports leaving the system."""
        hr_client, _ = resourcing
        document = (await self._upload_visa(hr_client, resource["id"])).json()

        management, _ = await as_role(Role.MANAGEMENT)
        listing = (await management.get(f"{API}/resources/{resource['id']}/documents")).json()
        assert any(item["id"] == document["id"] for item in listing)

        blocked = await management.get(f"{API}/documents/{document['id']}/download")
        assert blocked.status_code == 403

    async def test_sales_cannot_read_personal_documents_at_all(self, as_role, resourcing, resource):
        hr_client, _ = resourcing
        document = (await self._upload_visa(hr_client, resource["id"])).json()

        sales, _ = await as_role(Role.SALES)
        assert (await sales.get(f"{API}/resources/{resource['id']}/documents")).status_code == 403
        assert (await sales.get(f"{API}/documents/{document['id']}/download")).status_code == 403

    async def test_management_sees_the_reference_number_but_still_cannot_download(
        self, as_role, resourcing, resource
    ):
        """Management holds document.personal:view but not :download.

        Oversight of what is on file, without extra copies of passports leaving
        the system (SECURITY.md section 3).
        """
        hr_client, _ = resourcing
        await self._upload_visa(hr_client, resource["id"])

        hr_view = (await hr_client.get(f"{API}/resources/{resource['id']}/documents")).json()
        assert hr_view[0]["reference_number"] == "V-99887766"

        management, _ = await as_role(Role.MANAGEMENT)
        mgmt_view = (await management.get(f"{API}/resources/{resource['id']}/documents")).json()
        assert mgmt_view[0]["reference_number"] == "V-99887766"
        assert mgmt_view[0]["can_download"] is False

    def test_the_reference_number_is_stripped_without_the_view_permission(self):
        """No current role lacks it, so the guard is asserted directly."""
        from app.api.v1.resources import _document_response
        from app.models.talent import Document, DocumentType, ResourceDocument

        link = ResourceDocument(
            id=uuid.uuid4(),
            resource_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            doc_type=DocumentType.PASSPORT,
            reference_number="P-12345678",
            created_at=datetime.now(UTC),
        )
        link.document = Document(
            storage_key="k",
            original_filename="passport.pdf",
            content_type="application/pdf",
            size_bytes=10,
            checksum_sha256="x" * 64,
        )

        hidden = _document_response(
            link, today=date.today(), can_view_personal=False, can_download=False
        )
        assert hidden.reference_number is None
        assert hidden.can_download is False

    async def test_every_download_is_audited(self, as_role, resourcing, resource):
        """For a visa, knowing who took a copy is the point of the control."""
        client, _ = resourcing
        document = (await self._upload_visa(client, resource["id"])).json()
        await client.get(f"{API}/documents/{document['id']}/download")

        admin, _ = await as_role(Role.ADMIN)
        logs = (await admin.get(f"{API}/audit", params={"action": "DOCUMENT_DOWNLOADED"})).json()
        assert logs["total"] >= 1
        assert any("VISA" in entry["summary"] for entry in logs["items"])

    async def test_anonymous_download_is_rejected(self, client, resourcing, resource):
        hr_client, _ = resourcing
        document = (await self._upload_visa(hr_client, resource["id"])).json()

        del hr_client.headers["Authorization"]
        assert (
            await hr_client.get(f"{API}/documents/{document['id']}/download")
        ).status_code == 401


class TestTalentAuthorization:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (Role.ADMIN, 201),
            (Role.HR_RESOURCING, 201),
            (Role.SALES, 403),
            (Role.MANAGEMENT, 403),
        ],
    )
    async def test_create_resource(self, as_role, role, expected):
        client, _ = await as_role(role)
        response = await client.post(f"{API}/resources", json=resource_payload())
        assert response.status_code == expected

    @pytest.mark.parametrize("role", list(Role))
    async def test_every_role_can_read_resources(self, as_role, role):
        client, _ = await as_role(role)
        assert (await client.get(f"{API}/resources")).status_code == 200

    async def test_sales_never_sees_consultant_cost(self, as_role, resourcing):
        """Sales negotiates the client price; Resourcing negotiates the cost."""
        hr_client, _ = resourcing
        created = (
            await hr_client.post(
                f"{API}/resources",
                json=resource_payload(
                    expected_cost_amount="12000",
                    expected_cost_currency="QAR",
                    expected_cost_unit="MONTHLY",
                ),
            )
        ).json()
        assert float(created["expected_cost_amount"]) == 12000.0

        sales, _ = await as_role(Role.SALES)
        seen = (await sales.get(f"{API}/resources/{created['id']}")).json()
        assert seen["expected_cost_amount"] is None

    async def test_anonymous_callers_are_rejected(self, client):
        for path in ("/resources", "/resources/available", "/documents/expiring"):
            assert (await client.get(f"{API}{path}")).status_code == 401
