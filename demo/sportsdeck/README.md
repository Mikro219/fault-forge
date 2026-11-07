# SportsDeck stack

A real concurrency target with Next.js, Postgres and Redis. The code isn't vendored here: it's built from
a Git repo at a pinned commit, into a local image, with no secrets.

```bash
bash demo/sportsdeck/build.sh <repo-url-or-path> 065e475 buggy
bash demo/sportsdeck/build.sh <repo-url-or-path> 065e475 fixed demo/sportsdeck/reference_fix
uv run pytest tests/test_stack.py
```

The commit's lockfile doesn't match its `package.json`, so `build.sh` falls back to `npm install` and says so. That leaves dependency versions unpinned.
