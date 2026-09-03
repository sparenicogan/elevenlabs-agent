"""Synthetic customers and their financial history.

Companies and contacts mirror the real records in the HubSpot development account, linked by
id. Everything financial, and everything used to verify identity, exists only here and only
in DynamoDB — the CRM never sees a date of birth, a phone number, or a franc (FR-027,
FR-028). That split is the point: two systems, two sets of facts, and the boundary between
them is enforced rather than described.

Every value is invented. No real company, person, address, phone number or payment appears
here (FR-032).

Dates are day offsets from the seeding date rather than fixed dates, so the overdue invoice
is always overdue and the disputed payment always sits inside the matching tolerance. The
fixtures do not go stale between rehearsals.
"""

import hashlib
from datetime import date, timedelta

TODAY = date.today()

# Deterministic 7-digit payment references. Random-looking but stable across seed runs, so a
# reference read aloud in one rehearsal is still valid in the next. Sparse over ten million
# values, which is what makes typo detection safe (FR-010h).
_REFERENCE_SEED = 1467900


def _day(offset: int) -> date:
    """A date `offset` days before seeding."""
    return TODAY - timedelta(days=offset)


class _Sequence:
    """Assigns invoice numbers per calendar year, in date order, as a real ledger would."""

    def __init__(self) -> None:
        self._per_year: dict[int, int] = {}
        self._reference = _REFERENCE_SEED

    def invoice_number(self, when: date) -> str:
        count = self._per_year.get(when.year, 0) + 1
        self._per_year[when.year] = count
        return f"INV-{when.year}-{count:04d}"

    def payment_reference(self) -> str:
        # Steps by a prime so consecutive references are not adjacent, which would defeat
        # the sparseness the typo rule depends on.
        self._reference = (self._reference + 104729) % 10_000_000
        return f"{self._reference:07d}"


# --- Companies -------------------------------------------------------------------------
# language is what the agent greets in. Alpina Tech is English so the demo can be followed
# by an English-speaking audience; the others carry the language of their HubSpot record.

COMPANIES = [
    {
        "key": "alpina",
        "customer_id": "CUST-00417",
        "company_name": "Alpina Tech",
        "hubspot_company_id": "445909044455",
        "hubspot_contact_id": "859557757171",
        "first_name": "Klaus",
        "last_name": "Mueller",
        "email": "klaus.mueller@alpina-tech.ch",
        "phone": "+41 44 501 22 18",
        "date_of_birth": "1974-03-12",
        "account_opening_year": 2019,
        "language": "en",
        "city": "Zürich",
        "postcode": "8005",
        "street": "Industriestrasse 14",
        "role": "golden path: disputed overdue invoice with a payment that never allocated",
    },
    {
        "key": "ticino",
        "customer_id": "CUST-00982",
        "company_name": "Ticino Industries",
        "hubspot_company_id": "446019693775",
        "hubspot_contact_id": "859585377479",
        "first_name": "Marco",
        "last_name": "Rossi",
        "email": "marco.rossi@ticino-ind.ch",
        "phone": "+41 91 604 77 31",
        "date_of_birth": "1981-11-02",
        "account_opening_year": 2021,
        "language": "it",
        "city": "Lugano",
        "postcode": "6900",
        "street": "Via Motta 7",
        "role": "clean history: a small goodwill credit is granted on the call",
    },
    {
        "key": "apex",
        "customer_id": "CUST-01144",
        "company_name": "Apex Capital",
        "hubspot_company_id": "445900025039",
        "hubspot_contact_id": "859554863314",
        "first_name": "Charles",
        "last_name": "Lavigne",
        "email": "charles.lavigne@apex-capital.ch",
        "phone": "+41 22 918 40 65",
        "date_of_birth": "1969-07-25",
        "account_opening_year": 2017,
        "language": "fr",
        "city": "Genève",
        "postcode": "1204",
        "street": "Rue du Rhône 22",
        "role": "near the rolling credit ceiling: a further small credit must be refused",
    },
    {
        "key": "precision",
        "customer_id": "CUST-01390",
        "company_name": "Precision Systems",
        "hubspot_company_id": "446087611588",
        # Martin Keller is the contact recorded against the account. Julia Fischer and
        # Daniel Zimmermann also work there and exist in the CRM, but are not authorised to
        # act on it — which is the scenario this company exists to exercise (FR-007a).
        "hubspot_contact_id": "859585276132",
        "first_name": "Martin",
        "last_name": "Keller",
        "email": "martin.keller@precision-systems.ch",
        # No phone on file, so an inbound call is unrecognised and the greeting falls back
        # to the configured default language (FR-033a).
        "phone": None,
        "date_of_birth": "1988-01-19",
        "account_opening_year": 2023,
        "language": "de",
        "city": "Bern",
        "postcode": "3011",
        "street": "Bahnhofplatz 3",
        "role": "an employee who is not the account contact, calling from an unknown number",
    },
    {
        "key": "heritage",
        "customer_id": "CUST-01502",
        "company_name": "Heritage Manufacturing",
        "hubspot_company_id": "445941470419",
        "hubspot_contact_id": "859555877061",
        "first_name": "Andrea",
        "last_name": "Colombo",
        "email": "andrea.colombo@heritage-mfg.ch",
        "phone": "+41 91 233 08 54",
        "date_of_birth": "1971-05-30",
        "account_opening_year": 2016,
        "language": "it",
        "city": "Bellinzona",
        "postcode": "6500",
        "street": "Viale Stazione 41",
        "role": "over CHF 10,000 unpaid across the last three invoices",
    },
    {
        "key": "nexus",
        "customer_id": "CUST-01633",
        "company_name": "Nexus Consulting",
        "hubspot_company_id": "446043304132",
        "hubspot_contact_id": "859560119498",
        "first_name": "Raphael",
        "last_name": "Mueller",
        "email": "raphael.mueller@nexus-consulting.ch",
        "phone": "+41 31 776 15 09",
        "date_of_birth": "1985-09-14",
        "account_opening_year": 2020,
        "language": "de",
        "city": "Bern",
        "postcode": "3006",
        "street": "Thunstrasse 88",
        "role": "overpaid: the last payment exceeds the invoice it settled",
    },
    {
        "key": "innovatech",
        "customer_id": "CUST-01718",
        "company_name": "Innovatech",
        "hubspot_company_id": "446069714152",
        "hubspot_contact_id": "859482418401",
        "first_name": "Stephane",
        "last_name": "Richard",
        "email": "stephane.richard@innovatech.ch",
        "phone": "+41 21 340 62 77",
        "date_of_birth": "1979-12-08",
        "account_opening_year": 2018,
        "language": "fr",
        "city": "Lausanne",
        "postcode": "1003",
        "street": "Avenue de la Gare 19",
        "role": "long history back to 2024, all settled",
    },
    {
        "key": "synergy",
        "customer_id": "CUST-01845",
        "company_name": "Synergy Solutions",
        "hubspot_company_id": "445925214417",
        "hubspot_contact_id": "859519450331",
        "first_name": "Sandra",
        "last_name": "Hoffmann",
        "email": "sandra.hoffmann@synergy-sol.ch",
        "phone": "+41 61 285 93 40",
        "date_of_birth": "1983-02-21",
        "account_opening_year": 2022,
        "language": "de",
        "city": "Basel",
        "postcode": "4051",
        "street": "Steinenvorstadt 6",
        "role": "one recent invoice unpaid but not yet overdue",
    },
    {
        "key": "lumina",
        "customer_id": "CUST-01960",
        "company_name": "Lumina Analytics",
        "hubspot_company_id": "445925214416",
        "hubspot_contact_id": "859557637333",
        "first_name": "Marie",
        "last_name": "Rousseau",
        "email": "marie.rousseau@lumina-analytics.ch",
        "phone": "+41 22 447 31 86",
        "date_of_birth": "1990-06-17",
        "account_opening_year": 2024,
        "language": "fr",
        "city": "Genève",
        "postcode": "1201",
        "street": "Rue de Lausanne 55",
        "role": "newest customer, short history, everything paid on time",
    },
    {
        "key": "frontier",
        "customer_id": "CUST-02071",
        "company_name": "Digital Frontier",
        "hubspot_company_id": "446082282723",
        "hubspot_contact_id": "859557757170",
        "first_name": "Francois",
        "last_name": "Hubert",
        "email": "francois.hubert@digital-frontier.ch",
        "phone": "+41 26 512 70 24",
        "date_of_birth": "1976-10-03",
        "account_opening_year": 2019,
        "language": "fr",
        "city": "Fribourg",
        "postcode": "1700",
        "street": "Route des Arsenaux 12",
        "role": "two overdue invoices, chased before",
    },
]


# --- Financial history ------------------------------------------------------------------
# (days_ago, amount, settled_after_days | None). None means never paid.
# Every company has at least one invoice inside the last 90 days.

_HISTORY: dict[str, list[tuple[int, str, int | None]]] = {
    # Golden path. The CHF 4,200 invoice is overdue and its payment is added separately
    # below, unallocated, so the caller is right and the system does not yet know it.
    "alpina": [
        (760, "3150.00", 28),
        (610, "2480.00", 24),
        (455, "5920.00", 31),
        (300, "1870.00", 19),
        (180, "4310.00", 26),
        (75, "4200.00", None),
        (28, "980.00", 21),
    ],
    "ticino": [
        (420, "2260.00", 22),
        (255, "1740.00", 18),
        (120, "3080.00", 25),
        (40, "1420.00", 16),
    ],
    "apex": [
        (690, "8400.00", 34),
        (520, "6150.00", 29),
        (330, "7720.00", 27),
        (145, "5480.00", 23),
        (52, "6900.00", 20),
    ],
    "precision": [
        (210, "1980.00", 26),
        (95, "2340.00", 22),
        (33, "1650.00", 18),
    ],
    # Over CHF 10,000 unpaid across the last three invoices: 4,850 + 3,600 + 2,940 = 11,390.
    "heritage": [
        (830, "5400.00", 30),
        (640, "4120.00", 33),
        (470, "6880.00", 41),
        (250, "3970.00", 28),
        (118, "4850.00", None),
        (74, "3600.00", None),
        (36, "2940.00", None),
    ],
    # The most recent payment exceeds the invoice it settled — 3,000 against 2,780.
    "nexus": [
        (580, "3420.00", 25),
        (390, "2910.00", 27),
        (205, "4160.00", 24),
        (61, "2780.00", 19),
    ],
    "innovatech": [
        (880, "2650.00", 29),
        (720, "3310.00", 26),
        (545, "4470.00", 31),
        (365, "2980.00", 23),
        (190, "3760.00", 25),
        (48, "2210.00", 20),
    ],
    "synergy": [
        (310, "1890.00", 24),
        (165, "2430.00", 21),
        (22, "3120.00", None),  # unpaid but not yet overdue
    ],
    "lumina": [
        (140, "1560.00", 19),
        (68, "2080.00", 22),
        (25, "1340.00", 15),
    ],
    "frontier": [
        (400, "3890.00", 30),
        (240, "2670.00", 28),
        (112, "4520.00", None),  # overdue
        (58, "3180.00", None),  # overdue
    ],
}

# Terms are net 30 throughout, so an unpaid invoice past its due date is overdue.
PAYMENT_TERMS_DAYS = 30


def _status(days_ago: int, settled_after: int | None) -> str:
    """OPEN, OVERDUE or PAID, derived from the dates rather than asserted."""
    if settled_after is not None:
        return "PAID"
    return "OVERDUE" if days_ago > PAYMENT_TERMS_DAYS else "OPEN"


def build_ledger() -> list[dict]:
    """
    Builds every ledger entry for every company.

    Returns: invoices, the payments that settled them, and the goodwill credits that put one
             customer near the rolling ceiling. Invoice numbers are assigned per year in date
             order, so a 2024 invoice reads INV-2024-0001 exactly as it would in a real
             ledger.
    """
    sequence = _Sequence()
    entries: list[dict] = []

    dated = sorted(
        (
            (_day(days_ago), key, amount, settled_after, days_ago)
            for key, rows in _HISTORY.items()
            for days_ago, amount, settled_after in rows
        ),
        key=lambda row: row[0],
    )

    by_key = {c["key"]: c for c in COMPANIES}
    counters: dict[str, int] = {}

    for issued, key, amount, settled_after, days_ago in dated:
        company = by_key[key]
        counters[key] = counters.get(key, 0) + 1
        suffix = counters[key]
        entry_id = f"inv_{company['customer_id'][-5:]}_{suffix:03d}"

        entries.append(
            {
                "customer_id": company["customer_id"],
                "entry_id": entry_id,
                "type": "INVOICE",
                "invoice_number": sequence.invoice_number(issued),
                "payment_reference": sequence.payment_reference(),
                "entry_date": issued.isoformat(),
                "due_date": (issued + timedelta(days=PAYMENT_TERMS_DAYS)).isoformat(),
                "amount": amount,
                "currency": "CHF",
                "status": _status(days_ago, settled_after),
                "decision_source": "SEED",
                "approval_status": "NOT_REQUIRED",
            }
        )

        if settled_after is None:
            continue

        # Nexus overpaid its most recent invoice; everyone else paid exactly.
        paid = "3000.00" if (key == "nexus" and amount == "2780.00") else amount
        entries.append(
            {
                "customer_id": company["customer_id"],
                "entry_id": f"pay_{company['customer_id'][-5:]}_{suffix:03d}",
                "type": "PAYMENT",
                "entry_date": (issued + timedelta(days=settled_after)).isoformat(),
                "amount": f"-{paid}",
                "currency": "CHF",
                "status": "ALLOCATED",
                "reference": entries[-1]["payment_reference"],
                "allocated_to": [entry_id],
                "payer_name": company["company_name"],
                "decision_source": "SEED",
                "approval_status": "NOT_REQUIRED",
            }
        )

    entries.extend(_golden_path_payment())
    entries.extend(_goodwill_credits())
    return entries


def _golden_path_payment() -> list[dict]:
    """
    The payment at the centre of the demo.

    Returns: one unallocated payment for the CHF 4,200 invoice. It carries no reference,
             which is why it never allocated, and a payer address that no longer matches the
             customer record — the discrepancy the agent raises after the match (US8).
    """
    return [
        {
            "customer_id": "CUST-00417",
            "entry_id": "pay_00417_disputed",
            "type": "PAYMENT",
            "entry_date": _day(38).isoformat(),
            "amount": "-4200.00",
            "currency": "CHF",
            "status": "UNALLOCATED",
            "reference": None,
            "payer_name": "Alpina Tech",
            "payer_address": {
                "street": "Alte Landstrasse 88",
                "postcode": "8702",
                "city": "Zollikon",
                "country": "CH",
            },
            "decision_source": "SEED",
            "approval_status": "NOT_REQUIRED",
        }
    ]


def _goodwill_credits() -> list[dict]:
    """
    Credits that put Apex Capital near the rolling ceiling.

    Returns: five credits totalling CHF 460 inside the last twelve months. A further CHF 80
             would reach CHF 540 and must be refused, even though CHF 80 is well within the
             per-request limit (FR-014).
    """
    return [
        {
            "customer_id": "CUST-01144",
            "entry_id": f"cn_01144_{index:03d}",
            "type": "CREDIT_NOTE",
            "entry_date": _day(days_ago).isoformat(),
            "amount": f"-{amount}",
            "currency": "CHF",
            "status": "APPROVED",
            "reason": "goodwill",
            "decision_source": "SEED",
            "approval_status": "APPROVED",
        }
        for index, (amount, days_ago) in enumerate(
            [("90.00", 300), ("95.00", 210), ("100.00", 150), ("85.00", 90), ("90.00", 30)],
            start=1,
        )
    ]


# --- The other people at each company ---------------------------------------------------
# Every contact in the CRM can reach their company's account, using their own details. Only
# their email and HubSpot id are real (they exist in the CRM); the phone and date of birth
# are generated deterministically below, because the CRM holds neither and the backend is
# the only place personal data lives (FR-027).

COLLEAGUES = [
    ("alpina", "859585000686", "Thomas", "Weber", "thomas.weber@alpina-tech.ch"),
    ("alpina", "859585552601", "Anna", "Schmidt", "anna.schmidt@alpina-tech.ch"),
    ("ticino", "859584517346", "Lucia", "Ferrari", "lucia.ferrari@ticino-ind.ch"),
    ("ticino", "859585552602", "Giovanni", "Bianchi", "giovanni.bianchi@ticino-ind.ch"),
    ("apex", "859559808229", "Olivier", "Lefevre", "olivier.lefevre@apex-capital.ch"),
    ("apex", "859585472726", "Veronique", "Champagne", "veronique.champagne@apex-capital.ch"),
    ("precision", "859479298251", "Julia", "Fischer", "julia.fischer@precision-systems.ch"),
    ("precision", "859585843404", "Daniel", "Zimmermann", "daniel.zimmermann@precision-systems.ch"),
    ("heritage", "859585176817", "Francesca", "Rizzo", "francesca.rizzo@heritage-mfg.ch"),
    ("heritage", "859585472725", "Carlo", "Moretti", "carlo.moretti@heritage-mfg.ch"),
    ("nexus", "859585276131", "Felix", "Graber", "felix.graber@nexus-consulting.ch"),
    ("nexus", "859585938681", "Beatrice", "Fuchs", "beatrice.fuchs@nexus-consulting.ch"),
    ("innovatech", "859559924937", "Luc", "Martin", "luc.martin@innovatech.ch"),
    ("innovatech", "859563078868", "Claire", "Moreau", "claire.moreau@innovatech.ch"),
    ("synergy", "859584707809", "Peter", "Bauer", "peter.bauer@synergy-sol.ch"),
    ("synergy", "859585377478", "Michael", "Lang", "michael.lang@synergy-sol.ch"),
    ("lumina", "859559297238", "Pierre", "Bernard", "pierre.bernard@lumina-analytics.ch"),
    ("lumina", "859585640653", "Jean", "Dupont", "jean.dupont@lumina-analytics.ch"),
    ("frontier", "859559808228", "Isabelle", "Deschamps", "isabelle.deschamps@digital-frontier.ch"),
    ("frontier", "859586047210", "Eric", "Leclerc", "eric.leclerc@digital-frontier.ch"),
]

# Swiss area codes by city, so a colleague's number looks like it belongs where they work.
_AREA_CODES = {
    "Zürich": "44",
    "Lugano": "91",
    "Genève": "22",
    "Bern": "31",
    "Bellinzona": "91",
    "Lausanne": "21",
    "Basel": "61",
    "Fribourg": "26",
}


def _personal_facts(email: str, city: str) -> tuple[str, str]:
    """
    Invents a phone number and date of birth for one person.

    email: their address, used as the seed so the values never change between runs.
    city:  where they work, so the area code is plausible.

    Returns: (phone in international form, date of birth as ISO).

    Generated rather than written out because the CRM holds neither — personal data lives
    only in the backend (FR-027) — and thirty hand-written dates of birth would be thirty
    chances to typo one.
    """
    seed = int(hashlib.sha256(email.encode()).hexdigest()[:12], 16)
    area = _AREA_CODES.get(city, "44")
    phone = f"+41 {area} {seed % 900 + 100} {seed // 900 % 90 + 10} {seed // 81000 % 90 + 10}"
    year = 1962 + seed % 36
    month = seed // 36 % 12 + 1
    day = seed // 432 % 28 + 1
    return phone, f"{year}-{month:02d}-{day:02d}"


def _contact(
    company: dict,
    contact_id: str,
    first: str,
    last: str,
    email: str,
    phone: str | None,
    date_of_birth: str,
) -> dict:
    """One person who may call about a company's account."""
    return {
        "contact_id": contact_id,
        "account_id": company["customer_id"],
        "company_name": company["company_name"],
        "first_name": first,
        "last_name": last,
        "date_of_birth": date_of_birth,
        "postal_address": {
            "street": company["street"],
            "postcode": company["postcode"],
            "city": company["city"],
            "country": "CH",
        },
        **({"phone": phone} if phone else {}),
        "email": email,
        "account_status": "ACTIVE",
        "preferred_language": company["language"],
        "hubspot_company_id": company["hubspot_company_id"],
        "hubspot_contact_id": contact_id,
        "failed_verification_attempts": 0,
    }


def _build_contacts() -> list[dict]:
    """Every person who can verify: one primary contact per company, plus their colleagues."""
    by_key = {c["key"]: c for c in COMPANIES}

    contacts = [
        _contact(
            company=c,
            contact_id=c["hubspot_contact_id"],
            first=c["first_name"],
            last=c["last_name"],
            email=c["email"],
            phone=c["phone"],
            date_of_birth=c["date_of_birth"],
        )
        for c in COMPANIES
    ]

    for key, contact_id, first, last, email in COLLEAGUES:
        company = by_key[key]
        phone, date_of_birth = _personal_facts(email, company["city"])
        contacts.append(_contact(company, contact_id, first, last, email, phone, date_of_birth))

    return contacts


CONTACTS = _build_contacts()

LEDGER = build_ledger()
