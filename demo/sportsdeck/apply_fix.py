"""Applies a reference fix: exact line replacements, refused unless they match. Usage: apply_fix.py DIR ROOT"""

import sys
from pathlib import Path


def main(fix_dir: Path, root: Path) -> None:
    lines = (fix_dir / "route.patch").read_text().splitlines()
    target = root / lines[1].removeprefix("+++ b/")
    old = "\n".join(line[1:] for line in lines[3:] if line.startswith("-")) + "\n"
    new = "\n".join(line[1:] for line in lines[3:] if line.startswith("+")) + "\n"
    text = target.read_text()
    if text.count(old) != 1:
        raise SystemExit("the fix doesn't match this commit")
    target.write_text(text.replace(old, new))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
