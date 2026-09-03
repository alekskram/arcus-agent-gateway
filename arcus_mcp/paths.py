"""Data directory resolution for repo vs installed copies.

State (none in v0.1.0, reserved for cache persistence) stays OUT of
site-packages: ~/.local/state/arcus-agent-gateway (overridable via
ARCUS_GATEWAY_DATA).
"""
import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("ARCUS_GATEWAY_DATA")
    if env:
        d = Path(env)
    else:
        d = Path.home() / ".local" / "state" / "arcus-agent-gateway"
    d.mkdir(parents=True, exist_ok=True)
    return d
