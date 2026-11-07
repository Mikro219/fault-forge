"""Stored real runs, and the retention that never touches them.

A replay is written only by the worker, from a run that actually completed, and no endpoint can add
one. It's stored in its own directory, away from the project directories that retention removes.
"""

import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Collection
from pathlib import Path

from pydantic import ValidationError

from faultforge.web.viewmodel import ReplaySummaryView, ReplayView

logger = logging.getLogger("faultforge.web")
SAFE_ID = re.compile(r"r_[a-f0-9]{16}")
MAX_REPLAY_BYTES = 4 * 1024 * 1024
MAX_RECORDED_LOADED = 16
MAX_REPLAYS_LOADED = 500


def _regression_checked(replay: ReplayView) -> bool:
    return replay.fix is not None and any(attempt.regression for attempt in replay.fix.attempts)


class ReplayStore:
    """Replays are never deleted, so growth is bounded by storing only the first few of each kind:
    a target, a verdict, and whether a fix ran. A stored run gains its fix if that kind has room."""

    def __init__(self, directory: Path, per_kind: int, recorded_dir: Path | None = None):
        self.directory = directory
        self.per_kind = per_kind
        # Runs recorded on a development machine, shipped with the code. Only ever read.
        self.recorded_dir = recorded_dir
        self._lock = threading.Lock()
        self._replays: dict[str, ReplayView] = {}

    def load(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        paths = sorted(self.directory.glob("*.json"))[:MAX_REPLAYS_LOADED]
        if self.recorded_dir is not None:
            paths += sorted(self.recorded_dir.glob("*.json"))[:MAX_RECORDED_LOADED]
        for path in paths:
            try:
                if path.is_symlink() or path.stat().st_size > MAX_REPLAY_BYTES:
                    raise ValueError("not a plain, small file")
                replay = ReplayView.model_validate_json(path.read_bytes())
                if path.stem != replay.id or not SAFE_ID.fullmatch(replay.id):
                    raise ValueError("the name doesn't match")
            except (OSError, ValueError, ValidationError):
                # Left on disk for a person to look at, and never served.
                logger.warning("skipped an invalid replay file")
                continue
            self._replays[replay.id] = replay

    @staticmethod
    def _kind(replay: ReplayView) -> tuple[str, str | None, bool, bool]:
        """Regression-checked runs are their own kind, so stored older runs can't crowd them out."""
        return replay.target, replay.result.verdict, replay.fix is not None, _regression_checked(replay)

    def save(self, replay: ReplayView) -> bool:
        if not SAFE_ID.fullmatch(replay.id) or replay.result.status != "completed":
            return False
        with self._lock:
            if replay.id not in self._replays or self._kind(self._replays[replay.id]) != self._kind(replay):
                kind = self._kind(replay)
                if sum(self._kind(item) == kind for item in self._replays.values()) >= self.per_kind:
                    return False
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{replay.id}.json"
            temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(replay.model_dump(mode="json")), encoding="utf-8")
            os.replace(temp, path)
            self._replays[replay.id] = replay
            return True

    def get(self, replay_id: str) -> ReplayView | None:
        with self._lock:
            return self._replays.get(replay_id)

    def summaries(self) -> list[ReplaySummaryView]:
        with self._lock:
            replays = sorted(self._replays.values(), key=lambda item: item.recorded_at, reverse=True)
        return [
            ReplaySummaryView(
                id=item.id, recorded_at=item.recorded_at, target=item.target, verdict=item.result.verdict,
                fix_verified=any(attempt.passed for attempt in item.fix.attempts) if item.fix else False,
                has_fix=item.fix is not None,
                regression_checked=_regression_checked(item),
                recorded_on=item.recorded_on,
            )
            for item in replays
        ]

    def __len__(self) -> int:
        with self._lock:
            return len(self._replays)


def apply_retention(projects_dir: Path, *, now: float, max_age_s: float, keep: int, active: Collection[str]) -> list[str]:
    """Removes project directories, and the run artifacts inside them, that are too old or beyond
    the newest `keep`. Only direct children of projects_dir are touched, and replays live elsewhere.
    A directory in use is skipped. Returns the names removed."""
    try:
        entries = sorted(
            ((path.lstat().st_mtime, path) for path in projects_dir.iterdir()),
            key=lambda item: item[0], reverse=True,
        )
    except OSError:
        return []
    removed = []
    kept = 0
    for modified, path in entries:
        if path.name in active:
            kept += 1
            continue
        if kept < keep and now - modified < max_age_s:
            kept += 1
            continue
        try:
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError as error:
            logger.warning("retention couldn't remove a project directory (%s)", type(error).__name__)
            continue
        removed.append(path.name)
    return removed
