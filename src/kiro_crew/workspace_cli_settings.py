"""Cross-process serialization for workspace ``cli.json`` overlays."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat

CLI_SETTINGS_LOCK_NAME = ".kirocrew-cli-settings.lock"
CLI_SETTINGS_LOCK_TIMEOUT_SECS = 2.0


@contextmanager
def workspace_cli_settings_lock(work_dir: Path) -> Iterator[Path]:
    """Yield a workspace ``cli.json`` path while its verified lock is held."""
    stack = ExitStack()
    try:
        settings_dir = work_dir / ".kiro" / "settings"
        settings_dir.mkdir(parents=True, exist_ok=True)
        lock_path = settings_dir / CLI_SETTINGS_LOCK_NAME
        if platform_compat.is_link_or_junction(lock_path):
            raise OSError("workspace CLI settings lock is a symlink or junction")
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
            raise OSError("workspace CLI settings lock changed while it was opened")
        stack.enter_context(
            platform_compat.file_lock(
                lock_fd,
                exclusive=True,
                timeout=CLI_SETTINGS_LOCK_TIMEOUT_SECS,
            )
        )
        current = pinned_fs.lstat_by_name(lock_path)
        if (
            platform_compat.is_link_or_junction(lock_path)
            or current is None
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("workspace CLI settings lock changed while it was acquired")
        yield settings_dir / "cli.json"
    finally:
        stack.close()
