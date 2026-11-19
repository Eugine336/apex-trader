"""Identify the active MT5 installation and account without persisting either identifier."""

from __future__ import annotations

import hashlib
import json
import ntpath
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MT5TerminalIdentity:
    fingerprint: str
    server: str


def read_terminal_identity(mt5: Any) -> MT5TerminalIdentity | None:
    """Use observations from the connected terminal, including its per-install data path."""
    terminal = mt5.terminal_info()
    if getattr(terminal, "connected", None) is not True:
        return None
    account = mt5.account_info()
    login = getattr(account, "login", None)
    server = getattr(account, "server", None)
    path = getattr(terminal, "data_path", None)
    if type(login) is not int or login <= 0:
        return None
    if not isinstance(server, str) or not server.strip() or not isinstance(path, str) or not path.strip():
        return None
    data = json.dumps([login, server.strip(), ntpath.normcase(ntpath.normpath(path.strip()))])
    return MT5TerminalIdentity(hashlib.sha256(data.encode("utf-8")).hexdigest(), server.strip())
