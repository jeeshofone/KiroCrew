"""A ``.env`` saved with a byte-order mark by default Windows tooling.

PowerShell 5.1's ``Out-File -Encoding utf8`` writes UTF-8 with a BOM, and its
default ``>`` / ``Out-File`` writes UTF-16LE with a BOM. Every reader and
in-place rewriter of the data home's ``.env`` must agree on the first key of
such a file: the readers load it un-prefixed, the rewriters can clear or
replace it, and a UTF-16 file is treated as unset instead of crashing the load.
"""

from __future__ import annotations

import codecs
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig, read_env_file_credential

_BODY = "JIRA_API_TOKEN=abc123\r\nSLACK_BOT_TOKEN=xyz\r\n"


def _utf8_bom(tmp_path: Path) -> Path:
    ep = tmp_path / ".env"
    ep.write_bytes(codecs.BOM_UTF8 + _BODY.encode("utf-8"))
    return ep


def _utf16_bom(tmp_path: Path) -> Path:
    ep = tmp_path / ".env"
    ep.write_bytes(_BODY.encode("utf-16"))  # native order, with a BOM
    assert ep.read_bytes()[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
    return ep


@pytest.fixture(autouse=True)
def _fresh_warning_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loader, "_warned_undecodable_env", set(), raising=False)


def _load(monkeypatch: pytest.MonkeyPatch, ep: Path) -> dict[str, str]:
    monkeypatch.setattr(loader, "env_path", lambda: ep)
    for key in ("JIRA_API_TOKEN", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return KiroCrewConfig.__new__(KiroCrewConfig).load_credentials(propagate=False)


class TestReaders:
    def test_utf8_bom_loads_the_first_key_unprefixed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        creds = _load(monkeypatch, _utf8_bom(tmp_path))
        assert creds["JIRA_API_TOKEN"] == "abc123"
        assert creds["SLACK_BOT_TOKEN"] == "xyz"
        assert not any(k.startswith("\ufeff") for k in creds)

    def test_utf8_bom_single_key_read(self, tmp_path: Path) -> None:
        assert read_env_file_credential("JIRA_API_TOKEN", _utf8_bom(tmp_path)) == "abc123"

    def test_utf16_is_unset_with_one_warning_naming_the_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ep = _utf16_bom(tmp_path)
        with caplog.at_level(logging.WARNING, logger=loader.logger.name):
            creds = _load(monkeypatch, ep)
            assert read_env_file_credential("JIRA_API_TOKEN", ep) == ""
        assert "JIRA_API_TOKEN" not in creds
        warnings = [r for r in caplog.records if "Cannot decode" in r.getMessage()]
        assert len(warnings) == 1
        assert str(ep) in warnings[0].getMessage()

    def test_plain_utf8_and_crlf_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ep = tmp_path / ".env"
        ep.write_bytes(_BODY.encode("utf-8"))
        assert _load(monkeypatch, ep) == {"JIRA_API_TOKEN": "abc123", "SLACK_BOT_TOKEN": "xyz"}
        assert read_env_file_credential("SLACK_BOT_TOKEN", ep) == "xyz"

    def test_service_warning_sees_a_bom_first_key(self, tmp_path: Path) -> None:
        from kiro_crew.service.common import _names_defined_in_env_file

        assert "JIRA_API_TOKEN" in _names_defined_in_env_file(_utf8_bom(tmp_path))
        assert _names_defined_in_env_file(_utf16_bom(tmp_path)) == set()


class TestRewriters:
    """A clear through a rewriter must remove the key the gateway loads."""

    def test_dashboard_channel_clear_removes_a_bom_first_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import messaging

        ep = _utf8_bom(tmp_path)
        monkeypatch.setattr(loader, "env_path", lambda: ep)
        messaging._write_env_updates({"JIRA_API_TOKEN": None})
        assert read_env_file_credential("JIRA_API_TOKEN", ep) == ""
        assert read_env_file_credential("SLACK_BOT_TOKEN", ep) == "xyz"
        assert not ep.read_bytes().startswith(codecs.BOM_UTF8)

    def test_weixin_writers_match_a_bom_first_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import weixin_qr as qr

        ep = tmp_path / ".env"
        ep.write_bytes(codecs.BOM_UTF8 + b"WEIXIN_TOKEN=old\nOTHER=1\n")
        monkeypatch.setattr(qr, "env_path", lambda: ep)
        assert qr._read_env_value("WEIXIN_TOKEN") == "old"
        qr._write_env_secret("WEIXIN_TOKEN", "new")
        assert read_env_file_credential("WEIXIN_TOKEN", ep) == "new"
        assert ep.read_text(encoding="utf-8").count("WEIXIN_TOKEN=") == 1

        ep.write_bytes(codecs.BOM_UTF8 + b"WEIXIN_TOKEN=old\nOTHER=1\n")
        qr._delete_env_key("WEIXIN_TOKEN")
        assert read_env_file_credential("WEIXIN_TOKEN", ep) == ""

    def test_rewriter_refuses_a_utf16_file_without_overwriting_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import messaging

        ep = _utf16_bom(tmp_path)
        before = ep.read_bytes()
        monkeypatch.setattr(loader, "env_path", lambda: ep)
        with pytest.raises(UnicodeDecodeError):
            messaging._write_env_updates({"JIRA_API_TOKEN": None})
        assert ep.read_bytes() == before


class TestMigrate:
    def _run(self, tmp_path: Path, ep: Path, **kwargs):
        from kiro_crew.secrets.migrate import migrate_env_secrets

        with (
            patch("kiro_crew.secrets.migrate.env_path", return_value=ep),
            patch("kiro_crew.secrets.migrate.config_dir", return_value=tmp_path / "cfg"),
        ):
            return migrate_env_secrets(**kwargs)

    def test_utf8_bom_first_key_migrates_and_keeps_the_bom(self, tmp_path: Path) -> None:
        ep = _utf8_bom(tmp_path)
        report = self._run(tmp_path, ep, dry_run=False)
        assert report.migrated == ["JIRA_API_TOKEN"]
        after = ep.read_bytes()
        assert after.startswith(codecs.BOM_UTF8)
        assert after[3:] == b"JIRA_API_TOKEN=secret://JIRA_API_TOKEN\r\nSLACK_BOT_TOKEN=xyz\r\n"

    def test_utf16_migrates_nothing(self, tmp_path: Path) -> None:
        ep = _utf16_bom(tmp_path)
        before = ep.read_bytes()
        report = self._run(tmp_path, ep, dry_run=False)
        assert report.migrated == []
        assert ep.read_bytes() == before
