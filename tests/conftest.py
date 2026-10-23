"""Load the plugin package (whose directory name contains a hyphen) so tests can
import it under a valid module name."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_hindsight_plus",
        str(ROOT / "__init__.py"),
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_hindsight_plus"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def core(plugin):
    return plugin.core
