# Browser tests

These tests drive the dashboard in a real Chromium browser with
[Playwright](https://playwright.dev/python/) to check screen behavior that the
plain Python tests cannot, such as the failure-and-recovery flows in the browser
interface.

## One-time setup

They need two things beyond the normal test dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install pytest-playwright
.\.venv\Scripts\python.exe -m playwright install chromium
```

The first installs the test plugin; the second downloads the Chromium browser
into a system cache outside this repository (about 115 MB). Neither is committed.

The `pytest-playwright` version is installed locally and is not pinned in
`requirements-dev.txt`, whose development-tool versions are managed centrally in
Build-Tools. Folding this pin into that shared set is left to the Build-Tools
work (Task 11).

## Running

The browser tests are excluded from the normal test run and must be asked for by
name, because they hold an event loop open for the length of the run and would
break the many tests that call `asyncio.run()` if mixed into the same process:

```powershell
# Everything except the browser tests (the normal suite):
.\.venv\Scripts\python.exe -m pytest

# The browser tests only:
.\.venv\Scripts\python.exe -m pytest tests/browser -m browser
```

When Chromium has not been downloaded, the browser tests skip cleanly rather
than fail, so the normal suite still runs everywhere.

## How the harness works

`conftest.py` provides the fixtures:

- `app_server` starts the real app in its own process over HTTPS, on a fresh
  temporary storage folder (`SND_DATA_DIR` / `SND_LOG_DIR`) with a throwaway
  self-signed certificate. HTTPS is required because the session and CSRF
  cookies use the `__Host-` prefix and only work on a secure origin.
- `logged_in_page` returns a browser page already signed in, with the
  self-signed certificate warning ignored.

`_run_app.py` is the small launcher that serves the app with the certificate
arguments the app's own start-up does not take.
