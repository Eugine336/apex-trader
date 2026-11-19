from types import SimpleNamespace

import pytest

from platforms.mt5.terminal_identity import read_terminal_identity


def _feed(login=710231, server="fixture-server", path=r"C:\Fixture\Terminal", connected=True):
    return SimpleNamespace(
        terminal_info=lambda: SimpleNamespace(connected=connected, data_path=path),
        account_info=lambda: SimpleNamespace(login=login, server=server),
    )


def test_fingerprint_binds_live_account_server_and_install_and_omits_account_and_path():
    identity = read_terminal_identity(_feed())
    assert identity == read_terminal_identity(_feed(path="c:/fixture/terminal/"))
    assert len(identity.fingerprint) == 64
    assert "710231" not in repr(identity) and "Fixture" not in repr(identity)
    assert identity.server == "fixture-server"
    for other in (_feed(login=710232), _feed(server="other"), _feed(path=r"C:\Other")):
        assert read_terminal_identity(other).fingerprint != identity.fingerprint


@pytest.mark.parametrize("feed", [_feed(connected=False), _feed(login=0), _feed(login=True),
                                  _feed(server=""), _feed(path=" ")])
def test_incomplete_or_disconnected_terminal_has_no_reusable_identity(feed):
    assert read_terminal_identity(feed) is None
