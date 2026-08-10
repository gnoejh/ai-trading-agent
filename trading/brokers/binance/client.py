"""Binance Spot client — crypto and bStocks.

Deliberately shaped like :class:`~trading.brokers.kiwoom.client.KiwoomClient`:
``call(endpoint_name, params) -> Page``. That is what lets `Universe`, `Screen`,
`AccountState` and the risk gate work against either venue without a second
implementation of each. The endpoint registry lives in `config.yaml`, so adding a
Binance call is a config edit exactly as it is for Kiwoom.

Two venues share this connection and one balance:

* **crypto** — USDT-quoted coins.
* **bStocks** — tokenized US equities, identified by the ``TRD_GRP_261``
  permission tag rather than by a symbol suffix. A suffix rule would sweep in
  BNB, SHIB, ARB and CKB, which are ordinary coins.

Binance differs from Kiwoom in ways that matter to the rest of the system:

* **Fees.** 0.1% maker/taker per side and no transaction tax, so a round trip is
  ~0.2% against KR's ~0.28%. Fees are therefore per-market in `accounting`.
* **No session.** Crypto trades continuously; there is no open or close to gate on.
* **Responses are often bare lists**, where Kiwoom always returns an object. They
  are normalised to ``{"rows": [...]}`` so the shared row extraction works.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

import httpx

from trading.config import AppConfig, BinanceSecrets, config

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Page:
    """Mirrors the Kiwoom client's Page so callers stay venue-agnostic."""

    body: dict
    next_key: str | None = None

    @property
    def has_more(self) -> bool:
        return False


class BinanceError(RuntimeError):
    def __init__(self, endpoint: str, code: int | str, message: str):
        super().__init__(f"{endpoint}: [{code}] {message}")
        self.endpoint, self.code, self.message = endpoint, code, message


class BinanceClient:
    def __init__(
        self,
        market: str,
        *,
        cfg: AppConfig | None = None,
        secrets: BinanceSecrets | None = None,
        allow_orders: bool | None = None,
        client: httpx.Client | None = None,
    ):
        self.cfg = cfg or config()
        self.bcfg = self.cfg.broker.binance
        self.market = market
        self.market_cfg = self.bcfg.market(market)
        self.secrets = secrets or BinanceSecrets()
        self.allow_orders = self.bcfg.allow_orders if allow_orders is None else allow_orders
        self.base_url = self.secrets.base_url(testnet=self.bcfg.use_testnet)
        self._http = client or httpx.Client(timeout=self.bcfg.timeout_s)
        self._last_call = 0.0
        # Binance rejects a request whose timestamp drifts outside recvWindow, and
        # a Windows clock can sit seconds off. Measured once against server time.
        self._time_offset_ms = 0

    # -- endpoints ------------------------------------------------------------

    def endpoint(self, name: str):
        return self.bcfg.endpoint(name)

    def is_order(self, name: str) -> bool:
        return self.endpoint(name).order

    # -- signing --------------------------------------------------------------

    def sync_time(self) -> int:
        """Measure clock drift against the exchange. Cheap, unsigned."""
        r = self._http.get(f"{self.base_url}/api/v3/time")
        r.raise_for_status()
        server = int(r.json()["serverTime"])
        self._time_offset_ms = server - int(time.time() * 1000)
        if abs(self._time_offset_ms) > 1000:
            log.warning("clock drift vs Binance: %+d ms (corrected)", self._time_offset_ms)
        return self._time_offset_ms

    def _sign(self, params: dict) -> dict:
        key, secret = self.secrets.credentials(testnet=self.bcfg.use_testnet)
        if not key or not secret:
            mode = "testnet" if self.bcfg.use_testnet else "mainnet"
            raise RuntimeError(f"no Binance {mode} credentials in .env")
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        params.setdefault("recvWindow", self.bcfg.recv_window_ms)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        params["signature"] = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return params

    def _throttle(self) -> None:
        gap = self.bcfg.min_call_interval_s
        if gap <= 0:
            return
        wait = gap - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    # -- calls ----------------------------------------------------------------

    def call(self, name: str, params: dict | None = None) -> Page:
        ep = self.endpoint(name)
        params = dict(params or {})

        if ep.order and not self.allow_orders:
            raise PermissionError(
                f"{name} places or cancels an order; enable broker.binance.allow_orders "
                "and route it through the risk gate"
            )

        key, _ = self.secrets.credentials(testnet=self.bcfg.use_testnet)
        headers = {"X-MBX-APIKEY": key} if ep.signed else {}
        if ep.signed:
            if self._time_offset_ms == 0:
                self.sync_time()
            params = self._sign(params)

        url = self.base_url + ep.path
        self._throttle()
        r = self._http.request(ep.method, url, params=params, headers=headers)
        self._last_call = time.monotonic()

        if r.status_code == 429 or r.status_code == 418:
            # Binance states how long to wait; respecting it avoids an IP ban.
            retry_after = int(r.headers.get("Retry-After", self.bcfg.retry_backoff_s))
            log.warning("%s: rate limited, sleeping %ss", name, retry_after)
            time.sleep(retry_after)
            self._throttle()
            r = self._http.request(ep.method, url, params=params, headers=headers)

        if r.status_code >= 400:
            try:
                err = r.json()
                raise BinanceError(
                    name, err.get("code", r.status_code), err.get("msg", r.text[:200])
                )
            except ValueError:
                raise BinanceError(name, r.status_code, r.text[:200]) from None

        data = r.json()
        # Kiwoom always answers with an object; Binance often answers with a bare
        # list. Normalising here keeps the shared row extraction venue-agnostic.
        if isinstance(data, list):
            data = {"rows": data}
        return Page(body=data)

    def price(self, symbol: str) -> float:
        body = self.call("price", {"symbol": symbol}).body
        return float(body.get("price", 0) or 0)

    def close(self) -> None:
        self._http.close()
