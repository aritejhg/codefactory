"""Protected local controller credentials; independent of cloud provisioning."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class ControllerCredentialError(RuntimeError):
    """A credential is unavailable; never include its contents in the error."""


def read_controller_token(path: Path) -> str:
    try:
        fd = os.open(path.expanduser(), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise ControllerCredentialError("controller credential file is unavailable") from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ControllerCredentialError("controller credential must be user-owned with mode 0600")
        raw = os.read(fd, 64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ControllerCredentialError("controller credential file is too large")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise ControllerCredentialError("controller credential file is invalid") from None
        if not value or any(ch.isspace() for ch in value):
            raise ControllerCredentialError("controller credential file is empty or invalid")
        return value
    finally:
        os.close(fd)
