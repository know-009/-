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
        "123456",
        date(2026, 9, 18),
        (date(2026, 8, 28), {"005930", "000660"}),
    )
    assert day_number == 3

    four_days = main.pd.concat([history, history.iloc[[0]]])
    monkeypatch.setattr(main.stock, "get_market_ohlcv_by_date", lambda *args: four_days)
    assert (
        main.get_new_listing_day(
            "123456",
            date(2026, 9, 21),
            (date(2026, 8, 28), {"005930", "000660"}),
        )
        is None
    )


def test_existing_stock_never_gets_new_listing_tag(monkeypatch):
    def unexpected_call(*args, **kwargs):
        raise AssertionError("기존 종목은 가격 이력을 조회하면 안 됩니다.")

    monkeypatch.setattr(main.stock, "get_market_ohlcv_by_date", unexpected_call)
    assert (
        main.get_new_listing_day(
            "005930",
            date(2026, 9, 18),
            (date(2026, 8, 28), {"005930", "000660"}),
        )
        is None
    )


def test_listing_reference_failure_only_omits_tag(monkeypatch):
    monkeypatch.setattr(
        main,
        "nearest_krx_business_day",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("조회 실패")),
    )
    assert main.get_listing_reference(date(2026, 9, 18)) is None


def snapshot(
    ticker: str,
    change: float,
    ratio: float,
    trading_date: date = date(2026, 9, 18),
) -> main.USStockSnapshot:
    average = 100_000_000.0
    return main.USStockSnapshot(
        ticker=ticker,
        trading_date=trading_date,
        close=100.0,
        change_pct=change,
        dollar_value=average * ratio,
        average_dollar_value_20d=average,
    )


def test_us_themes_are_derived_from_latest_session_leaders():
    themes = [
        {
            "name": f"테마{index}",
            "us_tickers": [f"T{index}A", f"T{index}B"],
            "kr_stocks": [{"code": f"00000{index}", "name": f"국내{index}"}],
        }
        for index in range(1, 7)
    ]
    snapshots = {}
    for index in range(1, 7):
        snapshots[f"T{index}A"] = snapshot(
            f"T{index}A", change=8 - index, ratio=2.5 - index * 0.1
        )
        snapshots[f"T{index}B"] = snapshot(
            f"T{index}B", change=7 - index, ratio=2.2 - index * 0.1
        )

    snapshots["T6B"] = snapshot(
        "T6B", change=30, ratio=10, trading_date=date(2026, 9, 17)
    )

    ranked = main.rank_us_themes(themes, snapshots)

    assert [item.name for item in ranked] == ["테마1", "테마2", "테마3"]
    assert all(
        stock.trading_date == date(2026, 9, 18)
        for theme in ranked
        for stock in theme.stocks
    )


def test_morning_theme_message_uses_readable_html_sections():
    leader = snapshot("LEAD", change=12.34, ratio=2.5)
    theme = main.ThemeResult(
        name="AI & 반도체",
        score=100,
        total_dollar_value=250_000_000,
        value_ratio=2.5,
        weighted_change_pct=8.75,
        breadth=1.0,
        stocks=(leader,),
        kr_stocks=({"code": "000001", "name": "국내 & 종목"},),
    )

    message = main.build_messages(date(2026, 9, 18), [theme], [])[0]

    assert "🇺🇸 <b>미국 마지막 거래일 주도 테마 TOP 3</b>" in message
    assert "🥇 <b>AI &amp; 반도체</b>" in message
    assert "<b>미국 주도주</b>" in message
    assert "<b>테마 흐름</b>" in message
    assert "국내 &amp; 종목" in message
