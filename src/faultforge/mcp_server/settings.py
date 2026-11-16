"""Where the MCP server keeps its state, and which projects it may touch (docs/PLAN.md V4.5).

The allowlist is read on every call, so an edit to mcp.toml applies without a restart. It fails
closed: a missing, unreadable or malformed file means no project is allowed.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from faultforge.errors import FaultForgeError

CONFIG_ENV = "FF_MCP_CONFIG"
STATE_ENV = "FF_MCP_STATE_DIR"
MAX_CONFIG_BYTES = 64 * 1024
MAX_ROOTS = 64
MAX_PATH_CHARS = 4096
MAX_IMAGES = 64
MAX_IMAGE_CHARS = 256
CONFIG_KEYS = {"allowed_roots", "allowed_images"}


def config_path() -> Path:
    configured = os.environ.get(CONFIG_ENV)
    return Path(configured) if configured else Path.home() / ".config" / "faultforge" / "mcp.toml"


def state_dir() -> Path:
    configured = os.environ.get(STATE_ENV)
    return Path(configured) if configured else Path.home() / ".local" / "state" / "faultforge"


@dataclass(frozen=True)
class McpSettings:
    config_path: Path
    state_dir: Path
    allowed_roots: tuple[Path, ...]
    # Why the allowlist is empty or shorter than the file says. Fixed text plus paths only.
    problems: tuple[str, ...] = ()
    # The prebuilt app images a stack project may name. Empty by default, so none is allowed.
    allowed_images: tuple[str, ...] = ()


def _string_list(data: dict, key: str, limit: int) -> list[str]:
    raw = data.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{key} must be a list of strings")
    if len(raw) > limit:
        raise ValueError(f"{key} has more than {limit} entries")
    return raw


def _parse_config(text: str) -> tuple[tuple[Path, ...], tuple[str, ...], list[str]]:
    data = tomllib.loads(text)
    unknown = set(data) - CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown keys {sorted(unknown)}")
    images = _string_list(data, "allowed_images", MAX_IMAGES)
    if any(not image or len(image) > MAX_IMAGE_CHARS or "\0" in image or image.startswith("-") for image in images):
        raise ValueError("allowed_images entries must be plain image names such as myapp:buggy")
    roots: list[Path] = []
    problems: list[str] = []
    for item in _string_list(data, "allowed_roots", MAX_ROOTS):
        path = Path(item).expanduser()
        if not item or "\0" in item or len(item) > MAX_PATH_CHARS or not path.is_absolute():
            problems.append(f"ignored {item!r}: a root must be an absolute path")
            continue
        resolved = path.resolve()
        if resolved == Path(resolved.anchor):
            problems.append(f"ignored {item!r}: the filesystem root can't be a project root")
        elif not resolved.is_dir():
            problems.append(f"ignored {item!r}: not a directory")
        else:
            roots.append(resolved)
    return tuple(dict.fromkeys(roots)), tuple(dict.fromkeys(images)), problems


def load_settings() -> McpSettings:
    path, state = config_path(), state_dir()
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError(f"larger than {MAX_CONFIG_BYTES} bytes")
        roots, images, problems = _parse_config(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return McpSettings(path, state, (), (f"{path} doesn't exist, so no project is allowed",))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as error:
        return McpSettings(path, state, (), (f"{path} is invalid, so no project is allowed: {error}",))
    return McpSettings(path, state, roots, tuple(problems), images)


def require_image_allowed(image: str | None, settings: McpSettings) -> None:
    """A stack project names the image it runs, so the owner must list it; otherwise any local image
    could be started under the project's stack.yaml."""
    if image is None or image not in settings.allowed_images:
        raise FaultForgeError(
            "project_not_allowed",
            f"app_image {image!r} isn't in allowed_images" if image else "stack.yaml names no app_image",
            f"Add the image to allowed_images in {settings.config_path}, for example "
            f'allowed_images = ["{image or "myapp:buggy"}"], then call the tool again.',
        )


def resolve_project(project_dir: object, settings: McpSettings) -> Path:
    """Symlinks are followed before the check, so a link inside a root can't point outside it."""
    if not isinstance(project_dir, str) or not project_dir or "\0" in project_dir or len(project_dir) > MAX_PATH_CHARS:
        raise FaultForgeError("invalid_argument", "project_dir must be a non-empty path", "Pass the project's absolute path.")
    path = Path(project_dir).expanduser()
    if not path.is_absolute():
        raise FaultForgeError("invalid_argument", f"project_dir {project_dir!r} isn't absolute", "Pass the project's absolute path.")
    resolved = path.resolve()
    if not any(resolved == root or resolved.is_relative_to(root) for root in settings.allowed_roots):
        raise FaultForgeError(
            "project_not_allowed",
            f"{resolved} isn't inside an allowed project root",
            f"Add its directory to allowed_roots in {settings.config_path}, for example "
            f'allowed_roots = ["{resolved}"], then call the tool again.',
        )
    if not resolved.is_dir():
        raise FaultForgeError("invalid_argument", f"{resolved} isn't a directory")
    return resolved


def private_dir(path: Path) -> Path:
    """Creates the directory, and its parents, readable by this user only."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise FaultForgeError("engine_failed", f"{path} isn't a directory")
    return path
