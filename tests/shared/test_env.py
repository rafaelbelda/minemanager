"""Environment parsing shared by both packages."""

from __future__ import annotations

from pathlib import Path

import pytest

from minemanager_shared.env import env_bool, env_csv, env_int, env_opt, env_path, env_str

VAR = "MM_TEST_PROBE"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)


class TestBool:
    @pytest.mark.parametrize("raw", ["1", "TRUE", "yes", " on "])
    def test_truthy(self, monkeypatch, raw):
        monkeypatch.setenv(VAR, raw)
        assert env_bool(VAR) is True

    @pytest.mark.parametrize("raw", ["0", "false", "nonsense"])
    def test_falsy(self, monkeypatch, raw):
        monkeypatch.setenv(VAR, raw)
        assert env_bool(VAR) is False

    def test_default_used_when_unset(self):
        assert env_bool(VAR) is False
        assert env_bool(VAR, True) is True

    def test_blank_falls_back_to_default(self, monkeypatch):
        """Blank is 'unset' everywhere else, so it must not silently mean False."""
        monkeypatch.setenv(VAR, "   ")
        assert env_bool(VAR, True) is True


class TestInt:
    def test_parses_and_trims(self, monkeypatch):
        monkeypatch.setenv(VAR, "  42  ")
        assert env_int(VAR, 7) == 42

    def test_blank_uses_default(self, monkeypatch):
        monkeypatch.setenv(VAR, "   ")
        assert env_int(VAR, 7) == 7

    def test_unset_uses_default(self):
        assert env_int(VAR, 7) == 7

    def test_malformed_exits_with_a_readable_message(self, monkeypatch):
        monkeypatch.setenv(VAR, "not-a-number")
        with pytest.raises(SystemExit, match="must be an integer"):
            env_int(VAR, 1)

class TestCsv:
    def test_splits_and_drops_blanks(self, monkeypatch):
        monkeypatch.setenv(VAR, "a, b ,,c ")
        assert env_csv(VAR) == ["a", "b", "c"]

    def test_unset_uses_default(self):
        assert env_csv(VAR, "x,y") == ["x", "y"]
        assert env_csv(VAR) == []

class TestStrPathOpt:
    def test_str_default(self):
        assert env_str(VAR, "fallback") == "fallback"

    def test_str_value(self, monkeypatch):
        monkeypatch.setenv(VAR, " value ")
        assert env_str(VAR, "fallback") == "value"

    def test_opt_is_none_when_unset(self):
        assert env_opt(VAR) is None

    def test_opt_blank_is_none(self, monkeypatch):
        monkeypatch.setenv(VAR, "   ")
        assert env_opt(VAR) is None

    def test_path_default_and_value(self, monkeypatch):
        assert env_path(VAR, "/var/lib/x") == Path("/var/lib/x")
        monkeypatch.setenv(VAR, "/tmp/other")
        assert env_path(VAR, "/var/lib/x") == Path("/tmp/other")


def test_hub_settings_read_through_the_helpers(monkeypatch):
    from minemanager_hub import config

    monkeypatch.setenv("MM_PORT", "9999")
    monkeypatch.setenv("MM_ENABLE_DOCS", "yes")
    monkeypatch.setenv("MM_ALLOWED_HOSTS", "a.example, b.example")
    settings = config.Settings()
    assert settings.port == 9999
    assert settings.enable_docs is True
    assert settings.allowed_hosts == {"a.example", "b.example"}


def test_agent_config_reads_through_the_helpers(monkeypatch, tmp_path):
    from minemanager_agent.config import AgentConfig

    monkeypatch.setenv("MM_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MM_ALLOW_INSECURE", "1")
    monkeypatch.delenv("MM_ENROLL_TOKEN", raising=False)
    cfg = AgentConfig()
    assert cfg.data_dir == tmp_path
    assert cfg.allow_insecure is True
    assert cfg.enroll_token is None
