from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import FinanceDataReader as fdr
import pandas as pd
import requests

from kis_client import KISClient


KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("intraday-monitor")

SCAN_INTERVAL_SECONDS = int(os.getenv("INTRADAY_SCAN_SECONDS", "30"))
CONFIRM_SCANS = int(os.getenv("INTRADAY_CONFIRM_SCANS", "3"))
ALERT_COOLDOWN_MINUTES = int(os.getenv("INTRADAY_ALERT_COOLDOWN_MINUTES", "15"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "").strip()
DART_API_KEY = os.getenv("DART_API_KEY", "").strip()

MARKETS = {"KOSPI": "0001", "KOSDAQ": "1001"}


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: int = 99) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def row_code(row: dict[str, Any]) -> str:
    value = row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or ""
    return str(value).zfill(6) if value else ""


@dataclass
class Candidate:
    ticker: str
    name: str
    market: str
    price: int = 0
    change_pct: float = 0.0
    amount_won: float = 0.0
    amount_rank: int = 99
    rise_rank: int = 99
    power_rank: int = 99
    execution_power: float = 0.0
    foreign_net_qty: float = 0.0
    institution_net_qty: float = 0.0
    program_net_qty: float = 0.0
    status_code: str = ""
    halted: bool = False
    theme: str = "미분류"
    disclosure: str = ""
    score: float = 0.0
    decision: str = "관망"

    def compact(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "theme": self.theme,
            "score": round(self.score, 2),
            "decision": self.decision,
        }


class ThemeResolver:
    def __init__(self) -> None:
        with (ROOT / "theme_map.json").open("r", encoding="utf-8") as fp:
            themes = json.load(fp)["themes"]
        self.by_code: dict[str, list[str]] = {}
        for theme in themes:
            for item in theme["kr_stocks"]:
                self.by_code.setdefault(item["code"], []).append(theme["name"])
        self.sectors: dict[str, str] = {}

    def load_sector_fallbacks(self) -> None:
        try:
            listing = fdr.StockListing("KRX")
            code_column = next(
                (c for c in ("Code", "Symbol", "Ticker") if c in listing.columns), None
            )
            sector_column = next(
                (c for c in ("Industry", "Sector", "업종") if c in listing.columns), None
            )
            if code_column and sector_column:
                self.sectors = {
                    str(code).zfill(6): str(sector)
                    for code, sector in zip(listing[code_column], listing[sector_column])
                    if pd.notna(sector) and str(sector).strip()
                }
        except Exception as exc:
            LOGGER.warning("업종 보조정보 조회 실패: %s", exc)

    def resolve(self, ticker: str) -> str:
        themes = self.by_code.get(ticker)
        if themes:
            return " / ".join(themes[:2])
        return self.sectors.get(ticker, "미분류")


class NewsHelper:
    def headline(self, company_name: str) -> str:
        if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
            return ""
        try:
            response = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                params={"query": company_name, "display": 3, "sort": "date"},
                headers={
                    "X-Naver-Client-Id": NAVER_CLIENT_ID,
                    "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
                },
                timeout=10,
            )
            response.raise_for_status()
            for item in response.json().get("items", []):
                title = re.sub(r"<[^>]+>", "", item.get("title", ""))
                title = title.replace("&quot;", '"').replace("&amp;", "&")
                if title:
                    return title[:90]
        except Exception as exc:
            LOGGER.warning("뉴스 조회 실패(%s): %s", company_name, exc)
        return ""


class DartHelper:
    """공시는 점수에 넣지 않고 후보의 당일 리스크/설명 보조값으로만 사용한다."""

    def __init__(self) -> None:
        self.corp_to_stock: dict[str, str] = {}
        self.by_stock: dict[str, str] = {}
        self.refreshed_at = 0.0
        if DART_API_KEY:
            self._load_corp_codes()

    def _load_corp_codes(self) -> None:
        try:
            response = requests.get(
                "https://opendart.fss.or.kr/api/corpCode.xml",
                params={"crtfc_key": DART_API_KEY},
                timeout=30,
            )
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                xml_name = archive.namelist()[0]
                root = ET.fromstring(archive.read(xml_name))
            for item in root.findall("list"):
                corp_code = (item.findtext("corp_code") or "").strip()
                stock_code = (item.findtext("stock_code") or "").strip()
                if corp_code and stock_code:
                    self.corp_to_stock[corp_code] = stock_code.zfill(6)
        except Exception as exc:
            LOGGER.warning("DART 기업코드 조회 실패: %s", exc)

    def refresh(self) -> None:
        if not DART_API_KEY or not self.corp_to_stock:
            return
        if time.monotonic() - self.refreshed_at < 180:
            return
        try:
            today = datetime.now(KST).strftime("%Y%m%d")
            response = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={
                    "crtfc_key": DART_API_KEY,
                    "bgn_de": today,
                    "end_de": today,
                    "page_count": "100",
                    "sort": "date",
                    "sort_mth": "desc",
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") not in ("000", "013"):
                raise RuntimeError(payload.get("message", "DART 오류"))
            disclosures: dict[str, str] = {}
            for item in payload.get("list", []):
                stock_code = self.corp_to_stock.get(str(item.get("corp_code", "")))
                report_name = str(item.get("report_nm", "")).strip()
                if stock_code and report_name and stock_code not in disclosures:
                    disclosures[stock_code] = report_name[:70]
            self.by_stock = disclosures
            self.refreshed_at = time.monotonic()
        except Exception as exc:
            LOGGER.warning("DART 당일 공시 조회 실패: %s", exc)

    def disclosure(self, ticker: str) -> str:
        return self.by_stock.get(ticker, "")


def score_candidate(candidate: Candidate) -> Candidate:
    liquidity = max(0.0, 35.0 * (31 - min(candidate.amount_rank, 31)) / 30)
    momentum_rank = max(0.0, 10.0 * (31 - min(candidate.rise_rank, 31)) / 30)
    momentum_rate = max(0.0, min(10.0, candidate.change_pct / 15 * 10))
    power_rank = max(0.0, 10.0 * (31 - min(candidate.power_rank, 31)) / 30)
    power_value = max(0.0, min(5.0, (candidate.execution_power - 100) / 20 * 5))

    flow = 0.0
    if candidate.foreign_net_qty > 0:
        flow += 10.0
    if candidate.institution_net_qty > 0:
        flow += 10.0
    if candidate.program_net_qty > 0:
        flow += 5.0

    status = 5.0 if candidate.status_code in ("", "00") and not candidate.halted else -30.0
    overheat = -20.0 if candidate.change_pct >= 18 else 0.0
    candidate.score = round(
        liquidity + momentum_rank + momentum_rate + power_rank + power_value + flow + status + overheat,
        2,
    )

    strict_buy = (
        candidate.score >= 70
        and candidate.amount_rank <= 12
        and 1.5 <= candidate.change_pct <= 12
        and candidate.execution_power >= 105
        and flow >= 10
        and status > 0
    )
    candidate.decision = "매수" if strict_buy else "관망"
    return candidate


class IntradayScanner:
    def __init__(self, client: KISClient, resolver: ThemeResolver) -> None:
        self.client = client
        self.resolver = resolver
        self.flow_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}

    def _flows(self, market: str, market_code: str) -> dict[str, dict[str, Any]]:
        cached_at, cached = self.flow_cache.get(market, (0.0, {}))
        if time.monotonic() - cached_at < 600:
            return cached
        rows = self.client.foreign_institution_rank(market_code)
        result = {row_code(row): row for row in rows if row_code(row)}
        self.flow_cache[market] = (time.monotonic(), result)
        return result

    def scan_market(self, market: str, market_code: str) -> list[Candidate]:
        amount_rows = self.client.volume_rank(market_code)
        rise_rows = self.client.fluctuation_rank(market_code)
        power_rows = self.client.volume_power_rank(market_code)
        flow_rows = self._flows(market, market_code)

        pool: dict[str, Candidate] = {}

        def ensure(row: dict[str, Any]) -> Candidate | None:
            ticker = row_code(row)
            if not ticker:
                return None
            name = str(row.get("hts_kor_isnm", "")).strip() or ticker
            if re.search(r"(?:우|우B|우C|\d+우B?|스팩)$", name):
                return None
            if ticker not in pool:
                pool[ticker] = Candidate(ticker=ticker, name=name, market=market)
            return pool[ticker]

        for position, row in enumerate(amount_rows, 1):
            item = ensure(row)
            if not item:
                continue
            item.amount_rank = integer(row.get("data_rank"), position)
            item.price = integer(row.get("stck_prpr"), 0)
            item.change_pct = number(row.get("prdy_ctrt"))
            item.amount_won = number(row.get("acml_tr_pbmn"))

        for position, row in enumerate(rise_rows, 1):
            item = ensure(row)
            if not item:
                continue
            item.rise_rank = integer(row.get("data_rank"), position)
            item.price = item.price or integer(row.get("stck_prpr"), 0)
            item.change_pct = number(row.get("prdy_ctrt"), item.change_pct)

        for position, row in enumerate(power_rows, 1):
            item = ensure(row)
            if not item:
                continue
            item.power_rank = integer(row.get("data_rank"), position)
            item.execution_power = number(row.get("tday_rltv"))
            item.price = item.price or integer(row.get("stck_prpr"), 0)
            item.change_pct = number(row.get("prdy_ctrt"), item.change_pct)

        # 순위 교집합을 우선한 예비점수로 상위 12개만 상세 시세를 확인한다.
        pre_ranked = sorted(
            pool.values(),
            key=lambda item: (
                (31 - min(item.amount_rank, 31)) * 2
                + (31 - min(item.rise_rank, 31))
                + (31 - min(item.power_rank, 31))
            ),
            reverse=True,
        )[:12]

        for item in pre_ranked:
            quote = self.client.quote(item.ticker)
            item.price = integer(quote.get("stck_prpr"), item.price)
            item.change_pct = number(quote.get("prdy_ctrt"), item.change_pct)
            item.amount_won = number(quote.get("acml_tr_pbmn"), item.amount_won)
            item.foreign_net_qty = number(quote.get("frgn_ntby_qty"))
            item.program_net_qty = number(quote.get("pgtr_ntby_qty"))
            item.status_code = str(quote.get("iscd_stat_cls_code", ""))
            item.halted = str(quote.get("temp_stop_yn", "N")) == "Y"

            flow = flow_rows.get(item.ticker, {})
            item.foreign_net_qty = number(
                flow.get("frgn_ntby_qty"), item.foreign_net_qty
            )
            item.institution_net_qty = number(flow.get("orgn_ntby_qty"))
            item.theme = self.resolver.resolve(item.ticker)
            score_candidate(item)

        return sorted(pre_ranked, key=lambda item: item.score, reverse=True)


def leading_theme(candidates: list[Candidate]) -> str:
    scores: dict[str, float] = {}
    for candidate in candidates[:5]:
        if candidate.theme == "미분류":
            continue
        for theme in candidate.theme.split(" / "):
            scores[theme] = scores.get(theme, 0.0) + candidate.score
    return max(scores, key=scores.get) if scores else "미분류"


def signature(candidates: list[Candidate]) -> str:
    leaders = ",".join(item.ticker for item in candidates[:2])
    return f"{leaders}|{leading_theme(candidates)}"


def change_reason(previous: dict[str, Any], current: list[Candidate]) -> list[str]:
    old_leaders = previous.get("leaders", [])
    new_leaders = [item.compact() for item in current[:2]]
    old_theme = previous.get("theme", "미분류")
    new_theme = leading_theme(current)
    reasons: list[str] = []
    if old_leaders and old_leaders[0]["ticker"] != new_leaders[0]["ticker"]:
        reasons.append(f"1순위 {old_leaders[0]['name']} → {new_leaders[0]['name']}")
    if len(old_leaders) > 1 and old_leaders[1]["ticker"] != new_leaders[1]["ticker"]:
        reasons.append(f"2순위 {old_leaders[1]['name']} → {new_leaders[1]['name']}")
    if old_theme != new_theme:
        reasons.append(f"주도 테마 {old_theme} → {new_theme}")
    return reasons


def is_meaningful(previous: dict[str, Any], current: list[Candidate]) -> bool:
    if len(current) < 2 or not previous.get("leaders"):
        return False
    old = previous["leaders"]
    top1_changed = old[0]["ticker"] != current[0].ticker
    top2_changed = len(old) > 1 and old[1]["ticker"] != current[1].ticker
    theme_changed = previous.get("theme") != leading_theme(current)
    return (
        top1_changed
        or (top2_changed and current[1].score >= 65)
        or (theme_changed and current[0].score >= 65)
    )


def load_state(path: Path) -> dict[str, Any]:
    today = datetime.now(KST).date().isoformat()
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("date") == today:
                return state
        except (OSError, json.JSONDecodeError):
            pass
    return {"date": today, "markets": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def update_market_state(
    market_state: dict[str, Any], candidates: list[Candidate], now: datetime
) -> tuple[bool, list[str]]:
    current_signature = signature(candidates)
    if not market_state.get("leaders"):
        market_state.update(
            {
                "leaders": [item.compact() for item in candidates[:2]],
                "theme": leading_theme(candidates),
                "pending_signature": "",
                "pending_count": 0,
                "last_alert_at": "",
            }
        )
        return False, []

    # A meaningful leading-theme change is time-sensitive: alert on the first
    # observed scan and do not apply the general cooldown. Updating the stored
    # theme here prevents duplicates while it remains unchanged, but permits
    # A -> B -> A transitions to be reported separately.
    theme_changed = market_state.get("theme") != leading_theme(candidates)
    meaningful_theme_change = theme_changed and candidates[0].score >= 65
    if meaningful_theme_change:
        reasons = change_reason(market_state, candidates)
        market_state.update(
            {
                "leaders": [item.compact() for item in candidates[:2]],
                "theme": leading_theme(candidates),
                "pending_signature": "",
                "pending_count": 0,
                "last_alert_at": now.isoformat(),
            }
        )
        return True, reasons

    if market_state.get("pending_signature") == current_signature:
        market_state["pending_count"] = int(market_state.get("pending_count", 0)) + 1
    else:
        market_state["pending_signature"] = current_signature
        market_state["pending_count"] = 1

    if market_state["pending_count"] < CONFIRM_SCANS:
        return False, []
    if not is_meaningful(market_state, candidates):
        return False, []

    top1_changed = market_state["leaders"][0]["ticker"] != candidates[0].ticker
    last_text = market_state.get("last_alert_at", "")
    if last_text and not top1_changed:
        last_alert = datetime.fromisoformat(last_text)
        elapsed = (now - last_alert).total_seconds() / 60
        if elapsed < ALERT_COOLDOWN_MINUTES:
            return False, []

    reasons = change_reason(market_state, candidates)
    market_state.update(
        {
            "leaders": [item.compact() for item in candidates[:2]],
            "theme": leading_theme(candidates),
            "pending_signature": "",
            "pending_count": 0,
            "last_alert_at": now.isoformat(),
        }
    )
    return True, reasons


def format_candidate(index: int, item: Candidate) -> str:
    flow_bits = []
    if item.foreign_net_qty > 0:
        flow_bits.append("외국인+")
    if item.institution_net_qty > 0:
        flow_bits.append("기관+")
    if item.program_net_qty > 0:
        flow_bits.append("프로그램+")
    flow = "/".join(flow_bits) if flow_bits else "수급확인 필요"
    disclosure = f"\n공시: {item.disclosure}" if item.disclosure else ""
    return (
        f"{index}순위 | {item.name}({item.ticker}) | {item.price:,}원 "
        f"({item.change_pct:+.2f}%)\n"
        f"테마: {item.theme} | 판단: {item.decision} | 점수 {item.score:.1f}\n"
        f"근거: 거래대금 {item.amount_rank}위·체결강도 {item.execution_power:.1f}·{flow}"
        f"{disclosure}"
    )


def build_alert(
    snapshots: dict[str, list[Candidate]], changes: dict[str, list[str]], news: NewsHelper
) -> str:
    now = datetime.now(KST)
    lines = [f"🔔 장중 주도 흐름 변화 {now:%H:%M:%S}"]
    for market in ("KOSPI", "KOSDAQ"):
        lines.extend(["", f"[{market}]"])
        for index, item in enumerate(snapshots[market][:2], 1):
            lines.append(format_candidate(index, item))
        reason = "; ".join(changes.get(market, [])) or "확정 기준상 변화 없음"
        lines.append(f"변화: {reason}")

    changed_leaders = [
        snapshots[market][0]
        for market in changes
        if snapshots.get(market)
    ]
    headlines = []
    for leader in changed_leaders[:2]:
        headline = news.headline(leader.name)
        if headline:
            headlines.append(f"- {leader.name}: {headline}")
    if headlines:
        lines.extend(["", "보조 뉴스", *headlines])
    lines.extend(
        [
            "",
            "※ 매수는 엄격한 수급·유동성·과열 회피 규칙을 모두 통과한 후보이며 자동주문이 아닙니다.",
        ]
    )
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("텔레그램 환경변수가 없습니다.")
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(1, 4):
        response = requests.post(
            endpoint,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=20,
        )
        if response.status_code == 200 and response.json().get("ok"):
            return
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
    raise RuntimeError("텔레그램 전송을 3회 실패했습니다.")


def run(until: clock_time, state_path: Path, dry_run: bool) -> None:
    now = datetime.now(KST)
    if now.weekday() >= 5:
        LOGGER.info("주말이므로 종료합니다.")
        return
    end_at = now.replace(hour=until.hour, minute=until.minute, second=0, microsecond=0)
    if now >= end_at:
        LOGGER.info("종료 시각이 이미 지났습니다.")
        return

    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now < market_open:
        time.sleep((market_open - now).total_seconds())

    resolver = ThemeResolver()
    resolver.load_sector_fallbacks()
    scanner = IntradayScanner(KISClient(), resolver)
    news = NewsHelper()
    dart = DartHelper()
    state = load_state(state_path)

    consecutive_errors = 0
    while datetime.now(KST) < end_at:
        loop_started = time.monotonic()
        try:
            snapshots = {
                market: scanner.scan_market(market, code)
                for market, code in MARKETS.items()
            }
            if any(len(items) < 2 for items in snapshots.values()):
                raise RuntimeError("시장별 후보가 2개 미만입니다.")

            dart.refresh()
            for candidates in snapshots.values():
                for candidate in candidates[:2]:
                    candidate.disclosure = dart.disclosure(candidate.ticker)

            now = datetime.now(KST)
            changes: dict[str, list[str]] = {}
            for market, candidates in snapshots.items():
                market_state = state["markets"].setdefault(market, {})
                changed, reasons = update_market_state(market_state, candidates, now)
                if changed:
                    changes[market] = reasons
            save_state(state_path, state)

            if changes:
                message = build_alert(snapshots, changes, news)
                if dry_run:
                    print(message, flush=True)
                else:
                    send_telegram(message)
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            LOGGER.exception("장중 스캔 실패 (%d회 연속)", consecutive_errors)
            if consecutive_errors >= 5:
                raise

        elapsed = time.monotonic() - loop_started
        time.sleep(max(1.0, SCAN_INTERVAL_SECONDS - elapsed))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--until", required=True, help="KST 종료 시각(HH:MM)")
    parser.add_argument("--state", default="runtime/intraday_state.json")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    arguments = parse_args()
    until_time = datetime.strptime(arguments.until, "%H:%M").time()
    try:
        run(until_time, Path(arguments.state), arguments.dry_run)
    except Exception as exc:
        LOGGER.exception("장중 감시기 종료")
        if not arguments.dry_run and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            try:
                send_telegram(f"⚠️ 장중 감시기 중단: {type(exc).__name__} — {exc}")
            except Exception:
                LOGGER.exception("장중 감시 실패 알림 전송도 실패")
        raise
