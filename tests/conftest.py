"""Session-wide test defaults.

The MCP server reads the owner's real Vultr and inference keys from ~/.config/faultforge unless
FF_VULTR_ENV and FF_INFERENCE_ENV point elsewhere. Setting them here, in os.environ before any test
runs, makes a missing file the default for the whole session. monkeypatch.undo() restores env vars
to these values, never to "unset", so no test can fall back to the real key files even after undo.
"""

import os
import tempfile
from pathlib import Path

_NO_KEYS = Path(tempfile.mkdtemp(prefix="ff-test-no-keys-"))
os.environ["FF_VULTR_ENV"] = str(_NO_KEYS / "vultr.env")
os.environ["FF_INFERENCE_ENV"] = str(_NO_KEYS / "inference.env")
