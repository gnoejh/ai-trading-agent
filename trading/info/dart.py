"""DART filings — the *cause* behind institutional flow.

`capture_flow.py` records that informed money is buying (기관 N일 연속순매수).
It cannot say why. Korean disclosure law means the reason is almost always a
filed document, and DART publishes those free and structured.

Joined on 종목코드 + date, a flow row becomes a hypothesis with a catalyst:

    008930  기관 30일 연속  +26.77%  <-  주식양수도계약 (control contest)

The filing *type* matters as much as its existence, because it predicts how the
flow decays. A 공급계약 fades gradually; a 최대주주변경 ends abruptly when the
contest resolves; a 전환사채권발행 is often dilutive and negative. Flow alone
cannot tell those apart, which is why holding purely on flow overstays.

DART keys on `corp_code`, an 8-digit DART identifier that is NOT the 종목코드.
The mapping arrives as a zipped XML of every registered company; it is fetched
once and cached, like the tradable universe.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx

from trading.config import AppConfig, DartSecrets, config

log = logging.getLogger(__name__)

BASE = "https://opendart.fss.or.kr/api"

# Filing types that plausibly drive sustained institutional buying, and how the
# flow behind each tends to behave. Used to tag a catalyst, never to trade on.
# (pattern, kind, sign). ORDER MATTERS -- the first match wins, so the
# meaning-inverting variants must precede their base forms.
#
# Korean filings invert on a verb suffix: 체결(conclude) vs 해지(terminate),
# 취득(acquire) vs 처분(dispose), 결정(decide) vs 철회(withdraw). An earlier
# version keyed on nouns alone and scored 자기주식취득신탁계약**해지** -- the END
# of a buyback -- as bullish. Classification reviewed by the trader model
# 2026-08-10, which flagged exactly that inversion.
CATALYSTS: list[tuple[str, str, str]] = [
    # -- inverted forms first --------------------------------------------------
    ("자기주식취득신탁계약해지", "buyback_end", "NEG"),   # buyback programme ending
    ("자기주식처분", "buyback_end", "NEG"),
    ("철회", "withdrawn", "NEG"),
    # -- supply improving ------------------------------------------------------
    ("자기주식취득", "buyback", "POS"),
    ("현금ㆍ현물배당", "dividend", "POS"),
    ("현금배당", "dividend", "POS"),
    ("단일판매", "contract", "POS"),
    ("공급계약", "contract", "POS"),
    # -- dilution --------------------------------------------------------------
    ("전환사채권발행", "dilution", "NEG"),
    ("신주인수권부사채", "dilution", "NEG"),
    ("유상증자", "dilution", "NEG"),
    # -- ownership: direction is in the BODY, not the title, so never signed ----
    ("주식등의대량보유상황보고서", "stake", "NEU"),
    ("임원ㆍ주요주주특정증권등소유상황보고서", "insider", "NEU"),
    ("최대주주변경", "control", "NEU"),
    ("주식양수도", "control", "NEU"),
    ("경영권", "control", "NEU"),
    # -- scheduled -------------------------------------------------------------
    ("매출액또는손익구조", "earnings", "NEU"),
    ("분기보고서", "earnings", "NEU"),
    ("반기보고서", "earnings", "NEU"),
    ("사업보고서", "earnings", "NEU"),
]


class DartClient:
    def __init__(self, cfg: AppConfig | None = None, secrets: DartSecrets | None = None,
                 client: httpx.Client | None = None):
        self.cfg = cfg or config()
        self.secrets = secrets or DartSecrets()
        self.dcfg = self.cfg.info.dart
        self._http = client or httpx.Client(timeout=self.dcfg.timeout_s)
        self._corp: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.secrets.api_key)

    def _get(self, path: str, **params) -> dict:
        params["crtfc_key"] = self.secrets.api_key
        r = self._http.get(f"{BASE}/{path}", params=params)
        r.raise_for_status()
        return r.json()

    # -- 종목코드 -> corp_code -------------------------------------------------

    def corp_map(self, *, force: bool = False) -> dict[str, str]:
        """Map 종목코드 -> corp_code. Cached; the file rarely changes."""
        if self._corp and not force:
            return self._corp
        cache = Path(self.dcfg.corp_cache)
        if cache.exists() and not force:
            age_h = (
                dt.datetime.now(dt.UTC)
                - dt.datetime.fromtimestamp(cache.stat().st_mtime, dt.UTC)
            ).total_seconds() / 3600
            if age_h < self.dcfg.corp_refresh_hours:
                self._corp = json.loads(cache.read_text(encoding="utf-8"))
                return self._corp

        # Returned as a ZIP containing CORPCODE.xml, not as JSON like every other
        # endpoint -- so this one cannot go through _get.
        r = self._http.get(
            f"{BASE}/corpCode.xml", params={"crtfc_key": self.secrets.api_key}
        )
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            xml = z.read(z.namelist()[0])
        out: dict[str, str] = {}
        for el in ET.fromstring(xml).iter("list"):
            stock = (el.findtext("stock_code") or "").strip()
            corp = (el.findtext("corp_code") or "").strip()
            # Most registered entities are unlisted and have no 종목코드.
            if stock and corp:
                out[stock] = corp
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out), encoding="utf-8")
        self._corp = out
        log.info("dart corp map: %d listed companies", len(out))
        return out

    # -- filings --------------------------------------------------------------

    def filings(self, symbol: str, days: int = 30) -> list[dict]:
        """Recent filings for a 종목코드, newest first."""
        corp = self.corp_map().get(symbol)
        if not corp:
            log.debug("no DART corp_code for %s", symbol)
            return []
        end = dt.datetime.now(dt.UTC).astimezone()
        start = end - dt.timedelta(days=days)
        try:
            data = self._get(
                "list.json",
                corp_code=corp,
                bgn_de=start.strftime("%Y%m%d"),
                end_de=end.strftime("%Y%m%d"),
                page_count=str(self.dcfg.page_count),
            )
        except httpx.HTTPError as exc:
            log.warning("dart filings failed for %s: %s", symbol, exc)
            return []
        # 013 is "no matching data" -- an empty result, not a failure.
        if data.get("status") not in ("000", "013"):
            log.warning("dart %s: %s %s", symbol, data.get("status"), data.get("message"))
            return []
        return data.get("list", [])

    def catalysts(self, symbol: str, days: int = 30) -> list[dict]:
        """Filings tagged with the kind of catalyst they represent."""
        out = []
        for f in self.filings(symbol, days):
            name = f.get("report_nm", "")
            hit = next(((k, sign) for pat, k, sign in CATALYSTS if pat in name), None)
            if hit:
                kind, sign = hit
                out.append(
                    {
                        "date": f.get("rcept_dt"),
                        "kind": kind,
                        "sign": sign,
                        "report": name.strip(),
                        "rcept_no": f.get("rcept_no"),
                    }
                )
        return out
