"""Synthetic customers, invoices and payments for the demo.

Every value here is invented. No real company, person, address, phone number or payment
appears in this file or in any environment it is loaded into (FR-032).

The four customers exist to make specific behaviours demonstrable rather than to look like
a plausible customer base:

  meier    the golden path — a disputed overdue invoice with a payment that never allocated
  rossi    a clean customer, for the autonomous credit
  dubois   a customer near the rolling credit ceiling, for the abuse case
  weber    a customer with no phone on file, so the greeting falls back to German
"""

from datetime import date, timedelta

# Dates are relative to seeding so the fixtures never go stale: the overdue invoice is
# always overdue, and the payment always sits inside the matching tolerance.
TODAY = date.today()


def _iso(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


CUSTOMERS = [
    {
        "customer_id": "CUST-00417",
        "company_name": "Meier Bau AG",
        "first_name": "Andrea",
        "last_name": "Meier",
        "date_of_birth": "1974-03-12",
        "postal_address": {
            "street": "Industriestrasse 14",
            "postcode": "8005",
            "city": "Zürich",
            "country": "CH",
        },
        "phone": "+41 44 123 45 67",
        "email": "buchhaltung@meier-bau.ch",
        "account_opening_year": 2019,
        "account_status": "ACTIVE",
        "preferred_language": "de",
        "failed_verification_attempts": 0,
    },
    {
        "customer_id": "CUST-00982",
        "company_name": "Rossi Logistica Sagl",
        "first_name": "Marco",
        "last_name": "Rossi",
        "date_of_birth": "1981-11-02",
        "postal_address": {
            "street": "Via Motta 7",
            "postcode": "6900",
            "city": "Lugano",
            "country": "CH",
        },
        "phone": "+41 91 222 33 44",
        "email": "amministrazione@rossi-logistica.ch",
        "account_opening_year": 2021,
        "account_status": "ACTIVE",
        "preferred_language": "it",
        "failed_verification_attempts": 0,
    },
    {
        "customer_id": "CUST-01144",
        "company_name": "Dubois Conseil Sàrl",
        "first_name": "Camille",
        "last_name": "Dubois",
        "date_of_birth": "1969-07-25",
        "postal_address": {
            "street": "Rue du Rhône 22",
            "postcode": "1204",
            "city": "Genève",
            "country": "CH",
        },
        "phone": "+41 22 555 66 77",
        "email": "compta@dubois-conseil.ch",
        "account_opening_year": 2017,
        "account_status": "ACTIVE",
        "preferred_language": "fr",
        "failed_verification_attempts": 0,
    },
    {
        "customer_id": "CUST-01390",
        "company_name": "Weber Handels GmbH",
        "first_name": "Thomas",
        "last_name": "Weber",
        "date_of_birth": "1988-01-19",
        "postal_address": {
            "street": "Bahnhofplatz 3",
            "postcode": "3011",
            "city": "Bern",
            "country": "CH",
        },
        # No phone on file, so an inbound call from this customer is unrecognised and the
        # greeting falls back to German (FR-033a).
        "email": "info@weber-handels.ch",
        "account_opening_year": 2023,
        "account_status": "ACTIVE",
        "failed_verification_attempts": 0,
    },
]


LEDGER = [
    # --- The golden path -------------------------------------------------------------
    # Overdue, with no payment linked to it. The caller insists it was paid, and they are
    # right: the payment below is theirs and never allocated.
    {
        "customer_id": "CUST-00417",
        "entry_id": "inv_00417_0412",
        "type": "INVOICE",
        "invoice_number": "INV-2026-0412",
        "payment_reference": "1467900",
        "entry_date": _iso(75),
        "due_date": _iso(45),
        "amount": "4200.00",
        "currency": "CHF",
        "status": "OVERDUE",
        "decision_source": "SEED",
        "approval_status": "NOT_REQUIRED",
    },
    # The payment that should have settled it. No reference at all, which is why it never
    # allocated, and a payer address that no longer matches the customer record — the
    # discrepancy the agent raises after the match (US8).
    {
        "customer_id": "CUST-00417",
        "entry_id": "pay_00417_0031",
        "type": "PAYMENT",
        "entry_date": _iso(38),
        "amount": "-4200.00",
        "currency": "CHF",
        "status": "UNALLOCATED",
        "reference": None,
        "payer_name": "Meier Bau AG",
        "payer_address": {
            "street": "Alte Landstrasse 88",
            "postcode": "8702",
            "city": "Zollikon",
            "country": "CH",
        },
        "decision_source": "SEED",
        "approval_status": "NOT_REQUIRED",
    },
    # The next invoice in sequence, unpaid. It exists so that a payment referencing
    # INV-2026-0413 resolves to a real invoice and is classified OTHER_INVOICE rather than
    # as a typo of 0412 — the case the two-identifier design exists for (FR-010h).
    {
        "customer_id": "CUST-00417",
        "entry_id": "inv_00417_0413",
        "type": "INVOICE",
        "invoice_number": "INV-2026-0413",
        "payment_reference": "8830142",
        "entry_date": _iso(40),
        "due_date": _iso(10),
        "amount": "1850.00",
        "currency": "CHF",
        "status": "OPEN",
        "decision_source": "SEED",
        "approval_status": "NOT_REQUIRED",
    },
    # --- Clean customer, for the autonomous credit ------------------------------------
    {
        "customer_id": "CUST-00982",
        "entry_id": "inv_00982_0501",
        "type": "INVOICE",
        "invoice_number": "INV-2026-0501",
        "payment_reference": "2094771",
        "entry_date": _iso(20),
        "due_date": _iso(-10),
        "amount": "780.00",
        "currency": "CHF",
        "status": "OPEN",
        "decision_source": "SEED",
        "approval_status": "NOT_REQUIRED",
    },
    # --- Near the rolling ceiling, for the abuse case ---------------------------------
    {
        "customer_id": "CUST-01144",
        "entry_id": "inv_01144_0388",
        "type": "INVOICE",
        "invoice_number": "INV-2026-0388",
        "payment_reference": "5512038",
        "entry_date": _iso(60),
        "due_date": _iso(30),
        "amount": "2400.00",
        "currency": "CHF",
        "status": "OPEN",
        "decision_source": "SEED",
        "approval_status": "NOT_REQUIRED",
    },
]

# CHF 460 of goodwill inside the rolling twelve months. A further CHF 80 request would reach
# CHF 540 and must be refused, even though CHF 80 is well within the per-request ceiling
# (FR-014).
LEDGER += [
    {
        "customer_id": "CUST-01144",
        "entry_id": f"cn_01144_{index:04d}",
        "type": "CREDIT_NOTE",
        "entry_date": _iso(days_ago),
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
