"""Test isolation: redirect state to a per-session temp dir BEFORE any
arcus_mcp import. Production state dir is never touched by tests."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="arcus-qa-")
os.environ["ARCUS_GATEWAY_DATA"] = _TMP
