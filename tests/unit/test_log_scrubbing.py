"""Nothing identifying reaches a log.

The allowlist in src/common/logging.py is the enforcement point: a field not on it is dropped
before the line is written, so a mistaken call site leaks nothing. These assert the list holds
what it should and nothing it should not, and that the modules calling it stay inside it.
"""

import pathlib
import re

import pytest

from src.common.logging import ALLOWED_FIELDS, log

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Fields that would identify a person or state a financial fact. None may ever be allowed,
# whatever a future call site would find convenient.
FORBIDDEN = (
    "email",
    "phone",
    "date_of_birth",
    "first_name",
    "last_name",
    "postal_address",
    "payer_address",
    "amount",
    "credit_amount",
    "value",
    "factors",
    "supplied",
    "summary_text",
    "transcript",
    "caller_stated_problem",
    "caller_self_description",
)


@pytest.mark.parametrize("field", FORBIDDEN)
def test_no_identifying_field_is_on_the_allowlist(field):
    assert field not in ALLOWED_FIELDS


def test_a_forbidden_field_is_dropped_rather_than_written(capsys):
    """The enforcement is in the writer, not in the discipline of the caller.

    Captured from the handler's own stream rather than caplog: the logger does not propagate
    to the root, which is deliberate — a Lambda writing twice writes to CloudWatch twice."""
    log("INFO", "probe", email="klaus.mueller@alpina-tech.ch", conversation_id="conv_1")
    written = capsys.readouterr().out + capsys.readouterr().err
    assert "klaus.mueller" not in written
    assert "conv_1" in written


def test_error_detail_carries_exception_text_not_caller_input():
    """It is the one field that carries free text, so it is the one worth stating the rule
    about: adapter exceptions, never anything a caller said."""
    assert "error_detail" in ALLOWED_FIELDS
    source = (ROOT / "src" / "common" / "logging.py").read_text()
    assert "Never caller-supplied input" in source


def test_every_field_any_handler_logs_is_on_the_allowlist():
    """A field that is dropped is a log line that says less than its author thought. Silent,
    and only noticed when someone needs it."""
    used, unknown = set(), set()
    for path in (ROOT / "src").rglob("*.py"):
        for call in re.findall(r"log\.(?:info|error)\(\s*\"[^\"]*\"([^)]*)\)", path.read_text()):
            used |= set(re.findall(r"(\w+)\s*=", call))
    for field in used:
        if field not in ALLOWED_FIELDS and not field.startswith("_"):
            unknown.add(field)
    assert not unknown, f"logged but dropped: {sorted(unknown)}"
