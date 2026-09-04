#!/usr/bin/env python3
"""v0.2.1: thin shim kept for git-checkout users.

Since 0.2.1 the recorder ships inside the wheel as the package module
arcus_mcp/recorder.py - `pip install arcus-agent-gateway[recorder]`
puts `python -m arcus_mcp.recorder` everywhere (that is also what the
systemd unit now runs; scripts/ was never packaged, which is what
broke the /opt install). This file stays so a plain `git clone` +
`pip install -e .` checkout can keep invoking the familiar path:

    python scripts/recorder.py --once

It does nothing by itself: it delegates to arcus_mcp.recorder
(re-exported below) and exits with the [recorder] install hint when
arcus_mcp cannot be imported at all (neither installed nor running
from a checkout).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Checkout case: running `python scripts/recorder.py` before any pip
# install - the repo root (the package's parent) is not on sys.path.
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    from arcus_mcp import recorder as _recorder
except ImportError:  # not installed AND not a checkout root: give the hint
    sys.exit("arcus_mcp is not importable - the recorder moved into the "
             "package in 0.2.1: pip install 'arcus-agent-gateway[recorder]' "
             "(or run from a git checkout of the repo)")

# Delegate everything to the real module. `import *` re-exports the
# public helpers (universe, snap_row, write_snapshot, roll_daily, ...);
# main/tick are named explicitly as the entry points this shim runs.
from arcus_mcp.recorder import *  # noqa: E402,F401,F403
from arcus_mcp.recorder import main, tick  # noqa: E402,F401

if __name__ == "__main__":
    main()
