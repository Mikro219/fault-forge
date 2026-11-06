"""Identifies the code actually running, from loaded code objects rather than files on disk."""

import hashlib
import inspect
import marshal
import sys
import types
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _is_app_module(name: str) -> bool:
    return name == "app" or name == "ticket_support" or name.startswith("ticket_support.")


def _code_objects(owner: object, module_name: str, prefix: str) -> list[tuple[str, bytes]]:
    found = []
    for name, value in sorted(vars(owner).items()):
        qualified = f"{prefix}.{name}"
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if isinstance(value, types.FunctionType) and value.__module__ == module_name:
            found.append((qualified, marshal.dumps(value.__code__)))
        elif inspect.isclass(value) and value.__module__ == module_name:
            found.extend(_code_objects(value, module_name, qualified))
    return found


def _app_modules() -> list[types.ModuleType]:
    return [module for name, module in sorted(sys.modules.items()) if _is_app_module(name)]


def compute_build_id() -> str:
    digest = hashlib.sha256(sys.version.encode())
    for module in _app_modules():
        for qualified, code in _code_objects(module, module.__name__, module.__name__):
            digest.update(qualified.encode())
            digest.update(code)
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class Build:
    build_id: str
    instance_id: str
    started_at: float
    started_at_iso: str
    source_files: tuple[Path, ...]

    def stale_source(self) -> bool:
        """True if code on disk changed after this process started."""
        return any(path.stat().st_mtime > self.started_at for path in self.source_files)


def capture() -> Build:
    """Call once at process start, after the app's modules are imported."""
    now = datetime.now(UTC)
    files = tuple(
        Path(module.__file__).resolve() for module in _app_modules() if module.__file__ is not None
    )
    return Build(
        build_id=compute_build_id(),
        instance_id=uuid.uuid4().hex,
        started_at=now.timestamp(),
        started_at_iso=now.isoformat(),
        source_files=files,
    )
