"""
Optional artifact inputs for integration checks.
"""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--uki-file", help="Built UKI to inspect with the PE parser")
