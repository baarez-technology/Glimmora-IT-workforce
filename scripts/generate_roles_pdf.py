"""Generate the Roles and Field Visibility reference PDF, straight from the code.

Run from anywhere:  python backend/scripts/generate_roles_pdf.py
Requires reportlab, which is in requirements-dev.txt -- this is a documentation
tool, not something the application imports at runtime.


Everything here is introspected from `app.core.permissions` and the API
serializers -- nothing is transcribed by hand, so the document cannot drift from
what the running system actually enforces.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

# backend/scripts/generate_roles_pdf.py -> repository root.
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import A4, landscape  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.core.permissions import (  # noqa: E402
    FIELD_PERMISSIONS,
    ROLE_DESCRIPTIONS,
    ROLE_LABELS,
    Permission,
    Role,
    permissions_for,
)

P_ = Permission
ROLES = [Role.ADMIN, Role.MANAGEMENT, Role.SALES, Role.HR_RESOURCING]
SHORT = {
    Role.ADMIN: "Admin",
    Role.MANAGEMENT: "Management",
    Role.SALES: "Sales",
    Role.HR_RESOURCING: "Resourcing",
}

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6B7280")
RULE = colors.HexColor("#E5E7EB")
HEADER_BG = colors.HexColor("#111827")
ZEBRA = colors.HexColor("#F9FAFB")
YES = colors.HexColor("#047857")
NO = colors.HexColor("#B91C1C")
PARTIAL = colors.HexColor("#B45309")
ACCENT = colors.HexColor("#1D4ED8")

# --------------------------------------------------------------------- styles
sheet = getSampleStyleSheet()


def style(name: str, **kwargs) -> ParagraphStyle:
    base = {
        "name": name,
        "fontName": "Helvetica",
        "fontSize": 8.5,
        "leading": 11,
        "textColor": INK,
        "alignment": TA_LEFT,
    }
    base.update(kwargs)
    return ParagraphStyle(**base)


S = {
    "title": style("title", fontName="Helvetica-Bold", fontSize=24, leading=28),
    "subtitle": style("subtitle", fontSize=11, leading=15, textColor=MUTED),
    "h1": style("h1", fontName="Helvetica-Bold", fontSize=15, leading=19, spaceBefore=12,
                spaceAfter=6),
    "h2": style("h2", fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=10,
                spaceAfter=4),
    "body": style("body", fontSize=9, leading=12.5, spaceAfter=5),
    "small": style("small", fontSize=7.5, leading=10, textColor=MUTED),
    "cell": style("cell", fontSize=7.6, leading=9.6),
    "cellb": style("cellb", fontName="Helvetica-Bold", fontSize=7.6, leading=9.6),
    "cellh": style("cellh", fontName="Helvetica-Bold", fontSize=7.6, leading=9.6,
                   textColor=colors.white),
    "note": style("note", fontSize=8, leading=11, textColor=MUTED),
}


def para(text: str, key: str = "body") -> Paragraph:
    return Paragraph(text, S[key])


def mark(value: str) -> Paragraph:
    """A cell marker whose meaning is carried by the word, not only the colour."""
    palette = {
        "full": (YES, "full"),
        "yes": (YES, "yes"),
        "read": (ACCENT, "read"),
        "no": (NO, "no"),
        "part": (PARTIAL, "partial"),
    }
    colour, label = palette[value]
    return Paragraph(f'<font color="#{colour.hexval()[2:]}"><b>{label}</b></font>', S["cell"])


def table(data, widths, *, zebra_from: int = 1) -> Table:
    t = Table(data, colWidths=widths, repeatRows=1)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
    ]
    for row in range(zebra_from, len(data)):
        if (row - zebra_from) % 2 == 1:
            commands.append(("BACKGROUND", (0, row), (-1, row), ZEBRA))
    t.setStyle(TableStyle(commands))
    return t


def header_row(labels: list[str]) -> list[Paragraph]:
    return [Paragraph(label, S["cellh"]) for label in labels]


def code(text: str, size: str = "6.5") -> str:
    return f"<font face='Courier' size='{size}'>{text}</font>"


# ------------------------------------------------------------------- content
story: list = []

story.append(para("Glimmora IT Workforce", "title"))
story.append(Spacer(1, 3))
story.append(para("Roles and field visibility -- the complete reference", "subtitle"))
story.append(Spacer(1, 10))
story.append(
    para(
        f"Generated from the running code on {date.today():%d %B %Y}. Every table below is "
        f"introspected from {code('app.core.permissions', '7.5')} and the API serializers, so this "
        "document describes what the system actually enforces rather than what any specification "
        "says it should.",
        "note",
    )
)
story.append(Spacer(1, 14))

# --- 1. the four roles ------------------------------------------------------
story.append(para("1. The four roles", "h1"))
story.append(
    para(
        "Access control has two layers. <b>Action permissions</b> decide whether a role may call "
        "an endpoint at all. <b>Field permissions</b> decide whether a role may see a particular "
        "value in the response -- and where a field is restricted, the key is removed from the "
        "payload entirely rather than returned as null, so a caller cannot tell "
        "&quot;no value&quot; from &quot;not allowed&quot; by inspecting the shape."
    )
)

rows = [header_row(["Role", "Purpose", "Permissions"])]
for role in ROLES:
    rows.append(
        [
            para(f"<b>{ROLE_LABELS[role]}</b><br/>{code(role.value, '7')}", "cell"),
            para(ROLE_DESCRIPTIONS[role], "cell"),
            para(f"<b>{len(permissions_for(role))}</b> of {len(list(Permission))}", "cell"),
        ]
    )
story.append(table(rows, [46 * mm, 180 * mm, 43 * mm]))

story.append(Spacer(1, 8))
story.append(
    para(
        "<b>How to read the tables.</b> "
        "<font color='#047857'><b>full</b></font> = create, read and update. "
        "<font color='#1D4ED8'><b>read</b></font> = view only. "
        "<font color='#B45309'><b>partial</b></font> = some operations only, explained in the row. "
        "<font color='#047857'><b>yes</b></font> = permitted, where the area has no separate "
        "write step. "
        "<font color='#B91C1C'><b>no</b></font> = no access; the endpoint returns 403.",
        "note",
    )
)

# --- 2. the money rule ------------------------------------------------------
story.append(PageBreak())
story.append(para("2. The money rule -- who sees which side of a deal", "h1"))
story.append(
    para(
        "This is the single most important thing to understand about the roles, and it is not a "
        "convenience setting. Sales negotiates the price the client pays. Resourcing negotiates "
        "what the consultant is paid. Neither is shown the other side, because somebody who knows "
        "both numbers is negotiating against their own colleague. Management sees both, because "
        "somebody has to."
    )
)

money = [
    (P_.FIELD_RESOURCE_COST, "Consultant cost rate",
     "What Glimmora pays the consultant. Resourcing negotiates it."),
    (P_.FIELD_BILLING_RATE, "Client bill rate",
     "What the client pays Glimmora. Sales negotiates it."),
    (P_.FIELD_MARGIN, "Margin and gross profit",
     "The difference between the two, and every figure derived from it."),
    (P_.FIELD_CONTRACT_VALUE, "Contract value",
     "Monthly revenue multiplied out over the length of the engagement."),
    (P_.FIELD_DOCUMENT_PERSONAL_VIEW, "Personal documents -- view",
     "Passports, visas, work permits: that they exist, and when they expire."),
    (P_.FIELD_DOCUMENT_PERSONAL_DOWNLOAD, "Personal documents -- download",
     "Taking a copy of the file itself. Every download is audited."),
]

rows = [header_row(["Field group", "What it is", *[SHORT[r] for r in ROLES]])]
for permission, label, meaning in money:
    rows.append(
        [
            para(f"<b>{label}</b><br/>{code(permission.value)}", "cell"),
            para(meaning, "cell"),
            *[mark("full" if permission in permissions_for(role) else "no") for role in ROLES],
        ]
    )
story.append(table(rows, [50 * mm, 107 * mm, 28 * mm, 28 * mm, 28 * mm, 28 * mm]))

story.append(Spacer(1, 8))
story.append(
    para(
        "<b>Read that table twice.</b> Sales cannot see what a consultant costs. Resourcing cannot "
        "see what the client is billed. Each sees the figures derived from its own side. "
        "Resourcing is the only non-admin role that may download a passport, because Resourcing "
        "files the visa applications; Management may confirm a document exists but cannot take a "
        "copy away.",
        "note",
    )
)

# --- 3. entity access -------------------------------------------------------
story.append(PageBreak())
story.append(para("3. What each role may do, area by area", "h1"))

# (label, read permission, write permission or None, note, per-role override)
areas: list[tuple[str, list[tuple]]] = [
    (
        "Identity and platform",
        [
            ("Users", P_.USER_READ, P_.USER_UPDATE,
             "Only Admin creates, edits or deactivates a user.", {}),
            ("Roles matrix", P_.ROLE_READ, None,
             "Read-only for everybody, Admin included: the matrix lives in code.", {}),
            ("Audit log", P_.AUDIT_VIEW, None,
             "Append-only. No edit or delete path exists in the API at all.", {}),
            ("Scoring rules", P_.SCORING_CONFIG_READ, P_.SCORING_CONFIG_EDIT,
             "Everyone sees the rules; only Admin publishes a new version.", {}),
            ("Notifications", P_.NOTIFICATION_READ, None,
             "Your own inbox. Reads are scoped to the caller, not to the role.", {}),
        ],
    ),
    (
        "Accounts",
        [
            ("Customers and partners", P_.ACCOUNT_READ, P_.ACCOUNT_UPDATE,
             "Sales owns the client relationship.", {}),
            ("Contacts", P_.CONTACT_READ, P_.CONTACT_WRITE, "", {}),
            ("Projects", P_.PROJECT_READ, P_.PROJECT_WRITE, "", {}),
            ("Activities", P_.ACTIVITY_READ, P_.ACTIVITY_WRITE,
             "Resourcing may log activity without owning the account.", {}),
        ],
    ),
    (
        "Demand",
        [
            ("Requirements", P_.REQUIREMENT_READ, P_.REQUIREMENT_UPDATE,
             "Sales creates and closes. Resourcing may update an existing one, "
             "not raise or delete it.",
             {Role.HR_RESOURCING: "part"}),
            ("JD parsing", P_.JD_PARSE, None,
             "Parsed output is a draft until a human accepts it.", {}),
        ],
    ),
    (
        "Talent",
        [
            ("Consultants", P_.RESOURCE_READ, P_.RESOURCE_UPDATE,
             "Resourcing owns the talent cloud. Sales reads it, to match against demand.", {}),
            ("CV parsing", P_.CV_PARSE, None, "Resourcing only.", {}),
            ("Documents", P_.DOCUMENT_READ, P_.DOCUMENT_WRITE,
             "Sales cannot read the document list at all -- not even the filenames.", {}),
        ],
    ),
    (
        "Intelligence",
        [
            ("Forward matching", P_.MATCHING_READ, P_.MATCHING_RUN,
             "Management observes the results; it does not run the engine.", {}),
            ("Reverse matching", P_.REVERSE_MATCHING_READ, P_.REVERSE_MATCHING_RUN,
             "Resourcing runs redeployment. Sales reads the suggestions.", {}),
            ("Opportunity scoring", P_.SCORING_READ, P_.SCORING_RUN,
             "Sales recomputes a score; Resourcing reads it.", {}),
            ("Commercial calculator", P_.COMMERCIAL_RUN, None,
             "Sales only. Resourcing has no view of client pricing.", {}),
        ],
    ),
    (
        "Pipeline",
        [
            ("Opportunities", P_.OPPORTUNITY_READ, P_.OPPORTUNITY_WRITE,
             "Stage moves and the pursue / hold / decline decision.", {}),
            ("Submissions", P_.SUBMISSION_READ, P_.SUBMISSION_WRITE,
             "Both Sales and Resourcing may put a candidate forward.", {}),
            ("Interviews", P_.INTERVIEW_READ, P_.INTERVIEW_WRITE, "", {}),
            ("Communications", P_.COMMUNICATION_READ, P_.COMMUNICATION_WRITE,
             "Send and log. Delivery is recorded whether or not email is configured.", {}),
        ],
    ),
    (
        "Delivery",
        [
            ("Deployments", P_.DEPLOYMENT_READ, P_.DEPLOYMENT_WRITE,
             "Placing a consultant on a project.", {}),
            ("Billing records", P_.BILLING_READ, P_.BILLING_WRITE,
             "Sales confirms what was actually invoiced. Resourcing has no access.", {}),
        ],
    ),
    (
        "Dashboards and data",
        [
            ("Management dashboard", P_.DASHBOARD_MANAGEMENT, None, "", {}),
            ("Sales dashboard", P_.DASHBOARD_SALES, None, "", {}),
            ("Resourcing dashboard", P_.DASHBOARD_HR, None, "", {}),
            ("Admin dashboard", P_.DASHBOARD_ADMIN, None, "", {}),
            ("Excel import", P_.IMPORT_RUN, None,
             "Two-step: validate, then commit. Management cannot import.", {}),
            ("Excel export", P_.EXPORT_RUN, None,
             "Exports obey the field rules -- a hidden column is absent from the workbook.", {}),
        ],
    ),
]

for area, entries in areas:
    block: list = [para(area, "h2")]
    rows = [header_row(["Area", *[SHORT[r] for r in ROLES], "Note"])]
    for label, read_perm, write_perm, note, override in entries:
        cells = []
        for role in ROLES:
            if role in override:
                cells.append(mark(override[role]))
                continue
            granted = permissions_for(role)
            if write_perm is not None and write_perm in granted:
                cells.append(mark("full"))
            elif read_perm in granted:
                cells.append(mark("read" if write_perm is not None else "yes"))
            else:
                cells.append(mark("no"))
        rows.append([para(label, "cellb"), *cells, para(note, "cell")])
    block.append(table(rows, [46 * mm, 24 * mm, 24 * mm, 24 * mm, 24 * mm, 127 * mm]))
    story.append(KeepTogether(block))

# --- 4. field-by-field ------------------------------------------------------
story.append(PageBreak())
story.append(para("4. Field by field: exactly what is removed, and from whom", "h1"))
story.append(
    para(
        "These are the precise fields the API strips per role, taken from the serializers rather "
        f"than from documentation. Where a field is removed, the response carries a "
        f"{code('restricted_fields', '7.5')} list naming what was withheld -- so the interface can "
        "say &quot;hidden by your role&quot; instead of showing a suspiciously short record that "
        "looks complete."
    )
)

field_rules = [
    ("Consultant", "GET /resources", P_.FIELD_RESOURCE_COST,
     ["expected_cost_amount", "expected_cost_currency", "expected_cost_unit"]),
    ("Consultant", "GET /resources", P_.FIELD_BILLING_RATE,
     ["target_billing_amount", "target_billing_currency", "target_billing_unit"]),
    ("Match explanation", "GET /matching/requirements/{id}", P_.FIELD_MARGIN,
     ["components -> cost", "components -> commercial"]),
    ("Redeployment suggestion", "GET /reverse-matching/resources/{id}", P_.FIELD_MARGIN,
     ["components -> cost", "components -> commercial"]),
    ("Opportunity score", "GET /scoring/requirements/{id}/explain", P_.FIELD_MARGIN,
     ["monthly_revenue", "monthly_cost", "gross_profit",
      "margin_percent", "contract_value", "total_profit"]),
    ("Opportunity", "GET /opportunities", P_.FIELD_MARGIN,
     ["expected_monthly_revenue", "expected_margin_percent", "contract_value"]),
    ("Submission", "GET /submissions", P_.FIELD_BILLING_RATE,
     ["proposed_bill_rate", "proposed_bill_currency", "proposed_bill_unit"]),
    ("Deployment", "GET /deployments", P_.FIELD_RESOURCE_COST,
     ["cost_rate", "cost_currency", "cost_unit"]),
    ("Deployment", "GET /deployments", P_.FIELD_BILLING_RATE,
     ["bill_rate", "bill_currency", "bill_unit"]),
    ("Consultant export", "GET /exports/resources.xlsx", P_.FIELD_RESOURCE_COST,
     ["Expected cost column omitted from the workbook"]),
    ("Requirement export", "GET /exports/requirements.xlsx", P_.FIELD_BILLING_RATE,
     ["Rate columns omitted from the workbook"]),
]

rows = [header_row(["Record", "Endpoint", "Removed unless permitted",
                    *[SHORT[r] for r in ROLES]])]
for record, endpoint, permission, fields in field_rules:
    rows.append(
        [
            para(f"<b>{record}</b>", "cell"),
            para(code(endpoint), "cell"),
            para("<br/>".join(code(f) for f in fields), "cell"),
            *[mark("full" if permission in permissions_for(role) else "no") for role in ROLES],
        ]
    )
story.append(table(rows, [34 * mm, 58 * mm, 65 * mm, 28 * mm, 28 * mm, 28 * mm, 28 * mm]))

story.append(Spacer(1, 8))
story.append(
    para(
        "<b>Documents are handled separately.</b> A personal document -- passport, visa, work "
        "permit, national ID -- is listed to Management but cannot be downloaded by it. Resourcing "
        "and Admin may download; Sales cannot read the document list at all. Every download writes "
        "an audit row naming who took the copy, because for a passport that is the entire point of "
        "the control.",
        "note",
    )
)

# --- 5. per-role summaries --------------------------------------------------
story.append(PageBreak())
story.append(para("5. Each role, in its own words", "h1"))

FIELD_MEANING = {
    P_.FIELD_RESOURCE_COST: "consultant cost rates",
    P_.FIELD_BILLING_RATE: "client bill rates",
    P_.FIELD_MARGIN: "margin and gross profit",
    P_.FIELD_CONTRACT_VALUE: "total contract value",
    P_.FIELD_DOCUMENT_PERSONAL_VIEW: "personal document details",
    P_.FIELD_DOCUMENT_PERSONAL_DOWNLOAD: "personal document downloads",
}

ROLE_STORY = {
    Role.ADMIN: (
        "Runs the platform. Holds every permission, including the two nobody else does: "
        "creating users, and publishing a new version of the scoring rules. A scoring weight "
        "change alters how the whole business prioritises its sales effort, so it is an "
        "administrative act with a version history -- not a preference."
    ),
    Role.MANAGEMENT: (
        "Sees everything and changes almost nothing. It is the only role besides Admin that sees "
        "both sides of the money at once, which is what makes the management dashboard meaningful. "
        "It cannot run the matching engine, edit a requirement, import data or confirm billing -- "
        "oversight that can also operate the thing it oversees is not oversight."
    ),
    Role.SALES: (
        "Owns demand: accounts, requirements, opportunities, pricing and the client relationship. "
        "Sees the bill rate, the margin and the contract value, because it must prioritise on "
        "profitability. Never sees what a consultant costs, and cannot open the document vault."
    ),
    Role.HR_RESOURCING: (
        "Owns supply: the talent cloud, CVs, documents, availability and redeployment. Sees the "
        "consultant's cost rate and may download a passport for a visa application. Never sees the "
        "client bill rate, the margin, or the billing ledger."
    ),
}

for role in ROLES:
    granted = permissions_for(role)
    visible = [FIELD_MEANING[p] for p, _, _ in money if p in granted]
    hidden = [FIELD_MEANING[p] for p, _, _ in money if p not in granted]

    block = [
        para(f"{ROLE_LABELS[role]} -- {len(granted)} permissions", "h2"),
        para(ROLE_STORY[role]),
    ]
    rows = [
        header_row(["", "Detail"]),
        [para("Sees", "cellb"),
         para(", ".join(visible).capitalize() if visible else "None of the restricted fields.",
           "cell")],
        [para("Cannot see", "cellb"),
         para(", ".join(hidden).capitalize() if hidden else "Nothing is withheld from this role.",
           "cell")],
        [para("Writes", "cellb"),
         para(", ".join(
             sorted({p.value.split(":")[0].replace("_", " ")
                     for p in granted
                     if p.value.split(":")[-1] in {"write", "create", "update", "delete", "edit"}})
         ) or "Nothing -- this role is read-only.", "cell")],
        [para("Runs", "cellb"),
         para(", ".join(
             sorted({p.value.split(":")[0].replace("_", " ")
                     for p in granted
                     if p.value.split(":")[-1] in {"run", "parse"}})
         ) or "No engines.", "cell")],
    ]
    block.append(table(rows, [30 * mm, 239 * mm]))
    story.append(KeepTogether(block))
    story.append(Spacer(1, 4))

# --- 6. complete permission list -------------------------------------------
story.append(PageBreak())
story.append(para("6. Every permission, in full", "h1"))
story.append(
    para(
        f"All {len(list(Permission))} permissions in the system. Field permissions are marked, and "
        "are enforced in the serializer rather than at the endpoint -- hiding a rate in the "
        "interface is not a security control."
    )
)

rows = [header_row(["Permission", "Type", *[SHORT[r] for r in ROLES]])]
for permission in sorted(Permission, key=lambda p: p.value):
    rows.append(
        [
            para(code(permission.value, "7"), "cell"),
            para("field" if permission in FIELD_PERMISSIONS else "action", "cell"),
            *[mark("full" if permission in permissions_for(role) else "no") for role in ROLES],
        ]
    )
story.append(table(rows, [100 * mm, 25 * mm, 36 * mm, 36 * mm, 36 * mm, 36 * mm]))

# --- 7. the rules behind it -------------------------------------------------
story.append(PageBreak())
story.append(para("7. Why the boundaries sit where they do", "h1"))

principles = [
    ("A restricted field is absent, not null",
     "When a role may not see a value, the key is removed from the JSON entirely. Returning null "
     "would let a caller distinguish &quot;there is no rate&quot; from &quot;you may not "
     "see the rate&quot;, which leaks the existence of the rate."),
    ("What was withheld is named",
     f"Every response that strips a field also returns a {code('restricted_fields', '8')} list. "
     "The interface then states which components were hidden, rather than quietly showing a "
     "shorter breakdown that looks complete."),
    ("Management observes; it does not operate",
     "Management reads across the entire business, including both sides of the money, and holds "
     "almost no write permission at all."),
    ("Sales and Resourcing are deliberately asymmetric",
     "Sales owns demand, pricing and the client. Resourcing owns supply, cost and visas. Each has "
     "write access on its own side and read-only on the other, so the two negotiate against the "
     "market rather than against each other."),
    ("Only Admin changes the rules",
     "Users, roles and scoring configuration are Admin-only, and every scoring config is versioned "
     "so an old score can still be explained by the rules that produced it."),
    ("Everything sensitive is audited",
     "Sign-ins, permission changes, document downloads, score computations, submissions, "
     "deployments and billing confirmations all write an append-only audit row. The audit log has "
     "no update or delete path in the API."),
]

for heading, text in principles:
    story.append(KeepTogether([para(heading, "h2"), para(text)]))

story.append(Spacer(1, 12))
story.append(
    para(
        "This document is generated from the codebase. If a permission changes, regenerate it "
        "rather than editing it -- a hand-corrected copy of an access-control matrix is a copy "
        "that will eventually be wrong.",
        "small",
    )
)


# ---------------------------------------------------------------------- build
def decorate(canvas, doc) -> None:
    canvas.saveState()
    width, _ = landscape(A4)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(14 * mm, 10 * mm, "Glimmora IT Workforce -- Roles and Field Visibility")
    canvas.drawRightString(width - 14 * mm, 10 * mm, f"Page {doc.page}")
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.4)
    canvas.line(14 * mm, 13 * mm, width - 14 * mm, 13 * mm)
    canvas.restoreState()


out = ROOT / "docs" / "Glimmora-Roles-and-Field-Visibility.pdf"
out.parent.mkdir(parents=True, exist_ok=True)

doc = BaseDocTemplate(
    str(out),
    pagesize=landscape(A4),
    leftMargin=14 * mm,
    rightMargin=14 * mm,
    topMargin=14 * mm,
    bottomMargin=17 * mm,
    title="Glimmora IT Workforce - Roles and Field Visibility",
    author="Glimmora IT Workforce Intelligence Engine",
)
frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body", showBoundary=0)
doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=decorate)])
doc.build(story)

print(f"written: {out}")
print(f"size:    {out.stat().st_size:,} bytes")
