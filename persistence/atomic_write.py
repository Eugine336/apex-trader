"""Atomic file writer — crash-safe persistence for JSON state files.

Writes to a temporary file in the same directory, flushes + fsyncs,
then atomically replaces the target via os.replace().  On POSIX and
Windows (NTFS), os.replace is atomic, so a crash mid-write can never
leave a partial/corrupt target file.
"""

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically.

    The caller sees either the old file or the complete new file — never
    a half-written state.  The temp file is created in the same directory
    so that os.replace() never crosses filesystem boundaries.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            print(f"[atomic_write] failed to clean up temp file: {tmp}", file=__import__('sys').stderr)
        raise
