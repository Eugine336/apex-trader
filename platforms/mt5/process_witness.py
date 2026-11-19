"""Observe the running Windows terminal process without reading its command line."""
from __future__ import annotations

import ntpath


def terminal_process_for(install_dir: str, previous=None):
    """Return the unique running MT5 process from this installation, or no witness."""
    if not isinstance(install_dir, str) or not ntpath.isabs(install_dir):
        return None
    import psutil  # Windows deployment dependency; MT5's Python API supplies no process ID.

    directory = ntpath.normcase(ntpath.normpath(install_dir))
    if previous is not None and previous[0] == directory and previous[1].is_running():
        return previous
    matches = []
    try:
        for process in psutil.process_iter(["name"]):
            name = (process.info["name"] or "").lower()
            if name not in {"terminal.exe", "terminal64.exe"}:
                continue
            try:
                folder = ntpath.normcase(ntpath.dirname(process.exe()))
                if folder == directory:
                    # Resolve creation time now. is_running() subsequently checks PID reuse.
                    process.create_time()
                    matches.append(process)
            except psutil.NoSuchProcess:  # SAFE-FALLBACK: process exited during the snapshot.
                continue
    except psutil.Error:
        # A same-name process whose path cannot be checked makes the binding ambiguous.
        return None
    return (directory, matches[0]) if len(matches) == 1 else None
