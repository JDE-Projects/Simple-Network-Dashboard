"""FastAPI's automatic OpenTelemetry export is switched off in code.

FastAPI can add exporters from ``OTEL_*`` environment variables when
``FASTAPI_OTEL_AUTO_CONFIGURE=true`` is set. The app passes an explicit
``auto_configure: False``, which overrides that variable, so nothing in the
environment can make the dashboard send telemetry.
"""

from __future__ import annotations

import main


def test_app_turns_off_automatic_telemetry_export():
    assert main.app._telemetry["auto_configure"] is False
