"""Run the Simple Network Dashboard server."""

import sys

import uvicorn

from simple_network_dashboard.main import _parse_port


uvicorn.run(
    "simple_network_dashboard.main:app",
    host="127.0.0.1",
    port=_parse_port(sys.argv[1:]),
    reload=False,
)
