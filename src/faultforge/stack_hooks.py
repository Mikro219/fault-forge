"""The stack hooks helper. Runs in its own container, inside a stack's sealed network namespace.

An app like a Next.js service has no /__ff endpoints, so this answers health, reset and state from
trusted SQL in the stack's definition. The app is untrusted and shares the loopback, so what this
reports can still be affected by what the app does to its own database. Standard library plus
pg8000 only: this file is copied alone into the image, and imports nothing from FaultForge.

State has no in_flight count, because a separate process can't see the app's requests.

Every call must carry the run's token, which the sandbox gives to this helper and to the runner
only. That keeps an app that calls a /__ff/ path by accident from resetting the data. It is not a
defence against a hostile app, which owns its schema and can act through it.

The helper logs in as an ordinary role that is a member of the tables' owner, never as a superuser,
so a trigger or a view function that fires in its session has no more than the owner's rights.
Every session sets its search_path first, so built-in names can't be shadowed from the app's
schema, and then row_security = off, so a statement that a row-security policy would filter fails
instead of reading or writing fewer rows than the tables hold.
"""

import hmac
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 4096
TOKEN_ENV = "FF_HOOKS_TOKEN"
TOKEN_HEADER = "X-FF-Hooks-Token"
DEFAULT_SEARCH_PATH = "pg_catalog, public"


def token_matches(expected: str, given: str | None) -> bool:
    """False for a missing token on either side: a helper without one answers nobody."""
    return bool(expected) and given is not None and hmac.compare_digest(expected.encode(), given.encode("utf-8", "replace"))


def session_statements(search_path: str, sql: str) -> list[str]:
    """What one session runs: the search_path, then row_security off, then the statement."""
    return [f"SET search_path = {search_path}", "SET row_security = off", sql]


def database_state(run: Callable[[str], Sequence], queries: dict[str, str]) -> dict[str, int]:
    return {name: int(run(sql)[0][0]) for name, sql in queries.items()}


def state_body(values: dict[str, int], build_id: str, instance_id: str, started_at: str) -> dict[str, object]:
    return {
        **values,
        "in_flight": 0,
        "stale_source": False,
        "build": {"build_id": build_id, "instance_id": instance_id, "instance_started_at": started_at, "stale_source": False},
    }


def make_handler(run: Callable[[str], Sequence], spec: dict, app_health_url: str, token: str = "") -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def _allowed(self) -> bool:
            if token_matches(token, self.headers.get(TOKEN_HEADER)):
                return True
            self._send(403, {"error": "token_required"})
            return False

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
                if not self._allowed():
                    return
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
                if not self._allowed():
                    return
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
            result: Sequence = ()
            for statement in session_statements(spec.get("search_path", DEFAULT_SEARCH_PATH), sql):
                result = connection.run(statement) or ()
            return result
        finally:
            connection.close()

    handler = make_handler(run, spec, spec["app_health_url"], os.environ.get(TOKEN_ENV, ""))
    ThreadingHTTPServer((host, port), handler).serve_forever()


if __name__ == "__main__":
    main()
