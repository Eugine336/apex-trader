from __future__ import annotations

import threading

from event_driven_bootstrap import EventDrivenSystem


def test_reservation_ledger_lazy_initialization_is_singleton_under_concurrency():
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._portfolio_admission_reservations_lock = threading.Lock()
    barrier = threading.Barrier(12)
    ledgers = []
    failures = []

    def resolve():
        try:
            barrier.wait(timeout=5.0)
            ledgers.append(system._get_portfolio_admission_reservations())
        except BaseException as exc:  # test must surface thread failures
            failures.append(exc)

    threads = [threading.Thread(target=resolve) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert len(ledgers) == 12
    assert len({id(ledger) for ledger in ledgers}) == 1
