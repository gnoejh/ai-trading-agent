"""The mainnet gate: measured criteria for leaving the testnet.

    uv run python -m trading.agent.promotion

The repository exists to earn the switch to mainnet: **promote if measured
profit is positive, stay and learn otherwise.** This module is that sentence
as arithmetic. It reads the same records the system already keeps — the
ledger's mark-to-mainnet round trips and the scorer's model-vs-shadow pairing —
and renders a verdict against thresholds in `config.yaml` (`promotion`).

The gate measures; the owner disposes. Nothing here flips `use_testnet` —
promotion is a deliberate config edit made by a human reading this report,
exactly as the kill switch is a human's file to remove.
"""

from __future__ import annotations

import json
from pathlib import Path

from trading.accounting.costs import CostLedger
from trading.config import AppConfig, config


def evaluate(cfg: AppConfig | None = None) -> dict:
    """Measure every promotion criterion. Numbers only; rendering is separate."""
    cfg = cfg or config()
    p = cfg.promotion
    since = p.since or cfg.score.trade_since
    ledger = CostLedger(cfg)
    markets = set(cfg.score.trade_markets)

    closed = ledger.closed_trades(since=since, markets=markets)
    n = len(closed)
    gross = sum(t["pnl_quote"] for t in closed)
    # Each trip is charged its own venue's round-trip rate on entry notional —
    # the same hurdle the exit policy prices, so testnet's free fills cannot
    # flatter the verdict.
    fees = sum(
        t["quantity"] * t["entry_price"] * ledger.breakeven_move_pct(t["market"]) for t in closed
    )
    net = gross - fees
    net_pcts = [t["return_pct"] - ledger.breakeven_move_pct(t["market"]) * 100 for t in closed]
    avg_net_pct = sum(net_pcts) / n if n else 0.0

    pairs = {"n": 0, "model_avg_pct": None, "shadow_avg_pct": None}
    exp_path = Path(cfg.score.experience)
    if exp_path.exists():
        try:
            pairs = json.loads(exp_path.read_text(encoding="utf-8")).get("model_vs_shadow", pairs)
        except (OSError, ValueError):
            pass
    pair_n = int(pairs.get("n") or 0)
    model_avg = pairs.get("model_avg_pct")
    shadow_avg = pairs.get("shadow_avg_pct")
    edge = (model_avg - shadow_avg) if (model_avg is not None and shadow_avg is not None) else None

    checks = [
        {
            "name": f"closed round trips ≥ {p.min_closed_trades}",
            "ok": n >= p.min_closed_trades,
            "detail": f"n={n} since {since or 'the beginning'}",
        },
        {
            "name": "net P&L after costs > 0",
            "ok": n > 0 and net > 0,
            "detail": f"{net:+,.2f} quote ({gross:+,.2f} gross − {fees:,.2f} fees)",
        },
        {
            "name": "avg net return per trip > 0",
            "ok": n > 0 and avg_net_pct > 0,
            "detail": f"{avg_net_pct:+.3f}%/trip after each venue's hurdle",
        },
        {
            "name": f"model-vs-shadow pairs ≥ {p.min_shadow_pairs}",
            "ok": pair_n >= p.min_shadow_pairs,
            "detail": f"n={pair_n}",
        },
        {
            "name": "model beats its shadow",
            "ok": edge is not None and edge > 0,
            "detail": (
                f"model {model_avg:+.2f}% vs random {shadow_avg:+.2f}% (edge {edge:+.2f}%)"
                if edge is not None
                else "no resolved pairs yet"
            ),
        },
    ]
    return {"ready": all(c["ok"] for c in checks), "checks": checks, "since": since}


def render(cfg: AppConfig | None = None) -> str:
    """The operator-facing verdict, one line per criterion."""
    cfg = cfg or config()
    result = evaluate(cfg)
    lines = ["*Mainnet gate* (promote on measured profit, stay otherwise)"]
    for c in result["checks"]:
        mark = "✅" if c["ok"] else "▫️"
        lines.append(f"  {mark} {c['name']} — {c['detail']}")
    # Backtest evidence is a PRIOR, never a criterion: it is survivorship-biased
    # and cost-free, so it informs the reading but cannot open the gate.
    replay_path = Path(cfg.score.replay_summary)
    if replay_path.exists():
        try:
            r = json.loads(replay_path.read_text(encoding="utf-8"))
            if r.get("n"):
                lines.append(
                    f"  ℹ️ backtest prior (not a criterion): n={r['n']}, model "
                    f"{r['model_avg_pct']:+.2f}% vs random {r['shadow_avg_pct']:+.2f}%, "
                    f"model wins {r['model_wins']}/{r['n']}"
                )
        except (OSError, ValueError):
            pass
    if result["ready"]:
        lines.append(
            "  🟢 ALL CRITERIA MET — the owner may set `use_testnet: false` "
            "(run preflight and wire_test --live first)"
        )
    else:
        lines.append("  🔵 not yet — stay on testnet and keep filling the corpus")
    return "\n".join(lines)


def main() -> int:
    print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
