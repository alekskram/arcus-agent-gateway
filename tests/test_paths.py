"""data_dir() resolution: ARCUS_GATEWAY_DATA wins, state stays out of
site-packages. conftest.py already points the env var at a per-session
temp dir before any arcus_mcp import, so these tests never touch the
production state directory (~/.local/state/arcus-agent-gateway)."""
from pathlib import Path

from arcus_mcp.paths import data_dir


def test_data_dir_respects_env_tmp_dir():
    """conftest sets ARCUS_GATEWAY_DATA to a temp dir: data_dir() must
    resolve under it (never the production path)."""
    import os
    env = os.environ.get("ARCUS_GATEWAY_DATA")
    assert env, "conftest.py must set ARCUS_GATEWAY_DATA before imports"
    d = data_dir()
    assert Path(env) in d.parents or d == Path(env)
    assert str(d).startswith(str(Path(env)))
    assert ".local" not in str(d.relative_to(Path(env)))  # not the default


def test_data_dir_is_created_and_stable():
    a, b = data_dir(), data_dir()
    assert a == b and a.is_dir()


def test_data_dir_env_override_wins(monkeypatch, tmp_path):
    custom = tmp_path / "state-x"
    monkeypatch.setenv("ARCUS_GATEWAY_DATA", str(custom))
    d = data_dir()
    assert d == custom
    assert d.is_dir()


def test_data_dir_default_without_env(monkeypatch):
    monkeypatch.delenv("ARCUS_GATEWAY_DATA", raising=False)
    d = data_dir()
    assert d == Path.home() / ".local" / "state" / "arcus-agent-gateway"
