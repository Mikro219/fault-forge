"""Validates targets, paths and filesystem locations before anything runs (docs/PLAN.md §4.9)."""

import ipaddress
import os
import re
import socket
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from faultforge.errors import FaultForgeError

LOOPBACK = "127.0.0.1"
MAX_PATH_LENGTH = 256
RUN_ID_PATTERN = re.compile(r"^run_\d{8}_\d{6}_[a-f0-9]{4}$")
FORBIDDEN_IN_PATH = ("//", "@", "\\", "#", "${")
ENCODED_SEPARATORS = re.compile(r"%(2f|5c|2e)", re.IGNORECASE)


def _unsafe(origin: str, reason: str) -> FaultForgeError:
    return FaultForgeError(
        "unsafe_target",
        f"refusing target {origin!r}: {reason}",
        "Use a target of the form http://127.0.0.1:<port>. Non-local targets aren't supported.",
    )


def validate_origin(origin: str) -> str:
    """Returns the canonical origin. Only http on the literal 127.0.0.1 is accepted."""
    if any(ord(char) <= 32 or ord(char) == 127 for char in origin):
        raise _unsafe(origin, "it contains whitespace or control characters")
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError as error:
        raise _unsafe(origin, str(error)) from error
    if parts.scheme != "http":
        raise _unsafe(origin, "the scheme must be http")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _unsafe(origin, "it must not contain credentials")
    if parts.path or parts.query or parts.fragment:
        raise _unsafe(origin, "it must not contain a path, query or fragment")
    if port is None or port == 0:
        raise _unsafe(origin, "it needs an explicit port")
    host = parts.hostname or ""
    if host == "localhost":
        _require_localhost_is_loopback(origin)
        host = LOOPBACK
    elif host != LOOPBACK:
        raise _unsafe(origin, "the host must be 127.0.0.1 or localhost")
    ipaddress.IPv4Address(host)
    return f"http://{host}:{port}"


def _require_localhost_is_loopback(origin: str) -> None:
    try:
        addresses = socket.getaddrinfo("localhost", None, family=socket.AF_INET)
    except OSError as error:
        raise _unsafe(origin, f"localhost doesn't resolve: {error}") from error
    if not addresses or any(
        not ipaddress.ip_address(address[4][0]).is_loopback for address in addresses
    ):
        raise _unsafe(origin, "localhost resolves to a non-loopback address")


def validate_path(path: str) -> str:
    def invalid(reason: str) -> FaultForgeError:
        return FaultForgeError(
            "config_invalid", f"invalid path {path!r}: {reason}", "Use a plain path such as /reserve."
        )

    if not path.startswith("/"):
        raise invalid("it must start with /")
    if len(path) > MAX_PATH_LENGTH:
        raise invalid(f"it is longer than {MAX_PATH_LENGTH} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise invalid("it contains control characters")
    for forbidden in FORBIDDEN_IN_PATH:
        if forbidden in path:
            raise invalid(f"it contains {forbidden!r}")
    if ".." in urlsplit(path).path.split("/"):
        raise invalid("it contains a .. segment")
    if ENCODED_SEPARATORS.search(path):
        raise invalid("it contains an encoded separator or dot")
    return path


def build_url(origin: str, path: str) -> str:
    """Joins a validated origin and path, then confirms the result still points at that origin."""
    url = f"{origin}{validate_path(path)}"
    parts, expected = urlsplit(url), urlsplit(origin)
    if (parts.scheme, parts.netloc) != (expected.scheme, expected.netloc):
        raise _unsafe(url, "the path changes the origin")
    return url


def allowed_roots(extra: Iterable[Path] | None = None) -> list[Path]:
    roots = [Path.cwd().resolve()]
    configured = os.environ.get("FAULTFORGE_ROOTS", "")
    roots.extend(Path(root).resolve() for root in configured.split(os.pathsep) if root)
    roots.extend(Path(root).resolve() for root in extra or [])
    return roots


def resolve_project_dir(project_dir: Path, roots: Iterable[Path]) -> Path:
    resolved = Path(project_dir).resolve()
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise FaultForgeError(
            "config_invalid",
            f"project directory {resolved} is outside the allowed roots",
            "Run from the project, or add its parent to FAULTFORGE_ROOTS.",
        )
    if not resolved.is_dir():
        raise FaultForgeError("config_invalid", f"project directory {resolved} doesn't exist")
    return resolved


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise FaultForgeError("run_not_found", f"invalid run id {run_id!r}")
    return run_id


def contained(root: Path, path: Path) -> Path:
    """Resolves path and requires it to stay inside root, which blocks symlink escapes."""
    resolved_root, resolved = root.resolve(), path.resolve()
    if resolved != resolved_root and not resolved.is_relative_to(resolved_root):
        raise FaultForgeError("config_invalid", f"{path} resolves outside {root}")
    return resolved
