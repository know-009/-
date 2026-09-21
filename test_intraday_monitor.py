from datetime import datetime
from zoneinfo import ZoneInfo

import intraday_monitor as monitor


def candidate(ticker: str, name: str, score: float, theme: str = "반도체"):
    return monitor.Candidate(
        ticker=ticker,
        name=name,
        market="KOSPI",
        price=10_000,
        change_pct=5.0,
        amount_rank=3,
        rise_rank=5,
        power_rank=5,
        execution_power=120.0,
        theme=theme,
        score=score,
        decision="매수",
    )


def test_score_requires_flow_for_buy():
    item = candidate("000001", "테스트", 0)
    monitor.score_candidate(item)
    assert item.decision == "관망"

    item.foreign_net_qty = 100
    monitor.score_candidate(item)
    assert item.decision == "매수"


def test_top1_change_is_meaningful():
    previous = {
        "leaders": [
            {"ticker": "000001", "name": "이전1"},
            {"ticker": "000002", "name": "이전2"},
        ],
        "theme": "반도체",
    }
    current = [candidate("000003", "신규1", 80), candidate("000002", "이전2", 70)]
    assert monitor.is_meaningful(previous, current)


def test_change_must_persist_for_confirmation(monkeypatch):
    monkeypatch.setattr(monitor, "CONFIRM_SCANS", 3)
    state = {
        "leaders": [
            {"ticker": "000001", "name": "이전1"},
            {"ticker": "000002", "name": "이전2"},
        ],
        "theme": "반도체",
        "pending_signature": "",
        "pending_count": 0,
        "last_alert_at": "",
    }
    current = [candidate("000003", "신규1", 80), candidate("000002", "이전2", 70)]
    now = datetime(2026, 9, 21, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert monitor.update_market_state(state, current, now)[0] is False
    assert monitor.update_market_state(state, current, now)[0] is False
    assert monitor.update_market_state(state, current, now)[0] is True


def test_meaningful_theme_change_alerts_immediately_despite_cooldown(monkeypatch):
    monkeypatch.setattr(monitor, "CONFIRM_SCANS", 3)
    state = {
        "leaders": [
            {"ticker": "000001", "name": "기존1"},
            {"ticker": "000002", "name": "기존2"},
        ],
        "theme": "반도체",
        "pending_signature": "",
        "pending_count": 0,
        "last_alert_at": "2026-09-21T09:59:00+09:00",
    }
    current = [
        candidate("000001", "기존1", 80, theme="로봇"),
        candidate("000002", "기존2", 70, theme="로봇"),
    ]
    now = datetime(2026, 9, 21, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    alerted, reasons = monitor.update_market_state(state, current, now)

    assert alerted is True
    assert reasons == ["주도 테마 반도체 → 로봇"]
    assert state["theme"] == "로봇"


def test_unchanged_theme_is_not_repeated():
    state = {
        "leaders": [
            {"ticker": "000001", "name": "기존1"},
            {"ticker": "000002", "name": "기존2"},
        ],
        "theme": "로봇",
        "pending_signature": "",
        "pending_count": 0,
        "last_alert_at": "2026-09-21T10:00:00+09:00",
    }
    current = [
        candidate("000001", "기존1", 80, theme="로봇"),
        candidate("000002", "기존2", 70, theme="로봇"),
    ]
    now = datetime(2026, 9, 21, 10, 0, 30, tzinfo=ZoneInfo("Asia/Seoul"))

    assert monitor.update_market_state(state, current, now)[0] is False


def test_intraday_candidate_has_toss_stock_link():
    message = monitor.format_candidate(1, candidate("000660", "SK하이닉스", 80))
    assert "https://www.tossinvest.com/stocks/A000660/order" in message
    assert "SK하이닉스(000660)" in message


def amount_candidate(ticker: str, name: str, rank: int):
    return monitor.Candidate(
        ticker=ticker,
        name=name,
        market="KOSPI",
        price=75_000,
        change_pct=4.25,
        amount_won=1_234_000_000_000,
        amount_rank=rank,
        theme="반도체",
    )


def test_amount_rank_first_scan_only_sets_baseline():
    state = {}
    current = [amount_candidate("000001", "기준종목", 1)]

    assert monitor.update_amount_rank_state("KOSPI", state, current) == []
    assert state["amount_top10"][0]["rank"] == 1


def test_amount_rank_rise_10_to_7_has_three_arrows():
    state = {
        "amount_top10": [
            amount_candidate("000001", "상승종목", 10).amount_rank_compact()
        ]
    }
    current = [amount_candidate("000001", "상승종목", 7)]

    changes = monitor.update_amount_rank_state("KOSPI", state, current)
    message = monitor.build_amount_rank_alert(
        {"KOSPI": changes},
        datetime(2026, 9, 21, 9, 10, 30, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert "🔺🔺🔺" in message
    assert "순위상승 <b>10 → 7</b> (3계단)" in message
    assert "https://www.tossinvest.com/stocks/A000001/order" in message


def test_amount_rank_detects_new_entry_and_exit():
    state = {
        "amount_top10": [
            amount_candidate("000001", "이탈종목", 10).amount_rank_compact()
        ]
    }
    current = [amount_candidate("000002", "신규종목", 10)]

    changes = monitor.update_amount_rank_state("KOSPI", state, current)
    message = monitor.build_amount_rank_alert({"KOSPI": changes})

    assert "🆕" in message
    assert "신규진입 → <b>10위</b>" in message
    assert "TOP 10 이탈" in message


def test_amount_rank_unchanged_does_not_alert():
    original = amount_candidate("000001", "동일종목", 5)
    state = {"amount_top10": [original.amount_rank_compact()]}

    assert monitor.update_amount_rank_state("KOSPI", state, [original]) == []
