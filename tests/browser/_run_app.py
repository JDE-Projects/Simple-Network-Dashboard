"""Launch the dashboard over HTTPS for the browser-test harness.

Run as its own process so the app imports with the test's temporary
``SND_DATA_DIR``/``SND_LOG_DIR`` in place. The app's own ``__main__`` block
serves plain HTTP with no certificate arguments, so this launcher calls uvicorn
directly with a throwaway certificate instead.

Everything needed is passed through the environment by the harness fixture:

- ``SND_DATA_DIR`` / ``SND_LOG_DIR``: temporary private storage for this run.
- ``SND_PUBLIC_ORIGIN``: the exact ``https://host:port`` the browser will send,
  so the WebSocket origin check accepts the test page.
- ``SND_TEST_HOST`` / ``SND_TEST_PORT``: where uvicorn listens.
- ``SND_TEST_CERT`` / ``SND_TEST_KEY``: the self-signed certificate and key.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ["SND_TEST_HOST"]
    port = int(os.environ["SND_TEST_PORT"])
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        ssl_certfile=os.environ["SND_TEST_CERT"],
        ssl_keyfile=os.environ["SND_TEST_KEY"],
        log_level="warning",
        reload=False,
    )


if __name__ == "__main__":
    main()
