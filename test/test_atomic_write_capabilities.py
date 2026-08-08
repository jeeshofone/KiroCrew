"""Tests for ``atomic_write``'s bytes and owner-only capabilities (issue #1105).

These two gaps are why a set of hand-rolled temp-write-and-rename sites could
not adopt the shared helper, and so silently missed the Windows rename retry:

* binary payloads (a compiled helper binary, an archive) could not pass through
  a ``content: str`` signature at all;
* secret-bearing payloads need an owner-only DACL applied BEFORE the content is
  written, and ``mode=`` cannot deliver that because ``fchmod_safe`` is a
  documented no-op on Windows.

The ordering assertions are the load-bearing ones. A lockdown applied after the
content is written, or a ``fchmod`` that widens the file back to the umask
default afterwards, would both still pass a naive "final mode is 0600" check
while leaving the secret exposed for the duration of the write.
"""

from __future__ import annotations

import logging
import os
import stat

import pytest

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat


def test_bytes_content_lands_verbatim(tmp_path):
    """Binary payloads must survive byte-for-byte, with no encoding applied."""
    target = tmp_path / "helper.bin"
    payload = bytes(range(256))

    aw.atomic_write(target, payload)

    assert target.read_bytes() == payload


def test_bytes_content_is_not_newline_translated(tmp_path):
    """The bug that text mode would introduce: CRLF rewriting inside a binary."""
    target = tmp_path / "payload.bin"

    aw.atomic_write(target, b"\r\n\n\r")

    assert target.read_bytes() == b"\r\n\n\r"


def test_str_content_still_writes_utf8(tmp_path):
    """The pre-existing text path must be untouched by the bytes addition."""
    target = tmp_path / "notes.txt"

    aw.atomic_write(target, "héllo")

    assert target.read_text(encoding="utf-8") == "héllo"


def test_newline_with_bytes_is_rejected(tmp_path):
    """Silently ignoring a meaningless argument would hide a caller bug."""
    with pytest.raises(TypeError, match="text-mode concept"):
        aw.atomic_write(tmp_path / "x.bin", b"data", newline="")


def test_restrict_to_owner_runs_before_any_content_is_written(tmp_path, monkeypatch):
    """The ordering IS the security property, so assert the sequence itself.

    A lockdown applied after the write leaves the secret readable for the whole
    write on Windows, where fchmod_safe does nothing.
    """
    events: list[str] = []
    real_restrict = platform_compat.restrict_to_owner

    def _spy(path):
        events.append("restrict")
        return real_restrict(path)

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _spy)

    target = tmp_path / "secret.key"
    original_fdopen = os.fdopen

    def _tracking_fdopen(fd, *args, **kwargs):
        handle = original_fdopen(fd, *args, **kwargs)
        real_write = handle.write

        def _write(data):
            events.append("write")
            return real_write(data)

        monkeypatch.setattr(handle, "write", _write, raising=False)
        return handle

    monkeypatch.setattr(os, "fdopen", _tracking_fdopen)

    aw.atomic_write(target, b"hmac-key-material", restrict_to_owner=True)

    assert events == ["restrict", "write"], events
    assert target.read_bytes() == b"hmac-key-material"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
def test_restrict_to_owner_is_not_widened_by_the_default_mode(tmp_path):
    """Regression guard: fchmod must not undo the lockdown it runs after.

    ``restrict_to_owner`` applies 0600 to the temp, then the helper fchmods it.
    Feeding the umask default in there would widen the file straight back.
    """
    target = tmp_path / "token.json"

    aw.atomic_write(target, "{}", restrict_to_owner=True)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
def test_explicit_owner_only_mode_is_accepted_alongside_the_flag(tmp_path):
    """The combination the real call sites use must not trip the guard."""
    target = tmp_path / "creds.json"

    aw.atomic_write(target, "{}", mode=0o600, restrict_to_owner=True)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_wider_mode_alongside_restrict_to_owner_is_rejected(tmp_path):
    """Narrowing it silently would hide the contradiction in the call."""
    with pytest.raises(ValueError, match="implies 0o600"):
        aw.atomic_write(tmp_path / "x", "data", mode=0o644, restrict_to_owner=True)


def test_a_failing_lockdown_leaves_no_temp_and_no_target(tmp_path, monkeypatch):
    """Fail-loud: a secret we cannot protect must not be written at all."""

    def _boom(path):
        raise OSError("cannot set DACL")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _boom)

    target = tmp_path / "secret.key"
    with pytest.raises(OSError, match="cannot set DACL"):
        aw.atomic_write(target, b"key", restrict_to_owner=True)

    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def _failing_restrict(monkeypatch):
    """Make the owner-only lockdown fail the way a read-only FS or icacls would."""

    def _boom(path):
        raise OSError("cannot set DACL")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _boom)


def test_restrict_on_error_warn_publishes_the_file_anyway(tmp_path, monkeypatch):
    """The counterpart to the fail-loud default, for callers that need the write.

    ``sel.py`` hard-fails every ``SecurityEventLog()`` init if its HMAC key is
    missing, and ``dashboard/refresh_tokens.py`` loses refresh-token
    reuse-detection state if its store is not persisted. For those two, dropping
    the write is the worse outcome, so the lockdown failure must not abort it.
    """
    _failing_restrict(monkeypatch)

    target = tmp_path / "hmac.key"
    aw.atomic_write(target, b"k" * 32, restrict_to_owner=True, restrict_on_error="warn")

    assert target.read_bytes() == b"k" * 32
    assert list(tmp_path.glob("*.tmp")) == [], "the temp must still be cleaned up"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
def test_restrict_on_error_warn_still_applies_the_posix_mode(tmp_path, monkeypatch):
    """A warn is NOT the same exposure on both platforms, and this pins which.

    On POSIX ``restrict_to_owner`` is ``chmod(0o600)``, and the ``fchmod_safe``
    that follows applies the same 0600 independently — so a warn still publishes
    an owner-only file here. On Windows ``fchmod_safe`` is a no-op, so there the
    same warn genuinely publishes under the inherited ACL. Asserting the POSIX
    half stops the docstring's claim from drifting away from the code.
    """
    _failing_restrict(monkeypatch)

    target = tmp_path / "hmac.key"
    aw.atomic_write(target, b"key", restrict_to_owner=True, restrict_on_error="warn")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_restrict_on_error_warn_does_not_log_the_payload(tmp_path, monkeypatch, caplog):
    """The warning fires on a secret write, so it must name the path, not the key."""
    _failing_restrict(monkeypatch)

    secret = b"correct-horse-battery-staple"
    target = tmp_path / "hmac.key"
    with caplog.at_level(logging.WARNING, logger="kiro_crew.atomic_write"):
        aw.atomic_write(target, secret, restrict_to_owner=True, restrict_on_error="warn")

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged, "premise: the warn path logged at all"
    assert secret.decode() not in logged, "the payload must never reach the log"
    assert target.name in logged, "the operator needs the destination path"


def test_restrict_on_error_without_restrict_to_owner_is_rejected(tmp_path):
    """Ignoring it would read as 'permissions handled' at a call site that has none."""
    with pytest.raises(ValueError, match="meaningless without restrict_to_owner"):
        aw.atomic_write(tmp_path / "x", "data", restrict_on_error="warn")
