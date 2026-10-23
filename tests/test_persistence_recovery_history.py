from persistence.recovery import find_confirmed_close_deals


def test_only_matching_mt5_exit_deals_confirm_a_closed_position():
    confirmed = find_confirmed_close_deals(
        {"1836084408", "55"},
        [
            # An entry deal must not prove that the position later closed.
            {"position_id": 1836084408, "entry": 0, "ticket": 10, "time": 100},
            # Matching exit-side deal confirms lifecycle closure.
            {"position_id": 1836084408, "entry": 1, "ticket": 11, "time": 200},
            # A close-by exit also confirms closure.
            {"position_id": "55", "entry": 3, "ticket": 12, "time": 210},
            # Deals for other positions must not affect the requested set.
            {"position_id": 99, "entry": 1, "ticket": 13, "time": 220},
            # Unknown entry type must be ignored.
            {"position_id": 55, "entry": 9, "ticket": 14, "time": 230},
        ],
    )

    assert set(confirmed) == {"1836084408", "55"}
    assert [deal["ticket"] for deal in confirmed["1836084408"]] == [11]
    assert [deal["ticket"] for deal in confirmed["55"]] == [12]


def test_empty_or_unavailable_history_never_confirms_closure():
    assert find_confirmed_close_deals({"1836084408"}, []) == {}
    assert find_confirmed_close_deals(
        {"1836084408"},
        [{"position_id": 1836084408, "entry": 0, "ticket": 10}],
    ) == {}


def test_position_ids_are_matched_exactly_after_numeric_normalization():
    confirmed = find_confirmed_close_deals(
        {"00123"},
        [{"position_id": 123, "entry": 1, "ticket": 9}],
    )
    # The requested ID remains an exact string identifier: numeric broker IDs
    # are normalized, but we don't fuzzy-match distinct strings.
    assert confirmed == {}
