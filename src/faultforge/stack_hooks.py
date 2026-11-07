"""The stack hooks helper. Runs in its own container, inside a stack's sealed network namespace.

An app like a Next.js service has no /__ff endpoints, so this answers health, reset and state from
trusted SQL in the stack's definition. The app is untrusted and shares the loopback, so what this
reports can still be affected by what the app does to its own database. Standard library plus
pg8000 only: this file is copied alone into the image, and imports nothing from FaultForge.

State has no in_flight count, because a separate process can't see the app's requests.
"""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 4096


def database_state(run: Callable[[str], Sequence], queries: dict[str, str]) -> dict[str, int]:
    return {name: int(run(sql)[0][0]) for name, sql in queries.items()}


def state_body(values: dict[str, int], build_id: str, instance_id: str, started_at: str) -> dict[str, object]:
    return {
        **values,
        "in_flight": 0,
        "stale_source": False,
        "build": {"build_id": build_id, "instance_id": instance_id, "instance_started_at": started_at, "stale_source": False},
    }


def make_handler(run: Callable[[str], Sequence], spec: dict, app_health_url: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def _send(self, status: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _state(self) -> dict[str, object]:
            return state_body(database_state(run, spec["state_sql"]), spec["build_id"], spec["instance_id"], spec["started_at"])

        def do_GET(self) -> None:
            try:
                if self.path == "/__ff/health":
                    # Any answer below 500 means the app is up, and an error status isn't failure here.
                    try:
                        with urllib.request.urlopen(app_health_url, timeout=2) as response:
                            status = response.status
                    except urllib.error.HTTPError as error:
                        status = error.code
                    self._send(200 if status < 500 else 503, {"status": status})
                elif self.path == "/__ff/state":
                    self._send(200, self._state())
                else:
                    self._send(404, {})
            except Exception as error:
                self._send(503, {"error": type(error).__name__})

        def do_POST(self) -> None:
            try:
                if self.path != "/__ff/reset":
                    self._send(404, {})
                    return
                for statement in spec["reset_sql"]:
                    run(statement)
                self._send(200, self._state())
            except Exception as error:
                self._send(503, {"error": type(error).__name__})

    return Handler


def main() -> None:
    import pg8000.native  # Only in the helper's image, so importing this module elsewhere works.

    spec = json.loads(os.environ["FF_STACK_HOOKS"])
    host, port = "127.0.0.1", int(spec["port"])

    def run(sql: str) -> Sequence:
        connection = pg8000.native.Connection(
            spec["database_user"], password=os.environ["FF_DATABASE_PASSWORD"], host="127.0.0.1",
            database=spec["database_name"], timeout=2,
        )
        try:
            return connection.run(sql) or ()
        finally:
            connection.close()

    ThreadingHTTPServer((host, port), make_handler(run, spec, spec["app_health_url"])).serve_forever()


if __name__ == "__main__":
    main()
