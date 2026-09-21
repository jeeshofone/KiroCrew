"""Native Kiro launch views whose skill directory is supplied by Crew.

The authored resource mapping remains the authority for Crew search/list/read.
Native aliases preserve the other spec fields but carry no skill:// resources:
Kiro 2.21.2 progressively loads bodies, yet enumerates all their metadata before
the first prompt. Bounding only the Crew prompt cannot bound that native cost.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import logging
import os
import stat
import threading
import uuid
import weakref
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.agent_discovery import (
    SCOPE_PROJECT,
    _read_agent_spec,
    agent_spec_effective_name,
    clear_list_agents_cache,
    list_agents,
)
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_agents_dir, kiro_home, project_agents_dir
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_crew.workspace_cli_settings import workspace_cli_settings_lock

logger = logging.getLogger(__name__)

_MANAGED_SETTING = "kirocrew.skillDiscovery.inheritFiles"
_INHERIT_SETTING = "chat.disableInheritingDefaultResources"
_INHERIT_SOURCE = "kirocrew.skillDiscovery.inheritSource"
_PREVIOUS_INHERITANCE = "kirocrew.skillDiscovery.previousInheritance"
_SEARCH_TOOL = "@kirocrew-core/skill_search"
_PROJECTION_LOCK_NAME = ".kirocrew-skill-projection.lock"
_PROJECTION_LEASE_DIR_NAME = ".kirocrew-skill-projection-leases"
_PROJECTION_LEASE_RECORD_SUFFIX = ".json"
_PROJECTION_LEASE_HOLDER_SUFFIX = ".lock"
_PROJECTION_LEASE_MAX_ALIASES = 1024
_PROJECTION_LEASE_MAX_BYTES = 65536
# A single host can legitimately run many workspaces and projections. The reader
# still needs a finite absence proof before it can authorize alias deletion.
_PROJECTION_LEASE_SCAN_LIMIT = 4096
# Pruning is maintenance on the native startup path. Overflow is deferred to a
# later projection instead of increasing one launch's filesystem work without bound.
_PROJECTION_PRUNE_WORK_LIMIT = 4096
_PROJECTION_METADATA_DIR_NAME = ".kirocrew-skill-projection-metadata"
# Native startup uses a short dedicated ceiling instead of the platform lock's
# generic five-minute ceiling.
_PROJECTION_LOCK_TIMEOUT_SECS = 2.0

# Generated specs stay in Kiro's shared agents directory, so metadata identifies
# them for direct scanners and scopes cleanup to the owning Kiro Crew data home.
_MANAGED_MARKER = "x-kirocrew-managed"
_MANAGED_MARKER_VALUE = "skill-view"
_MANAGED_CREW_HOME = "x-kirocrew-home"
_MANAGED_WORK_DIR = "x-kirocrew-work-dir"
_MANAGED_AGENT = "x-kirocrew-agent"
_MANAGED_SOURCE = "x-kirocrew-source"
_MANAGED_ALIAS_SHA256 = "x-kirocrew-alias-sha256"
_MANAGED_STAGED_OWNERSHIP = "x-kirocrew-staged-ownership"


def _parse_projection_json(raw: bytes) -> Any | None:
    """Parse bounded projection-owned JSON, returning uncertainty on malformed input."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None


@dataclass
class NativeSkillProjection:
    """Translate transport identities while Crew keeps the authored agent name."""

    aliases: dict[str, str]
    specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    search_agents: set[str] = field(default_factory=set)
    _lease_finalizer: Any = field(default=None, repr=False, compare=False)

    def agent(self, name: str) -> str:
        if name not in self.aliases:
            if name in self.errors:
                raise ValueError(f"Agent {name!r}: {self.errors[name]}")
            raise ValueError(f"Agent {name!r} has no prepared skill discovery view")
        return self.aliases[name]

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session/set_mode":
            return {**params, "modeId": self.agent(str(params.get("modeId", "")))}
        if method == "_kiro.dev/commands/execute":
            command = params.get("command", "")
            if isinstance(command, dict):
                name = str(command.get("command", "")).lstrip("/")
                args = command.get("args") or {}
                value = str(args.get("value", "")) if isinstance(args, dict) else ""
            else:
                words = str(command).strip().lstrip("/").split(None, 1)
                name = words[0] if words else ""
                value = words[1] if len(words) > 1 else ""
            if name == "agent" and value.strip() not in {"list", "schema"}:
                raise ValueError(
                    "Use Crew's agent selector to change agents so its skill scope stays in sync."
                )
        return params

    def frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        reverse = {alias: name for name, alias in self.aliases.items()}

        def visit(value: Any, field: str = "") -> Any:
            if isinstance(value, dict):
                return {key: visit(item, key) for key, item in value.items()}
            if isinstance(value, list):
                if field == "availableModes":
                    value = [
                        item
                        for item in value
                        if isinstance(item, dict) and item.get("id") in reverse
                    ]
                return [visit(item) for item in value]
            if field in {"id", "name", "agentName", "modeId", "currentModeId"} and isinstance(
                value, str
            ):
                return reverse.get(value, value)
            return value

        return visit(frame)


_ACTIVE_PROJECTIONS: weakref.WeakValueDictionary[int, NativeSkillProjection] = (
    weakref.WeakValueDictionary()
)
_ACTIVE_PROJECTIONS_LOCK = threading.Lock()


def _register_active_projection(projection: NativeSkillProjection) -> None:
    with _ACTIVE_PROJECTIONS_LOCK:
        _ACTIVE_PROJECTIONS[id(projection)] = projection


def _active_aliases() -> set[str]:
    with _ACTIVE_PROJECTIONS_LOCK:
        projections = tuple(_ACTIVE_PROJECTIONS.values())
    return {alias for projection in projections for alias in projection.aliases.values()}


def _projection_alias_lock(directory: Path) -> ExitStack:
    """Acquire the bounded cross-process lock for alias publication and pruning."""
    stack = ExitStack()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / _PROJECTION_LOCK_NAME
        if platform_compat.is_link_or_junction(lock_path):
            raise OSError("skill projection lock is a symlink or junction")
        lock_fd = stack.enter_context(platform_compat.open_lock_file(lock_path))
        opened = os.fstat(lock_fd)
        named = pinned_fs.lstat_by_name(lock_path)
        if (
            platform_compat.is_link_or_junction(lock_path)
            or named is None
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("skill projection lock changed while it was opened")
        stack.enter_context(
            platform_compat.file_lock(
                lock_fd, exclusive=True, timeout=_PROJECTION_LOCK_TIMEOUT_SECS
            )
        )
        current = pinned_fs.lstat_by_name(lock_path)
        if (
            platform_compat.is_link_or_junction(lock_path)
            or current is None
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("skill projection lock changed while it was acquired")
    except OSError:
        stack.close()
        raise
    return stack


def _ensure_projection_metadata_directory(directory: Path) -> Path:
    """Create and verify the hidden directory that owns projection sidecars."""
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    if platform_compat.is_link_or_junction(metadata_dir):
        raise OSError("skill projection metadata directory is a symlink or junction")
    metadata_dir.mkdir(parents=True, exist_ok=True)
    info = pinned_fs.lstat_by_name(metadata_dir)
    if (
        platform_compat.is_link_or_junction(metadata_dir)
        or info is None
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise OSError("skill projection metadata directory is not a real directory")
    return metadata_dir


def _unlink_projection_lease_if_unchanged(path: Path, identity: tuple[int, int]) -> bool:
    """Remove one unlocked lease only while its random name keeps its identity."""
    current = pinned_fs.lstat_by_name(path)
    if (
        current is None
        or platform_compat.is_link_or_junction(path)
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        return False
    if pinned_fs.supports_pinned_walk() and os.unlink in os.supports_dir_fd:
        try:
            parent_fd = os.open(path.parent, pinned_fs.dir_flags())
        except OSError:
            return False
        try:
            return pinned_fs.unlink_verified(parent_fd, path.name, identity)
        finally:
            os.close(parent_fd)
    if not platform_compat.IS_WINDOWS:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _projection_lease_holder_path(record_path: Path) -> Path:
    """Return the lifetime-lock sidecar paired with one readable lease record."""
    return record_path.with_name(
        record_path.name[: -len(_PROJECTION_LEASE_RECORD_SUFFIX)] + _PROJECTION_LEASE_HOLDER_SUFFIX
    )


def _acquire_projection_lease(directory: Path, aliases: set[str]) -> ExitStack:
    """Publish one readable lease record and hold its separate lock sidecar."""
    stack = ExitStack()
    if not aliases:
        return stack
    record = json.dumps({"aliases": sorted(aliases)}, separators=(",", ":"))
    record_bytes = record.encode("utf-8")
    if (
        len(aliases) > _PROJECTION_LEASE_MAX_ALIASES
        or len(record_bytes) > _PROJECTION_LEASE_MAX_BYTES
    ):
        raise OSError(
            "skill projection lease exceeds its reader bound "
            f"({len(aliases)} aliases, {len(record_bytes)} bytes)"
        )

    record_path: Path | None = None
    record_identity: tuple[int, int] | None = None
    holder_path: Path | None = None
    holder_identity: tuple[int, int] | None = None
    try:
        lease_dir = directory / _PROJECTION_LEASE_DIR_NAME
        if platform_compat.is_link_or_junction(lease_dir):
            raise OSError("skill projection lease directory is a symlink or junction")
        lease_dir.mkdir(parents=True, exist_ok=True)
        lease_info = pinned_fs.lstat_by_name(lease_dir)
        if (
            platform_compat.is_link_or_junction(lease_dir)
            or lease_info is None
            or not stat.S_ISDIR(lease_info.st_mode)
        ):
            raise OSError("skill projection lease directory is not a real directory")

        stem = f"{os.getpid()}-{uuid.uuid4().hex}"
        record_path = lease_dir / f"{stem}{_PROJECTION_LEASE_RECORD_SUFFIX}"
        holder_path = lease_dir / f"{stem}{_PROJECTION_LEASE_HOLDER_SUFFIX}"
        atomic_write(record_path, record, restrict_to_owner=True)
        record_created = pinned_fs.lstat_by_name(record_path)
        if (
            record_created is None
            or platform_compat.is_link_or_junction(record_path)
            or not stat.S_ISREG(record_created.st_mode)
        ):
            raise OSError("skill projection lease record was not a regular file")
        record_identity = (record_created.st_dev, record_created.st_ino)

        atomic_write(holder_path, "", restrict_to_owner=True)
        holder_created = pinned_fs.lstat_by_name(holder_path)
        if (
            holder_created is None
            or platform_compat.is_link_or_junction(holder_path)
            or not stat.S_ISREG(holder_created.st_mode)
        ):
            raise OSError("skill projection lease holder was not a regular file")
        holder_identity = (holder_created.st_dev, holder_created.st_ino)

        # Contexts leave first, then the record is removed before its holder. A
        # crash between those unlinks can leave only an inert holder, never a
        # record whose now-missing liveness proof must be retained as uncertain.
        stack.callback(_unlink_projection_lease_if_unchanged, holder_path, holder_identity)
        stack.callback(_unlink_projection_lease_if_unchanged, record_path, record_identity)
        holder_fd = stack.enter_context(platform_compat.open_lock_file(holder_path, create=False))
        opened = os.fstat(holder_fd)
        named = pinned_fs.lstat_by_name(holder_path)
        if (
            platform_compat.is_link_or_junction(record_path)
            or platform_compat.is_link_or_junction(holder_path)
            or named is None
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != holder_identity
            or (named.st_dev, named.st_ino) != holder_identity
        ):
            raise OSError("skill projection lease holder changed while it was opened")
        stack.enter_context(platform_compat.file_lock(holder_fd, exclusive=True, wait=False))
        current_record = pinned_fs.lstat_by_name(record_path)
        current_holder = pinned_fs.lstat_by_name(holder_path)
        if (
            current_record is None
            or current_holder is None
            or platform_compat.is_link_or_junction(record_path)
            or platform_compat.is_link_or_junction(holder_path)
            or (current_record.st_dev, current_record.st_ino) != record_identity
            or (current_holder.st_dev, current_holder.st_ino) != holder_identity
        ):
            raise OSError("skill projection lease changed while it was acquired")
    except OSError:
        stack.close()
        if record_path is not None and record_identity is not None:
            _unlink_projection_lease_if_unchanged(record_path, record_identity)
        if holder_path is not None and holder_identity is not None:
            _unlink_projection_lease_if_unchanged(holder_path, holder_identity)
        raise
    return stack


def _alias_has_external_lease(directory: Path, alias: str) -> bool:
    """Return whether another process may still use *alias*; uncertainty is live.

    Lease JSON is never locked because Windows byte-range locks are mandatory.
    Its paired holder is never parsed and carries the lifetime OS lock. A valid
    pair whose holder lock can be acquired is crash/finalizer residue, so both
    identity-verified sidecars are reclaimed.
    """
    lease_dir = directory / _PROJECTION_LEASE_DIR_NAME
    lease_info = pinned_fs.lstat_by_name(lease_dir)
    if lease_info is None:
        return False
    if platform_compat.is_link_or_junction(lease_dir) or not stat.S_ISDIR(lease_info.st_mode):
        return True
    scanned = 0
    try:
        with os.scandir(lease_dir) as entries:
            for entry in entries:
                if not entry.name.endswith(_PROJECTION_LEASE_RECORD_SUFFIX):
                    continue
                scanned += 1
                if scanned > _PROJECTION_LEASE_SCAN_LIMIT:
                    return True
                record_path = lease_dir / entry.name
                holder_path = _projection_lease_holder_path(record_path)
                stack = ExitStack()
                stale_identities: tuple[tuple[int, int], tuple[int, int]] | None = None
                try:
                    if platform_compat.is_link_or_junction(
                        record_path
                    ) or platform_compat.is_link_or_junction(holder_path):
                        return True
                    record_named = pinned_fs.lstat_by_name(record_path)
                    holder_named = pinned_fs.lstat_by_name(holder_path)
                    if (
                        record_named is None
                        or holder_named is None
                        or not stat.S_ISREG(record_named.st_mode)
                        or not stat.S_ISREG(holder_named.st_mode)
                    ):
                        return True
                    record_identity = (record_named.st_dev, record_named.st_ino)
                    holder_identity = (holder_named.st_dev, holder_named.st_ino)
                    record_fingerprint = (record_named.st_size, record_named.st_mtime_ns)

                    record_fd = platform_compat.open_file_no_reparse(record_path, nonblocking=True)
                    stack.callback(os.close, record_fd)
                    record_opened = os.fstat(record_fd)
                    if (
                        not stat.S_ISREG(record_opened.st_mode)
                        or (record_opened.st_dev, record_opened.st_ino) != record_identity
                        or (record_opened.st_size, record_opened.st_mtime_ns) != record_fingerprint
                    ):
                        return True
                    raw = os.read(record_fd, _PROJECTION_LEASE_MAX_BYTES + 1)
                    if len(raw) > _PROJECTION_LEASE_MAX_BYTES:
                        return True
                    body = _parse_projection_json(raw)
                    listed = body.get("aliases") if isinstance(body, dict) else None
                    if (
                        not isinstance(listed, list)
                        or len(listed) > _PROJECTION_LEASE_MAX_ALIASES
                        or any(not isinstance(value, str) for value in listed)
                    ):
                        return True
                    record_after_read = pinned_fs.lstat_by_name(record_path)
                    if (
                        record_after_read is None
                        or platform_compat.is_link_or_junction(record_path)
                        or (record_after_read.st_dev, record_after_read.st_ino) != record_identity
                        or (record_after_read.st_size, record_after_read.st_mtime_ns)
                        != record_fingerprint
                    ):
                        return True

                    holder_fd = stack.enter_context(
                        platform_compat.open_lock_file(holder_path, create=False)
                    )
                    holder_opened = os.fstat(holder_fd)
                    holder_after_open = pinned_fs.lstat_by_name(holder_path)
                    if (
                        holder_after_open is None
                        or platform_compat.is_link_or_junction(holder_path)
                        or not stat.S_ISREG(holder_opened.st_mode)
                        or not stat.S_ISREG(holder_after_open.st_mode)
                        or (holder_opened.st_dev, holder_opened.st_ino) != holder_identity
                        or (holder_after_open.st_dev, holder_after_open.st_ino) != holder_identity
                    ):
                        return True
                    try:
                        with platform_compat.file_lock(holder_fd, exclusive=True, wait=False):
                            record_current = pinned_fs.lstat_by_name(record_path)
                            holder_current = pinned_fs.lstat_by_name(holder_path)
                            if (
                                record_current is None
                                or holder_current is None
                                or platform_compat.is_link_or_junction(record_path)
                                or platform_compat.is_link_or_junction(holder_path)
                                or (record_current.st_dev, record_current.st_ino) != record_identity
                                or (record_current.st_size, record_current.st_mtime_ns)
                                != record_fingerprint
                                or (holder_current.st_dev, holder_current.st_ino) != holder_identity
                            ):
                                return True
                            stale_identities = (record_identity, holder_identity)
                    except (BlockingIOError, OSError):
                        record_current = pinned_fs.lstat_by_name(record_path)
                        if (
                            record_current is None
                            or platform_compat.is_link_or_junction(record_path)
                            or (record_current.st_dev, record_current.st_ino) != record_identity
                            or (record_current.st_size, record_current.st_mtime_ns)
                            != record_fingerprint
                        ):
                            return True
                        if alias in listed:
                            return True
                except (OSError, ValueError, TypeError):
                    return True
                finally:
                    stack.close()
                if stale_identities is not None:
                    record_identity, holder_identity = stale_identities
                    record_removed = _unlink_projection_lease_if_unchanged(
                        record_path, record_identity
                    )
                    holder_removed = _unlink_projection_lease_if_unchanged(
                        holder_path, holder_identity
                    )
                    if record_removed or holder_removed:
                        logger.debug("skill projection: reclaimed stale lease %s", record_path.name)
    except OSError:
        return True
    return False


def _settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = safe_read_file_bytes(str(path))
    if raw is None:
        raise ValueError(f"Cannot read Kiro settings at {path}")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Kiro settings must be an object: {path}")
    return data


def _restore_inheritance(path: Path, local: dict[str, Any]) -> None:
    """Undo only our overlay; a changed or removed native setting wins."""
    inherited = local.get(_MANAGED_SETTING)
    source = local.get(_INHERIT_SOURCE)
    if not isinstance(inherited, bool) or source not in ("local", "global"):
        return
    previous = local.get(_PREVIOUS_INHERITANCE)
    if previous is None:
        # Views prepared before rollback support recorded source and a boolean.
        previous = {"present": source == "local", "value": not inherited}
    if (
        not isinstance(previous, dict)
        or not isinstance(previous.get("present"), bool)
        or (previous["present"] and "value" not in previous)
    ):
        raise ValueError(f"Cannot restore Crew's inheritance overlay at {path}")
    if local.get(_INHERIT_SETTING) is True:
        if previous["present"]:
            local[_INHERIT_SETTING] = previous["value"]
        else:
            local.pop(_INHERIT_SETTING, None)
    for key in (_MANAGED_SETTING, _INHERIT_SOURCE, _PREVIOUS_INHERITANCE):
        local.pop(key, None)
    atomic_write(path, json.dumps(local, indent=2))


def _managed_marker(spec: object) -> bool:
    """Return whether a generated spec carries this lifecycle's marker."""
    return isinstance(spec, dict) and spec.get(_MANAGED_MARKER) == _MANAGED_MARKER_VALUE


def _managed_metadata_path_is_safe(path: Path) -> bool:
    """Return whether untrusted managed metadata is safe to probe on this host."""
    if not platform_compat.IS_WINDOWS:
        return True
    try:
        # Ask only about the volume root first. Unlike is_dir/is_file, this does
        # not resolve the attacker-controlled path or initiate SMB authentication.
        if platform_compat.path_volume_is_remote(path) is not False:
            return False
        return platform_compat.first_linked_ancestor(
            path
        ) is None and not platform_compat.is_link_or_junction(path)
    except (OSError, ValueError):
        return False


def _managed_alias_is_stale(spec: dict[str, Any]) -> bool:
    """Return whether a managed view's authored source cannot regenerate it."""
    work_dir_raw = spec.get(_MANAGED_WORK_DIR)
    agent = spec.get(_MANAGED_AGENT)
    source_raw = spec.get(_MANAGED_SOURCE)
    if (
        not isinstance(work_dir_raw, str)
        or not work_dir_raw
        or not isinstance(agent, str)
        or not agent
        or not isinstance(source_raw, str)
        or not source_raw
    ):
        return False
    try:
        work_dir = Path(work_dir_raw)
        if not _managed_metadata_path_is_safe(work_dir):
            return False
        if not work_dir.is_dir():
            return True
        source = Path(source_raw)
        project_source_parent = project_agents_dir(str(work_dir)).absolute()
        source_parent = source.absolute().parent
        expected_parents = {
            project_source_parent,
            kiro_agents_dir().absolute(),
        }
        if source_parent not in expected_parents or source.suffix not in {
            ".json",
            ".md",
        }:
            return False
        if not _managed_metadata_path_is_safe(source):
            return False
        if not source.is_file():
            return True
        authored = _read_agent_spec(source, operation="native_skill_projection", source="acp")
    except (OSError, ValueError):
        return False
    # Read/parse uncertainty is fail-safe. A valid source whose effective
    # dispatchable name differs proves this recorded pair cannot be regenerated.
    return (
        isinstance(authored, dict)
        and agent_spec_effective_name(
            source,
            authored,
            strip_legacy_suffix=source_parent == project_source_parent,
        )
        != agent
    )


def _unlink_alias_if_unchanged(path: Path, identity: tuple[int, int]) -> bool:
    """Unlink *path* only while it still names the classified alias inode.

    The caller holds the global projection lock, which excludes every product
    publisher. POSIX additionally pins the parent descriptor. Windows lacks
    unlink-at, so it performs one final no-link identity check before the
    by-name unlink; other platforms without a pinned walk retain the alias.
    """
    if pinned_fs.supports_pinned_walk() and os.unlink in os.supports_dir_fd:
        try:
            parent_fd = os.open(path.parent, pinned_fs.dir_flags())
        except OSError:
            return False
        try:
            return pinned_fs.unlink_verified(parent_fd, path.name, identity)
        finally:
            os.close(parent_fd)

    if platform_compat.IS_WINDOWS:
        current = pinned_fs.lstat_by_name(path)
        if (
            current is None
            or platform_compat.is_link_or_junction(path)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != identity
        ):
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True

    # An unknown non-Windows platform without descriptor-relative unlink has
    # neither the POSIX identity pin nor Windows' publication-lock contract.
    return False


def _ownership_record_for_digest(metadata: object, alias_digest: str) -> dict[str, Any] | None:
    """Return the current or staged ownership record covering exact alias bytes."""
    if not isinstance(metadata, dict):
        return None
    current = {key: value for key, value in metadata.items() if key != _MANAGED_STAGED_OWNERSHIP}
    if _managed_marker(current) and current.get(_MANAGED_ALIAS_SHA256) == alias_digest:
        return current
    staged = metadata.get(_MANAGED_STAGED_OWNERSHIP)
    if (
        isinstance(staged, dict)
        and _managed_marker(staged)
        and staged.get(_MANAGED_ALIAS_SHA256) == alias_digest
    ):
        return dict(staged)
    return None


def _managed_metadata_for_alias(
    directory: Path, path: Path, alias_raw: bytes
) -> tuple[dict[str, Any], Path | None, tuple[int, int] | None, bytes | None] | None:
    """Load ownership outside the Kiro agent spec, or one legacy in-spec record."""
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    directory_info = pinned_fs.lstat_by_name(metadata_dir)
    if directory_info is not None:
        if platform_compat.is_link_or_junction(metadata_dir) or not stat.S_ISDIR(
            directory_info.st_mode
        ):
            return None
        metadata_path = metadata_dir / f"{path.stem}.json"
        metadata_info = pinned_fs.lstat_by_name(metadata_path)
        if metadata_info is not None:
            if platform_compat.is_link_or_junction(metadata_path) or not stat.S_ISREG(
                metadata_info.st_mode
            ):
                return None
            try:
                metadata_raw = safe_read_file_bytes(str(metadata_path))
            except FileTooLargeError:
                return None
            if metadata_raw is None:
                return None
            metadata = _parse_projection_json(metadata_raw)
            ownership = _ownership_record_for_digest(
                metadata, hashlib.sha256(alias_raw).hexdigest()
            )
            if ownership is None:
                return None
            return (
                ownership,
                metadata_path,
                (metadata_info.st_dev, metadata_info.st_ino),
                metadata_raw,
            )

    # Migration-only fallback for aliases produced by earlier review heads.
    # New views never carry these keys because kiro-cli denies unknown fields.
    legacy = _parse_projection_json(alias_raw)
    if not isinstance(legacy, dict) or not _managed_marker(legacy):
        return None
    return legacy, None, None, None


def _publish_projected_alias(
    directory: Path,
    metadata_dir: Path,
    alias: str,
    alias_raw: str,
    ownership: dict[str, Any],
) -> None:
    """Publish metadata before its alias while preserving an existing valid generation."""
    alias_path = directory / f"{alias}.json"
    metadata_path = metadata_dir / f"{alias}.json"
    alias_bytes = alias_raw.encode()
    current_raw: bytes | None = None
    current_ownership: dict[str, Any] | None = None
    current_info = pinned_fs.lstat_by_name(alias_path)
    if current_info is not None:
        if platform_compat.is_link_or_junction(alias_path) or not stat.S_ISREG(
            current_info.st_mode
        ):
            raise OSError("existing skill projection alias is not a regular file")
        try:
            current_raw = safe_read_file_bytes(str(alias_path))
        except FileTooLargeError as exc:
            raise OSError("existing skill projection alias exceeds its reader bound") from exc
        if current_raw is None:
            raise OSError("existing skill projection alias cannot be read safely")
        current = _managed_metadata_for_alias(directory, alias_path, current_raw)
        if current is None:
            raise OSError("existing skill projection ownership cannot be verified")
        current_ownership = current[0]

    new_ownership = {
        **ownership,
        _MANAGED_ALIAS_SHA256: hashlib.sha256(alias_bytes).hexdigest(),
    }
    staged_ownership: dict[str, Any] = new_ownership
    needs_convergence = False
    if current_raw is not None and current_ownership is not None:
        current_digest = hashlib.sha256(current_raw).hexdigest()
        if current_digest != new_ownership[_MANAGED_ALIAS_SHA256]:
            current_record = {
                **current_ownership,
                _MANAGED_ALIAS_SHA256: current_digest,
            }
            current_record.pop(_MANAGED_STAGED_OWNERSHIP, None)
            staged_ownership = {
                **current_record,
                _MANAGED_STAGED_OWNERSHIP: new_ownership,
            }
            needs_convergence = True

    atomic_write(
        metadata_path,
        json.dumps(staged_ownership, ensure_ascii=False, separators=(",", ":")),
        restrict_to_owner=True,
    )
    atomic_write(alias_path, alias_raw, restrict_to_owner=True)
    if needs_convergence:
        atomic_write(
            metadata_path,
            json.dumps(new_ownership, ensure_ascii=False, separators=(",", ":")),
            restrict_to_owner=True,
        )


def _prune_stale_managed_aliases(directory: Path, crew_home_id: str, *, keep: set[str]) -> None:
    """Remove stale aliases owned by this Kiro Crew data home while its lock is held."""
    active = _active_aliases()
    work = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not (
                    entry.name.startswith(NATIVE_SKILL_ALIAS_PREFIX)
                    and entry.name.endswith(".json")
                ):
                    continue
                if work >= _PROJECTION_PRUNE_WORK_LIMIT:
                    logger.debug(
                        "skill projection: deferred remaining alias pruning after %d candidates",
                        work,
                    )
                    return
                work += 1
                path = directory / entry.name
                if (
                    path.stem in keep
                    or path.stem in active
                    or _alias_has_external_lease(directory, path.stem)
                ):
                    continue
                candidate = pinned_fs.lstat_by_name(path)
                if candidate is None:
                    continue
                identity = (candidate.st_dev, candidate.st_ino)
                try:
                    raw = safe_read_file_bytes(str(path))
                except FileTooLargeError:
                    continue
                if raw is None:
                    continue
                managed = _managed_metadata_for_alias(directory, path, raw)
                if managed is None:
                    continue
                metadata, metadata_path, metadata_identity, metadata_raw = managed
                if metadata.get(_MANAGED_CREW_HOME) != crew_home_id:
                    continue
                if not _managed_alias_is_stale(metadata):
                    continue

                # Re-open and revalidate the exact alias and ownership sidecar at
                # deletion time. A sidecar digest binds the ownership record to these
                # projected bytes; any replacement or uncertainty keeps both files.
                current = pinned_fs.lstat_by_name(path)
                if current is None or (current.st_dev, current.st_ino) != identity:
                    continue
                try:
                    current_raw = safe_read_file_bytes(str(path))
                except FileTooLargeError:
                    continue
                if current_raw != raw:
                    continue
                current_managed = _managed_metadata_for_alias(directory, path, current_raw)
                if current_managed is None:
                    continue
                (
                    current_metadata,
                    current_metadata_path,
                    current_metadata_identity,
                    current_metadata_raw,
                ) = current_managed
                if (
                    current_metadata != metadata
                    or current_metadata_path != metadata_path
                    or current_metadata_identity != metadata_identity
                    or current_metadata_raw != metadata_raw
                    or current_metadata.get(_MANAGED_CREW_HOME) != crew_home_id
                    or not _managed_alias_is_stale(current_metadata)
                ):
                    continue
                if _unlink_alias_if_unchanged(path, identity):
                    if metadata_path is not None and metadata_identity is not None:
                        _unlink_projection_lease_if_unchanged(metadata_path, metadata_identity)
                    logger.info("skill projection: pruned stale managed alias %s", path.name)
                else:
                    logger.debug("skill projection: stale alias changed before removal: %s", path)
    except OSError:
        logger.debug("skill projection: cannot list %s to prune aliases", directory, exc_info=True)
        return


def prepare_native_skill_projection(
    work_dir: Path, *, enabled: bool | None = None
) -> NativeSkillProjection | None:
    """Prepare native views after spec freshness admission, before spawning.

    Uses the existing workspace CLI settings channel. No home, identity store,
    session store or authored agent file is relocated or rewritten.
    """
    directory = kiro_agents_dir()
    crew_home_id = data_home().absolute().as_posix()
    work_dir_id = work_dir.absolute().as_posix()
    if enabled is None:
        enabled = os.environ.get("KIROCREW_NATIVE_SKILL_PROJECTION", "1") != "0"
    if not enabled:
        if not (work_dir / ".kiro" / "settings" / "cli.json").exists():
            return None
        try:
            with workspace_cli_settings_lock(work_dir) as locked_settings:
                _restore_inheritance(locked_settings, _settings(locked_settings))
        except OSError:
            logger.warning(
                "skill projection: workspace settings lock unavailable during rollback",
                exc_info=True,
            )
        return None
    try:
        alias_lock = _projection_alias_lock(directory)
    except OSError:
        logger.warning(
            "skill projection: alias lock unavailable; retaining aliases, settings, and using "
            "authored agents",
            exc_info=True,
        )
        # No workspace settings snapshot exists before this lock acquisition, so
        # returning preserves the current file byte-for-byte.
        return None
    with alias_lock:
        clear_list_agents_cache()
        global_settings = _settings(kiro_home() / "settings" / "cli.json")
        aliases: dict[str, str] = {}
        specs: dict[str, dict[str, Any]] = {}
        ownership: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        search_agents: set[str] = set()
        for agent in list_agents(project_dir=str(work_dir)):
            if not agent.filename:
                continue
            source_dir = (
                project_agents_dir(str(work_dir)) if agent.scope == SCOPE_PROJECT else directory
            )
            source = source_dir / agent.filename
            spec = _read_agent_spec(source, operation="native_skill_projection", source="acp")
            if spec is None:
                continue
            identity = f"{crew_home_id}\n{work_dir_id}\n{agent.name}"
            alias = NATIVE_SKILL_ALIAS_PREFIX + hashlib.sha256(identity.encode()).hexdigest()[:24]
            view = copy.deepcopy(spec)
            view["name"] = alias
            resources = view.get("resources", [])
            resources = resources if isinstance(resources, list) else []
            view["resources"] = [
                r for r in resources if not (isinstance(r, str) and r.startswith("skill://"))
            ]
            needs_search = agent.name == "kirocrew" or any(
                isinstance(r, str) and r.startswith("skill://") for r in resources
            )
            if needs_search:
                excluded = view.get("excludedTools", [])
                if isinstance(excluded, list) and any(
                    isinstance(t, str)
                    and (t == "@kirocrew-core" or fnmatch.fnmatchcase(_SEARCH_TOOL, t))
                    for t in excluded
                ):
                    errors[agent.name] = (
                        "skill_search is explicitly excluded; bounded skill discovery requires it"
                    )
                    continue
                # The bounded directory must have a loading path even for a custom spec
                # whose authored resources rely on native skill activation. Expose
                # only the read/search capability; do not grant server-wide tools or
                # change the author's approval policy.
                from kiro_crew.agent import managed_mcp_spec_entry

                servers = view.setdefault("mcpServers", {})
                if not isinstance(servers, dict):
                    errors[agent.name] = "mcpServers must be an object"
                    continue
                original_core = servers.get("kirocrew-core", {})
                if not isinstance(original_core, dict):
                    errors[agent.name] = "kirocrew-core must be a server object"
                    continue
                disabled = original_core.get("disabled", False)
                disabled_tools = original_core.get("disabledTools", [])
                if not isinstance(disabled, bool):
                    errors[agent.name] = "kirocrew-core.disabled must be a boolean"
                    continue
                if not isinstance(disabled_tools, list) or any(
                    not isinstance(tool, str) for tool in disabled_tools
                ):
                    errors[agent.name] = "kirocrew-core.disabledTools must be a list of strings"
                    continue
                if disabled or "skill_search" in disabled_tools:
                    errors[agent.name] = (
                        "skill_search is disabled; bounded skill discovery requires it"
                    )
                    continue
                entry = managed_mcp_spec_entry("kirocrew-core")
                if entry is None:
                    errors[agent.name] = "Crew's managed skill search server is unavailable"
                    continue
                for key in ("autoApprove", "disabledTools", "timeout"):
                    if key in original_core:
                        entry[key] = original_core[key]
                servers["kirocrew-core"] = entry
                tools = view.get("tools", [])
                if tools != "*" and isinstance(tools, list):
                    if not any(t in tools for t in ("*", "@kirocrew-core", _SEARCH_TOOL)):
                        view["tools"] = [*tools, _SEARCH_TOOL]
                search_agents.add(agent.name)
            prompt = view.get("prompt")
            if isinstance(prompt, str) and prompt.startswith("file://"):
                path = Path(prompt[7:]).expanduser()
                if not path.is_absolute():
                    view["prompt"] = "file://" + (source.parent / path).absolute().as_posix()
            aliases[agent.name] = alias
            specs[agent.name] = view
            ownership[alias] = {
                _MANAGED_MARKER: _MANAGED_MARKER_VALUE,
                _MANAGED_CREW_HOME: crew_home_id,
                _MANAGED_WORK_DIR: work_dir_id,
                _MANAGED_AGENT: agent.name,
                _MANAGED_SOURCE: source.absolute().as_posix(),
            }

        try:
            settings_lock = workspace_cli_settings_lock(work_dir)
            with settings_lock as locked_settings:
                # This is the authoritative read for both projected resources and
                # the write below. Every in-product workspace cli.json writer uses
                # the same sidecar lock, so no effort or Tool Search update can land
                # between this read and commit.
                local = _settings(locked_settings)
                inherited = local.get(_MANAGED_SETTING)
                preference_source = local.get(_INHERIT_SOURCE)
                if not isinstance(inherited, bool) or local.get(_INHERIT_SETTING) is not True:
                    local[_PREVIOUS_INHERITANCE] = {
                        "present": _INHERIT_SETTING in local,
                        "value": local.get(_INHERIT_SETTING),
                    }
                    preference_source = "local" if _INHERIT_SETTING in local else "global"
                    inherited = (
                        local.get(_INHERIT_SETTING, global_settings.get(_INHERIT_SETTING))
                        is not True
                    )
                elif preference_source == "global":
                    inherited = global_settings.get(_INHERIT_SETTING) is not True

                if inherited:
                    for view in specs.values():
                        for resource in (
                            f"file://{kiro_home().as_posix()}/steering/**/*.md",
                            "file://.kiro/steering/**/*.md",
                            "file://AGENTS.md",
                        ):
                            if resource not in view["resources"]:
                                view["resources"].append(resource)

                metadata_dir = _ensure_projection_metadata_directory(directory) if aliases else None
                lease_stack = _acquire_projection_lease(directory, set(aliases.values()))
                try:
                    for agent_name, alias in aliases.items():
                        assert metadata_dir is not None
                        _publish_projected_alias(
                            directory,
                            metadata_dir,
                            alias,
                            json.dumps(specs[agent_name], ensure_ascii=False),
                            ownership[alias],
                        )
                    local[_MANAGED_SETTING] = inherited
                    local[_INHERIT_SOURCE] = preference_source
                    local[_INHERIT_SETTING] = True
                    atomic_write(locked_settings, json.dumps(local, indent=2))
                    prepared = NativeSkillProjection(aliases, specs, errors, search_agents)
                    prepared._lease_finalizer = weakref.finalize(prepared, lease_stack.close)
                except BaseException:
                    lease_stack.close()
                    raise
        except OSError:
            logger.warning(
                "skill projection: workspace settings or lease lock unavailable; retaining "
                "aliases and using authored agents",
                exc_info=True,
            )
            return None
        _register_active_projection(prepared)
        _prune_stale_managed_aliases(directory, crew_home_id, keep=set(aliases.values()))

    return prepared
