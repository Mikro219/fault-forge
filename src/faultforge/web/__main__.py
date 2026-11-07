"""Starts the app: python -m faultforge.web. Binds to loopback unless told otherwise."""

import logging
import os
import sys

import uvicorn

from faultforge.web.app import create_app
from faultforge.web.runs import RunManager
from faultforge.web.settings import WebSettings, take_api_key
from faultforge.web.targets import load_targets

GRACEFUL_CONNECTIONS_S = 5


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        api_key = take_api_key()
        settings = WebSettings.from_env(os.environ)
        port = int(os.environ.get("FF_WEB_PORT", "8080"))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    manager = RunManager(settings, load_targets(), api_key=api_key)
    # proxy_headers is off because client_key decides what to trust from X-Forwarded-For. uvicorn's
    # version would replace the peer address that decision depends on.
    uvicorn.run(
        create_app(manager),
        host=os.environ.get("FF_WEB_HOST", "127.0.0.1"),
        port=port,
        proxy_headers=False,
        server_header=False,
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_CONNECTIONS_S,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
