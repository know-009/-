from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from pykrx import stock


KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent
THEME_MAP_PATH = Path(os.getenv("THEME_MAP_PATH", ROOT / "theme_map.json"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
STRICT_DATA_VALIDATION = os.getenv("STRICT_DATA_VALIDATION", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

TELEGRAM_TEXT_LIMIT = 4_096
NEW_LISTING_TRADING_DAYS = 3
LOGGER = logging.getLogger("morning-stock-briefing")


@dataclass(frozen=True)
class USStockSnapshot:
    ticker: str
    trading_date: date
    close: float
    change_pct: float
    dollar_value: float
    average_dollar_value_20d: float

    @property
    def value_ratio(self) -> float:
        if self.average_dollar_value_20d <= 0:
            return 0.0
        return self.dollar_value / self.average_dollar_value_20d

    @property
    def leader_score(self) -> float:
        """Rank the latest-session leaders by momentum, activity and liquidity."""
        momentum = max(-30.0, min(60.0, self.change_pct * 4.0))
        activity = max(-15.0, min(45.0, (self.value_ratio - 1.0) * 30.0))
        liquidity = max(
            0.0,
            min(25.0, math.log10(max(self.dollar_value, 1.0) / 10_000_000) * 8.0),
        )
        return momentum + activity + liquidity


@dataclass(frozen=True)
class ThemeResult:
    name: str
    score: float
    total_dollar_value: float
    value_ratio: float
    weighted_change_pct: float
    breadth: float
    stocks: tuple[USStockSnapshot, ...]
    kr_stocks: tuple[dict[str, str], ...]


def load_theme_map() -> list[dict[str, Any]]:
    with THEME_MAP_PATH.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    themes = payload.get("themes", [])
    if len(themes) < 3:
        raise ValueError("theme_map.json에는 최소 3개 테마가 필요합니다.")
    return themes


def nearest_krx_business_day(day: date, *, previous: bool) -> date:
    """pykrx의 KRX 달력으로 가장 가까운 영업일을 찾는다."""
    value = stock.get_nearest_business_day_in_a_week(
        day.strftime("%Y%m%d"), prev=previous
    )
    if not value:
        raise RuntimeError(f"KRX 영업일을 확인하지 못했습니다: {day}")
    return datetime.strptime(value, "%Y%m%d").date()


def get_run_dates(now: datetime | None = None) -> tuple[date, date]:
    """실행일(KST)과 그보다 앞선 가장 최근 KRX 거래일을 반환한다."""
    now_kst = now.astimezone(KST) if now else datetime.now(KST)
    run_date = now_kst.date()

    # 주말에는 GitHub Actions 수동 실행 시에도 중복 브리핑을 보내지 않는다.
    if run_date.weekday() >= 5:
        raise SystemExit(f"{run_date}은 주말이므로 발송하지 않습니다.")

    candidate = run_date - timedelta(days=1)
    target_date = nearest_krx_business_day(candidate, previous=True)
    return run_date, target_date


def _extract_ticker_frame(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    if not isinstance(raw.columns, pd.MultiIndex):
        return raw.copy()

    level0 = raw.columns.get_level_values(0)
    level1 = raw.columns.get_level_values(1)
    if ticker in level0:
        return raw[ticker].copy()
    if ticker in level1:
        return raw.xs(ticker, axis=1, level=1).copy()
    return pd.DataFrame()


def fetch_us_snapshots(themes: list[dict[str, Any]]) -> dict[str, USStockSnapshot]:
    tickers = sorted({ticker for theme in themes for ticker in theme["us_tickers"]})
    raw = yf.download(
        tickers=tickers,
        period="2mo",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=True,
        timeout=30,
    )

    snapshots: dict[str, USStockSnapshot] = {}
    for ticker in tickers:
        frame = _extract_ticker_frame(raw, ticker)
        if frame.empty or not {"Close", "Volume"}.issubset(frame.columns):
            LOGGER.warning("미국 시세 누락: %s", ticker)
            continue

        frame = frame[["Close", "Volume"]].dropna()
        frame = frame[(frame["Close"] > 0) & (frame["Volume"] > 0)]
        if len(frame) < 7:
            LOGGER.warning("미국 시세 표본 부족: %s (%d rows)", ticker, len(frame))
            continue

        last = frame.iloc[-1]
        previous_close = float(frame["Close"].iloc[-2])
        baseline = frame.iloc[:-1].tail(20)
        dollar_values = baseline["Close"] * baseline["Volume"]
        last_index = pd.Timestamp(frame.index[-1])

        snapshots[ticker] = USStockSnapshot(
            ticker=ticker,
            trading_date=last_index.date(),
            close=float(last["Close"]),
            change_pct=(float(last["Close"]) / previous_close - 1) * 100,
            dollar_value=float(last["Close"] * last["Volume"]),
            average_dollar_value_20d=float(dollar_values.mean()),
        )

    return snapshots


def rank_us_themes(
    themes: list[dict[str, Any]], snapshots: dict[str, USStockSnapshot]
) -> list[ThemeResult]:
    if not snapshots:
        raise RuntimeError("미국 종목 시세를 한 건도 조회하지 못했습니다.")

    # A ticker can occasionally retain an older daily candle.  Only the newest
    # completed US session is eligible for the 07:50 briefing.
    latest_date = max(item.trading_date for item in snapshots.values())
    latest_snapshots = {
        ticker: item
        for ticker, item in snapshots.items()
        if item.trading_date == latest_date
    }
    if len(latest_snapshots) < 10:
        raise RuntimeError(
            f"미국 최신 거래일({latest_date}) 시세가 {len(latest_snapshots)}개뿐입니다."
        )

    # Find the stocks that actually led the latest session first.  Themes are
    # then ranked by how strongly those leaders cluster inside each theme.
    ordered_stocks = sorted(
        latest_snapshots.values(), key=lambda item: item.leader_score, reverse=True
    )
    rising_stocks = [item for item in ordered_stocks if item.change_pct > 0]
    leader_count = max(12, min(25, math.ceil(len(ordered_stocks) * 0.20)))
    leader_pool = rising_stocks[:leader_count]
    if len(leader_pool) < 8:
        leader_pool = ordered_stocks[:leader_count]
    leader_tickers = {item.ticker for item in leader_pool}

    ranked: list[ThemeResult] = []
    for theme in themes:
        stocks = tuple(
            latest_snapshots[ticker]
            for ticker in theme["us_tickers"]
            if ticker in latest_snapshots
        )
        required = max(2, math.ceil(len(theme["us_tickers"]) * 0.6))
        if len(stocks) < required:
            LOGGER.warning("테마 표본 부족으로 제외: %s", theme["name"])
            continue

        leaders = tuple(item for item in stocks if item.ticker in leader_tickers)
        if not leaders:
            continue

        total_value = sum(item.dollar_value for item in stocks)
        baseline_value = sum(item.average_dollar_value_20d for item in stocks)
        value_ratio = total_value / baseline_value if baseline_value else 0.0
        weighted_change = (
            sum(item.change_pct * item.dollar_value for item in stocks) / total_value
            if total_value
            else 0.0
        )
        breadth = sum(item.change_pct > 0 for item in stocks) / len(stocks)

        # The latest-session leaders drive the theme score.  Breadth prevents a
        # single isolated mover from overwhelming a broadly weak theme.
        leader_strength = sum(max(0.0, item.leader_score) for item in leaders)
        score = (
            leader_strength
            + len(leaders) * 15
            + breadth * 25
            + max(-15, min(25, weighted_change * 2.0))
        )
        ranked.append(
            ThemeResult(
                name=theme["name"],
                score=score,
                total_dollar_value=total_value,
                value_ratio=value_ratio,
                weighted_change_pct=weighted_change,
                breadth=breadth,
                stocks=tuple(
                    sorted(leaders, key=lambda item: item.leader_score, reverse=True)
                ),
                kr_stocks=tuple(theme["kr_stocks"]),
            )
        )

    ranked.sort(key=lambda item: item.score, reverse=True)
    if len(ranked) < 3:
        raise RuntimeError("유효한 미국 테마가 3개 미만입니다.")
    return ranked[:3]


def _safe_ticker_set(function_name: str, target: str) -> set[str]:
    function = getattr(stock, function_name, None)
    if function is None:
        return set()
    try:
        return set(function(target))
    except Exception as exc:  # 보조 필터 실패는 이름 필터로 보완한다.
        LOGGER.warning("%s 조회 실패: %s", function_name, exc)
        return set()


def _is_preferred_share(name: str) -> bool:
    return bool(re.search(r"(?:우|우B|우C|\d+우B?)$", name))


def get_new_listing_day(
    ticker: str,
    target_date: date,
    listing_reference: tuple[date, set[str]] | None,
) -> int | None:
    """Return 1-3 only when a ticker was absent from an older KRX universe."""
    if listing_reference is None:
        return None
    reference_date, old_tickers = listing_reference
    if not old_tickers or ticker in old_tickers:
        return None

    try:
        history = stock.get_market_ohlcv_by_date(
            reference_date.strftime("%Y%m%d"),
            target_date.strftime("%Y%m%d"),
            ticker,
        )
    except Exception as exc:
        LOGGER.warning("신규상장 거래일 판정 실패(%s): %s", ticker, exc)
        return None

    if history.empty:
        return None
    traded = history[(history["종가"] > 0) & (history["거래량"] > 0)]
    day_number = len(traded)
    return day_number if 1 <= day_number <= NEW_LISTING_TRADING_DAYS else None


def get_listing_reference(target_date: date) -> tuple[date, set[str]] | None:
    """Get an older listed universe without making the briefing depend on it."""
    try:
        reference_date = nearest_krx_business_day(
            target_date - timedelta(days=21), previous=True
        )
        tickers = set(
            stock.get_market_ticker_list(
                reference_date.strftime("%Y%m%d"), market="ALL"
            )
        )
        if not tickers:
            raise RuntimeError("과거 KRX 종목목록이 비어 있습니다.")
        return reference_date, tickers
    except Exception as exc:
        # New-listing tags are optional metadata.  Omitting them is safer than
        # failing or falsely labelling an established stock as newly listed.
        LOGGER.warning("신규상장 기준목록 조회 실패, 신규 태그 생략: %s", exc)
        return None


def fetch_kr_market_top10(target_date: date) -> list[dict[str, Any]]:
    target = target_date.strftime("%Y%m%d")
    kospi = stock.get_market_ohlcv(target, market="KOSPI").copy()
    kosdaq = stock.get_market_ohlcv(target, market="KOSDAQ").copy()
    if kospi.empty or kosdaq.empty:
        raise RuntimeError(f"{target_date} KOSPI/KOSDAQ 시세가 불완전합니다.")
    kospi["시장"] = "KOSPI"
    kosdaq["시장"] = "KOSDAQ"
    market = pd.concat([kospi, kosdaq], axis=0)

    cap = stock.get_market_cap(target, market="KOSPI")
    top3_kospi = set(cap.nlargest(3, "시가총액").index)
    etfs = _safe_ticker_set("get_etf_ticker_list", target)
    etns = _safe_ticker_set("get_etn_ticker_list", target)
    excluded_codes = top3_kospi | etfs | etns
    excluded_words = ("스팩", "SPAC", "ETN", "인버스", "레버리지")
    listing_reference = get_listing_reference(target_date)

    market = market[market["거래대금"] > 0].sort_values("거래대금", ascending=False)
    results: list[dict[str, Any]] = []
    for ticker, row in market.iterrows():
        ticker = str(ticker).zfill(6)
        name = stock.get_market_ticker_name(ticker)
        if (
            ticker in excluded_codes
            or any(word.casefold() in name.casefold() for word in excluded_words)
            or _is_preferred_share(name)
        ):
            continue

        results.append(
            {
                "ticker": ticker,
                "name": name,
                "market": row["시장"],
                "change": float(row["등락률"]),
                "amount_eok": int(float(row["거래대금"]) / 100_000_000),
                "new_listing_day": get_new_listing_day(
                    ticker, target_date, listing_reference
                ),
            }
        )
        if len(results) == 10:
            break

    if len(results) < 10:
        raise RuntimeError(f"국내 거래대금 상위 종목이 {len(results)}개만 조회됐습니다.")
    return results


def _signed(value: float) -> str:
    return f"{value:+.2f}%"


def build_messages(
    target_date: date,
    themes: list[ThemeResult],
    kr_top10: list[dict[str, Any]],
) -> list[str]:
    linked_by_code = {
        item["code"]: theme.name
        for theme in themes
        for item in theme.kr_stocks
    }
    us_dates = sorted({stock.trading_date for theme in themes for stock in theme.stocks})
    us_date_text = us_dates[-1].isoformat() if us_dates else "확인 불가"

    header = [
        f"📅 [{target_date.isoformat()}] 모닝 마켓 브리핑 (07:50)",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"1. 미국 마지막 거래일 주도주 기반 테마 TOP 3 (미국 기준 {us_date_text})",
        "주도주 선별: 당일 상승률 + 거래대금 + 20일 평균 대비 거래대금",
    ]
    for index, theme in enumerate(themes, 1):
        leaders = sorted(theme.stocks, key=lambda item: item.dollar_value, reverse=True)[:3]
        leader_text = ", ".join(
            f"{item.ticker}({_signed(item.change_pct)}, {item.value_ratio:.1f}배)"
            for item in leaders
        )
        kr_text = ", ".join(
            f"{item['name']}({item['code']})" for item in theme.kr_stocks
        )
        header.extend(
            [
                f"{index}) {theme.name}",
                f"- 미 주도주: {leader_text}",
                f"- 테마 거래대금 강도: {theme.value_ratio:.2f}배 / 가중 등락률 {_signed(theme.weighted_change_pct)} / 상승 확산 {theme.breadth * 100:.0f}%",
                f"- 국내 연계주: {kr_text}",
            ]
        )

    domestic = [
        "🇰🇷 국내 거래대금 TOP 10",
        "(KOSPI 시총 1~3위·ETF·ETN·스팩·레버리지·인버스·우선주 제외)",
    ]
    duplicates: list[str] = []
    for index, item in enumerate(kr_top10, 1):
        new_tag = (
            f"🔥 [신규 {item['new_listing_day']}일차] "
            if item["new_listing_day"]
            else ""
        )
        theme_name = linked_by_code.get(item["ticker"])
        duplicate_tag = f" ★ [중복: {theme_name}]" if theme_name else ""
        if theme_name:
            duplicates.append(f"{item['name']}({theme_name})")
        domestic.append(
            f"{index:02d}. {new_tag}{item['name']} [{item['market']}] "
            f"{_signed(item['change'])} | {item['amount_eok']:,}억{duplicate_tag}"
        )

    cross = ["📌 핵심 교차 수급"]
    if duplicates:
        cross.append("미국 테마 연계 + 국내 거래대금 상위: " + ", ".join(duplicates))
    else:
        cross.append("미국 주도 테마 국내 연계주와 국내 TOP 10의 중복 종목 없음")
    cross.append("※ 신규 태그는 상장 당일을 1일차로 계산해 첫 3거래일까지만 표시합니다.")

    messages = ["\n".join(header), "\n".join(domestic), "\n".join(cross)]
    if any(len(message) > TELEGRAM_TEXT_LIMIT for message in messages):
        raise RuntimeError("텔레그램 메시지가 4,096자 제한을 초과했습니다.")
    return messages


def send_telegram_messages(messages: list[str]) -> None:
    if DRY_RUN:
        print("\n\n".join(messages))
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 환경변수가 없습니다."
        )

    session = requests.Session()
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for message in messages:
        for attempt in range(1, 4):
            response = session.post(
                endpoint,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            if response.status_code == 200 and response.json().get("ok") is True:
                break
            if response.status_code == 429:
                retry_after = float(
                    response.json().get("parameters", {}).get("retry_after", 1.0)
                )
                time.sleep(min(retry_after, 10.0))
                continue
            if response.status_code >= 500 and attempt < 3:
                time.sleep(2 ** (attempt - 1))
                continue
            response.raise_for_status()
        else:
            raise RuntimeError("텔레그램 메시지 전송을 3회 실패했습니다.")


def send_failure_notice(error: Exception) -> None:
    if DRY_RUN or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": f"⚠️ 모닝 브리핑 생성 실패: {type(error).__name__} — {error}",
            },
            timeout=10,
        ).raise_for_status()
    except Exception:
        LOGGER.exception("실패 알림 전송도 실패했습니다.")


def main() -> None:
    run_date, target_date = get_run_dates()
    LOGGER.info("실행일(KST)=%s, 국내 기준일=%s", run_date, target_date)

    themes = load_theme_map()
    snapshots = fetch_us_snapshots(themes)
    top3_themes = rank_us_themes(themes, snapshots)
    kr_top10 = fetch_kr_market_top10(target_date)

    if STRICT_DATA_VALIDATION and len(snapshots) < 20:
        raise RuntimeError(f"미국 종목 시세가 {len(snapshots)}개뿐이라 발송을 중단합니다.")

    messages = build_messages(target_date, top3_themes, kr_top10)
    send_telegram_messages(messages)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        main()
    except SystemExit as exc:
        LOGGER.info("%s", exc)
    except Exception as exc:
        LOGGER.exception("브리핑 작업 실패")
        send_failure_notice(exc)
        raise
