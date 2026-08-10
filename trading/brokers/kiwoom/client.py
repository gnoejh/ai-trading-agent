"""Spec-driven Kiwoom REST client.

Every Kiwoom REST call is the same shape -- `POST {domain}{url}` with the API id
in the `api-id` header, a JSON body, and `cont-yn`/`next-key` for continuation.
So one client covers all ~300 endpoints, taking the URL, the required fields and
the field lengths from the parsed spec rather than from hand-written wrappers.

Two guardrails are structural, not optional:

* A client is bound to one :class:`Market`. KR and US are separate surfaces, and
  a mismatched id raises rather than silently hitting the wrong tree.
* Order placement is refused unless the caller passes ``allow_orders=True``. That
  is a master switch, not a risk gate -- position and loss limits belong in front
  of this layer.

Tunables come from `config.yaml`; nothing here is a magic number.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Self
from zoneinfo import ZoneInfo

import httpx

from trading.config import AppConfig, KiwoomSecrets, config
from trading.rag.spec_parser import Market
from trading.rag.spec_store import SpecStore

log = logging.getLogger(__name__)

TOKEN_ISSUE = "au10001"
TOKEN_REVOKE = "au10002"


class KiwoomError(RuntimeError):
    """Non-zero `return_code` in a Kiwoom response body."""

    def __init__(self, api_id: str, code: str, message: str):
        super().__init__(f"{api_id}: [{code}] {message}")
        self.api_id, self.code, self.message = api_id, code, message


@dataclass(slots=True)
class Page:
    """One response page. `next_key` is set when more data follows."""

    body: dict
    next_key: str | None = None

    @property
    def has_more(self) -> bool:
        return bool(self.next_key)


class KiwoomClient:
    def __init__(
        self,
        market: Market,
        *,
        store: SpecStore | None = None,
        cfg: AppConfig | None = None,
        secrets: KiwoomSecrets | None = None,
        allow_orders: bool | None = None,
        client: httpx.Client | None = None,
    ):
        if market not in (Market.KR, Market.US):
            raise ValueError(f"market must be KR or US, got {market}")
        self.cfg = cfg or config()
        self.kcfg = self.cfg.broker.kiwoom
        self.market = market
        self.market_cfg = self.kcfg.market(market)
        self.store = store or SpecStore.load(self.cfg.specs.index)
        self.secrets = secrets or KiwoomSecrets()
        # Explicit argument overrides config, so a caller can enable orders for one
        # client without loosening the global default.
        self.allow_orders = self.kcfg.allow_orders if allow_orders is None else allow_orders
        self._http = client or httpx.Client(timeout=self.kcfg.timeout_s)
        self._tz = ZoneInfo(self.kcfg.timezone)
        self._token: str | None = None
        self._token_expires: dt.datetime | None = None
        self._last_call = 0.0

    # -- auth -----------------------------------------------------------------

    def _issue_token(self) -> None:
        spec = self.store.get(TOKEN_ISSUE)
        app_key, secret_key = self.secrets.credentials(testnet=self.kcfg.use_testnet)
        if not app_key or not secret_key:
            mode = "testnet" if self.kcfg.use_testnet else "mainnet"
            raise RuntimeError(f"no Kiwoom {mode} credentials in .env")

        r = self._http.post(
            spec.domain(testnet=self.kcfg.use_testnet) + spec.url,
            json={"grant_type": "client_credentials", "appkey": app_key, "secretkey": secret_key},
            headers={"Content-Type": "application/json;charset=UTF-8", "api-id": TOKEN_ISSUE},
        )
        r.raise_for_status()
        data = r.json()
        # Auth failures come back as HTTP 200 with a non-zero return_code, so the
        # status check above does not catch them. Report the broker's own message
        # rather than dying on a missing `token` key.
        code = str(data.get("return_code", "0"))
        if code not in ("0", "None") or "token" not in data:
            mode = "testnet" if self.kcfg.use_testnet else "mainnet"
            raise KiwoomError(
                TOKEN_ISSUE, code, f"{data.get('return_msg', 'no token in response')} ({mode})"
            )
        self._token = data["token"]
        # `expires_dt` is bare `YYYYMMDDHHMMSS` in the broker's timezone. Attaching
        # it explicitly keeps refresh correct on a host that is not in that zone.
        self._token_expires = dt.datetime.strptime(data["expires_dt"], "%Y%m%d%H%M%S").replace(
            tzinfo=self._tz
        )
        log.info("kiwoom token issued, expires %s", self._token_expires)

    def token(self) -> str:
        skew = dt.timedelta(minutes=self.kcfg.token_refresh_skew_min)
        if (
            self._token is None
            or self._token_expires is None
            or dt.datetime.now(self._tz) >= self._token_expires - skew
        ):
            self._issue_token()
        assert self._token is not None
        return self._token

    # -- calls ----------------------------------------------------------------

    def is_order(self, api_id: str) -> bool:
        return self.store.get(api_id).url.startswith(tuple(self.kcfg.order_paths))

    def _throttle(self) -> None:
        """Space out calls. Kiwoom 429s when quoting many symbols back to back."""
        gap = self.kcfg.min_call_interval_s
        if gap <= 0:
            return
        wait = gap - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def _request(self, url: str, body: dict, headers: dict, api_id: str) -> httpx.Response:
        """POST with throttling and bounded retry on 429."""
        for attempt in range(self.kcfg.max_retries_429 + 1):
            self._throttle()
            r = self._http.post(url, json=body, headers=headers)
            self._last_call = time.monotonic()
            if r.status_code != 429:
                r.raise_for_status()
                return r
            if attempt == self.kcfg.max_retries_429:
                break
            delay = self.kcfg.retry_backoff_s * (2**attempt)
            log.warning("%s: 429, retrying in %.1fs (attempt %d)", api_id, delay, attempt + 1)
            time.sleep(delay)
        r.raise_for_status()
        return r

    def call(self, api_id: str, body: dict | None = None, *, next_key: str | None = None) -> Page:
        """Validate `body` against the spec, then issue one request."""
        spec = self.store.get(api_id)
        body = body or {}

        if spec.is_websocket:
            raise ValueError(f"{api_id} is a realtime subscription; use the websocket client")
        if spec.market is not self.market:
            raise ValueError(
                f"{api_id} belongs to market {spec.market}, this client is bound to {self.market}"
            )
        if self.is_order(api_id) and not self.allow_orders:
            raise PermissionError(
                f"{api_id} places or amends an order; enable broker.kiwoom.allow_orders or pass "
                "allow_orders=True, and route it through the risk gate"
            )
        self.store.validate_body(api_id, body)

        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.token()}",
            "api-id": api_id,
        }
        if next_key:
            headers["cont-yn"] = "Y"
            headers["next-key"] = next_key

        url = spec.domain(testnet=self.kcfg.use_testnet) + spec.url
        r = self._request(url, body, headers, api_id)
        data = r.json()

        code = str(data.get("return_code", "0"))
        if code not in ("0", "None"):
            # Some account queries signal "no rows" with a non-zero return_code
            # rather than an empty list -- e.g. ust21050 returns 20 with
            # "해당 계좌의 미체결내역이 없습니다" when there are simply no open
            # orders. That is an empty result, and raising on it aborts a whole
            # cycle over a perfectly normal state.
            #
            # This tolerance is scoped to reads. A non-zero code on an ORDER
            # endpoint is never reinterpreted: an order that failed must never be
            # read as one that succeeded.
            if code in self.kcfg.empty_result_codes and not self.is_order(api_id):
                log.info("%s: empty result (code %s: %s)", api_id, code, data.get("return_msg", ""))
                return Page(body=data)
            raise KiwoomError(api_id, code, data.get("return_msg", ""))

        more = r.headers.get("cont-yn", "N").upper() == "Y"
        return Page(body=data, next_key=r.headers.get("next-key") if more else None)

    def paginate(self, api_id: str, body: dict | None = None, *, max_pages: int | None = None):
        """Yield pages, following `cont-yn`/`next-key` until exhausted."""
        limit = max_pages or self.kcfg.max_pages
        next_key = None
        for _ in range(limit):
            page = self.call(api_id, body, next_key=next_key)
            yield page
            if not page.has_more:
                return
            next_key = page.next_key
        log.warning("%s: stopped at max_pages=%d with more data available", api_id, limit)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
