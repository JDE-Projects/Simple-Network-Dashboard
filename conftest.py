# Its presence at the repo root makes pytest add the package to sys.path
# without relying on the working directory pytest happened to be launched from.

import copy
import importlib.util
import os
import tempfile

import pytest

# The application reads its storage locations once, when it is first imported.
# This file loads before any test module, so setting them here sends every test
# to throwaway folders, whatever the machine or environment holds. No test can
# read or change an installed dashboard's data.
_test_storage = tempfile.TemporaryDirectory(prefix="snd-tests-", ignore_cleanup_errors=True)
for _variable, _folder in (("SND_DATA_DIR", "data"), ("SND_LOG_DIR", "logs")):
    os.environ[_variable] = os.path.join(_test_storage.name, _folder)
    os.makedirs(os.environ[_variable])

from simple_network_dashboard import runtime_state  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_runtime_state():
    """Undo each test's changes to the shared runtime state, including recovery mode."""
    saved = {
        name: copy.deepcopy(value)
        for name, value in vars(runtime_state).items()
        if not name.startswith("__")
    }
    yield
    for name, value in saved.items():
        setattr(runtime_state, name, value)


# The browser (Playwright) tests import the `playwright` package when their
# modules load, which happens during collection, before pytest.ini's
# `-m "not browser"` default can deselect them. On any machine without the
# browser-test setup installed (CI, or a fresh clone), that import would fail
# and break the whole run. Skip collecting that folder entirely when the
# package is absent; when it is present the tests are collected and then
# deselected by default, or run explicitly with `-m browser`.
if importlib.util.find_spec("playwright") is None:
    collect_ignore = ["tests/browser"]
