"""Fails the build if environment identity or credential material reaches a tracked file.

The repository may be shared. Account ids, state bucket names and real phone numbers are
not credentials, but they identify a live environment and belong in untracked files
(terraform.tfvars, backend.hcl) or in CI variables. Credentials must never appear at all.

Run: uv run python scripts/check_no_secrets.py
"""

import re
import subprocess
from pathlib import Path

# Each pattern is (name, regex). Kept deliberately narrow: a noisy guard gets disabled.
PATTERNS = [
    ("AWS account id", re.compile(r"\b\d{12}\b")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # Legacy private-app tokens (pat-) and the Service Keys that replaced them in 2026.
    ("HubSpot token", re.compile(r"\bpat-(na|eu)\d-[0-9a-f-]{20,}")),
    ("HubSpot service key", re.compile(r"\b(sk|svc)-[a-z]{2}\d-[0-9a-f-]{20,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}")),
    ("E.164 phone number", re.compile(r"\+\d{7,15}\b")),
]

# Patterns that only make sense outside test fixtures. A fixture has to contain
# plausible-looking data to be worth anything, and all project data is synthetic by rule
# (FR-032). Credential patterns still apply everywhere — a real token in a fixture is a real
# token.
NON_FIXTURE_ONLY = frozenset({"AWS account id", "E.164 phone number"})

# Paths where a match is expected and harmless.
EXEMPT = ("scripts/check_no_secrets.py", "uv.lock", ".python-version")

# Values that look like the real thing but identify nothing: the all-zeros account used as
# a placeholder in examples and in CI, where terraform needs a syntactically valid value it
# will never authenticate against.
ALLOWED_VALUES = frozenset({"000000000000"})


def main() -> int:
    """
    Greps every tracked file for environment identity and credential material.

    Returns: 0 when clean, 1 when anything matched. Untracked files are not checked —
             terraform.tfvars and backend.hcl are meant to hold these values.
    """
    files = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()

    findings = []
    for path in files:
        if path.endswith(EXEMPT) or path in EXEMPT:
            continue
        try:
            content = Path(path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        in_tests = path.startswith("tests/")
        for name, pattern in PATTERNS:
            if in_tests and name in NON_FIXTURE_ONLY:
                continue
            for match in pattern.finditer(content):
                if match.group() in ALLOWED_VALUES:
                    continue
                line = content[: match.start()].count("\n") + 1
                findings.append(f"{path}:{line}: {name}: {match.group()}")

    if findings:
        print("Environment identity or credential material found in tracked files:\n")
        print("\n".join(findings))
        print("\nMove these to terraform.tfvars, backend.hcl, or a CI variable.")
        return 1

    print(f"clean: {len(files)} tracked files checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
