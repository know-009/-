from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests


class KISError(RuntimeError):
    pass


class KISClient:
    """주문 기능을 포함하지 않는 KIS 국내주식 시세 전용 REST 클라이언트."""

    def __init__(self) -> None:
        self.app_key = os.environ.get("KIS_APP_KEY", "").strip()
        self.app_secret = os.environ.get("KIS_APP_SECRET", "").strip()
        self.base_url = os.environ.get(
            "KIS_BASE_URL", "https://openapi.koreainvestment.com:9443"
        ).rstrip("/")
        if not self.app_key or not self.app_secret:
            raise KISError("KIS_APP_KEY 또는 KIS_APP_SECRET이 없습니다.")

        self.session = requests.Session()
        self._token = ""
        self._token_expires_at = 0.0
        self._last_request_at = 0.0
        self._lock = threading.Lock()

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 120:
            return self._token
        response = self.session.post(
            f"{self.base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise KISError(f"KIS 접근토큰 발급 실패: {payload}")
        self._token = str(token)
        self._token_expires_at = time.time() + int(payload.get("expires_in", 86_400))
        return self._token

    def _paced_get(self, path: str, tr_id: str, params: dict[str, str]) -> Any:
        # 짧은 순간에 요청이 몰리지 않도록 보수적으로 간격을 둔다.
        with self._lock:
            wait = 0.08 - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        response = self.session.get(
            f"{self.base_url}{path}", headers=headers, params=params, timeout=20
        )
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("rt_cd", "0")) != "0":
            raise KISError(
                f"KIS {tr_id} 오류 {payload.get('msg_cd')}: {payload.get('msg1')}"
            )
        return payload.get("output", [])

    def volume_rank(self, market_code: str) -> list[dict[str, Any]]:
        output = self._paced_get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01710000",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": market_code,
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "3",
                "FID_TRGT_CLS_CODE": "111111111",
                # KIS 공식 샘플은 설명상의 10자리와 달리 6자리 값을 사용한다.
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": "0",
                "FID_INPUT_PRICE_2": "0",
                "FID_VOL_CNT": "0",
                "FID_INPUT_DATE_1": "0",
            },
        )
        return output if isinstance(output, list) else []

    def fluctuation_rank(self, market_code: str) -> list[dict[str, Any]]:
        output = self._paced_get(
            "/uapi/domestic-stock/v1/ranking/fluctuation",
            "FHPST01700000",
            {
                "fid_rsfl_rate2": "29",
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20170",
                "fid_input_iscd": market_code,
                "fid_rank_sort_cls_code": "0",
                "fid_input_cnt_1": "0",
                "fid_prc_cls_code": "0",
                "fid_input_price_1": "",
                "fid_input_price_2": "",
                "fid_vol_cnt": "",
                "fid_trgt_cls_code": "0",
                "fid_trgt_exls_cls_code": "0",
                "fid_div_cls_code": "0",
                "fid_rsfl_rate1": "1",
            },
        )
        return output if isinstance(output, list) else []

    def volume_power_rank(self, market_code: str) -> list[dict[str, Any]]:
        output = self._paced_get(
            "/uapi/domestic-stock/v1/ranking/volume-power",
            "FHPST01680000",
            {
                "fid_trgt_exls_cls_code": "0",
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20168",
                "fid_input_iscd": market_code,
                "fid_div_cls_code": "0",
                "fid_input_price_1": "0",
                "fid_input_price_2": "1000000",
                "fid_vol_cnt": "0",
                "fid_trgt_cls_code": "0",
            },
        )
        return output if isinstance(output, list) else []

    def foreign_institution_rank(self, market_code: str) -> list[dict[str, Any]]:
        output = self._paced_get(
            "/uapi/domestic-stock/v1/quotations/foreign-institution-total",
            "FHPTJ04400000",
            {
                "FID_COND_MRKT_DIV_CODE": "V",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": market_code,
                "FID_DIV_CLS_CODE": "1",
                "FID_RANK_SORT_CLS_CODE": "0",
                "FID_ETC_CLS_CODE": "0",
            },
        )
        return output if isinstance(output, list) else []

    def quote(self, ticker: str) -> dict[str, Any]:
        output = self._paced_get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        return output if isinstance(output, dict) else {}
