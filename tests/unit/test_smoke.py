"""Keeps the test suite non-empty from the first commit, so CI is meaningful before any
business logic exists. Delete once real domain tests land in T032."""

import src


def test_package_imports() -> None:
    assert src is not None
