from datetime import date

import main


def test_preferred_share_filter():
    assert main._is_preferred_share("삼성전자우")
    assert main._is_preferred_share("현대차2우B")
    assert not main._is_preferred_share("삼성전자")
    assert not main._is_preferred_share("우진")


def test_new_listing_only_first_three_trading_days(monkeypatch):
    history = main.pd.DataFrame(
        {"종가": [1000, 1100, 1200], "거래량": [10, 20, 30]},
        index=main.pd.to_datetime(["2026-09-16", "2026-09-17", "2026-09-18"]),
    )
    monkeypatch.setattr(main.stock, "get_market_ohlcv_by_date", lambda *args: history)

    day_number = main.get_new_listing_day(
        "123456", date(2026, 9, 18), {"123456": date(2026, 9, 16)}
    )
    assert day_number == 3

    four_days = main.pd.concat([history, history.iloc[[0]]])
    monkeypatch.setattr(main.stock, "get_market_ohlcv_by_date", lambda *args: four_days)
    assert (
        main.get_new_listing_day(
            "123456", date(2026, 9, 21), {"123456": date(2026, 9, 16)}
        )
        is None
    )
