# Its presence at the repo root makes pytest add the repo root to sys.path, so
# `from main import ...` in tests/ resolves without relying on the working
# directory pytest happened to be launched from.

import importlib.util

# The browser (Playwright) tests import the `playwright` package when their
# modules load, which happens during collection, before pytest.ini's
# `-m "not browser"` default can deselect them. On any machine without the
# browser-test setup installed (CI, or a fresh clone), that import would fail
# and break the whole run. Skip collecting that folder entirely when the
# package is absent; when it is present the tests are collected and then
# deselected by default, or run explicitly with `-m browser`.
if importlib.util.find_spec("playwright") is None:
    collect_ignore = ["tests/browser"]
