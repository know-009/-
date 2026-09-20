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
