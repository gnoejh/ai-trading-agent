"""Snapshot Kiwoom institutional/foreign accumulation, daily.

    uv run python scripts/capture_flow.py

`ka10131` reports CURRENT standings over a lookback window -- there is no way to
ask it what the ranking was last Tuesday. So the decile test that would validate
the signal cannot be run against history; the data has to be accumulated forward.

This writes one line per symbol per run to data/flow_kr.jsonl. After ~2 weeks
there is enough to answer the question that matters:

    do names with high 기관/외국인 연속순매수일수 outperform over the following days?

Until then the signal is a hypothesis, and this is what turns it into evidence.
Cheap enough to run daily from the scheduler: two calls, no model, no orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.agent.universe import _rows
from trading.brokers.kiwoom.client import KiwoomClient
from trading.rag.spec_parser import Market

log = logging.getLogger("capture_flow")

OUT = Path("data/flow_kr.jsonl")
# 001 코스피, 101 코스닥 -- both, because the signal may differ by market cap tier.
MARKETS = ("001", "101")


def _num(v) -> float:
    try:
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--period", default="5", help="1/3/5/10/20/120 day lookback")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = KiwoomClient(Market.KR)
    stamp = dt.datetime.now(dt.UTC).isoformat()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    written = 0

    with OUT.open("a", encoding="utf-8") as fh:
        for market in MARKETS:
            body = {
                "dt": args.period,
                "mrkt_tp": market,
                "netslmt_tp": "2",  # 순매수, the only supported value
                "stk_inds_tp": "0",  # 종목, not 업종 -- excludes indices and ETFs
                "amt_qty_tp": "0",  # 금액
                "stex_tp": "1",  # KRX
            }
            try:
                rows = _rows(client.call("ka10131", body).body)
            except Exception as exc:  # noqa: BLE001 - one market failing is survivable
                log.error("ka10131 failed for %s: %s", market, exc)
                continue
            for rank, r in enumerate(rows, start=1):
                code = str(r.get("stk_cd", "")).lstrip("A")
                if not code:
                    continue
                fh.write(
                    json.dumps(
                        {
                            "ts": stamp,
                            "period": args.period,
                            "market": market,
                            "rank": rank,
                            "symbol": code,
                            "name": r.get("stk_nm", ""),
                            # The signal under test: consecutive days of net buying
                            # by each participant class, and the move so far.
                            "orgn_days": _num(r.get("orgn_cont_netprps_dys")),
                            "orgn_amt": _num(r.get("orgn_cont_netprps_amt")),
                            "frgnr_days": _num(r.get("frgnr_cont_netprps_dys")),
                            "frgnr_amt": _num(r.get("frgnr_cont_netprps_amt")),
                            "period_chg_pct": _num(r.get("prid_stkpc_flu_rt")),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                written += 1

    print(f"captured {written} rows -> {OUT}")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
