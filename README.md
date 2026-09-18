# ai-trading-agent

An LLM trading agent that is **built to measure whether it works**, and has not yet shown that it
does.

It runs three venues — Binance (spot, testnet), Kiwoom KR (paper) and Kiwoom US (measurement
only) — screens each market down to a shortlist, asks a model which name to buy, and hands the
answer to deterministic risk and exit code. Alongside every model decision it records what a
**random draw from the same shortlist** would have done. That control is the point of the
project: it is what turns "the account went up" into a claim you can check.

> **Status.** No real money is traded. The account is Binance testnet plus a Kiwoom paper
> account, and promotion to a live account is blocked by a measured gate that is currently
> **4 of 5**. The one criterion not met is the one that matters: the model does not yet beat its
> random control with a confidence interval above zero. Nothing here is investment advice, a
> product, or a demonstration of profitable trading.

## The idea

Most of the work is not the model. It is the apparatus that keeps the model honest:

- **A paired control.** Every decision journals the model's pick *and* a random pick from the
  identical menu. They resolve over the same horizon against the same benchmark, so the
  comparison controls for the market.
- **A gate, not an opinion.** `trading/agent/promotion.py` renders five criteria — closed-trip
  count, net P&L after costs, average net per trip, paired-sample size, and the model beating its
  control on a bootstrap CI. Promotion is a human act, taken only when the gate is green.
- **Exits derived from the cost hurdle.** A position is not flat at its entry price; it is flat at
  entry plus the round-trip cost. Targets, trails and time stops all fall out of that number
  rather than from arbitrary R-multiples.
- **Context RL.** The model's weights never change. What improves is the measured record it reads
  at decision time — buckets, calibration and priors that only render once they clear a
  sample-size floor. Below that floor the prompt says nothing rather than guessing.
- **An allocator as an annealing schedule.** The model's share of the book rises with realised
  profit *and* a demonstrated edge, and falls on realised loss alone. It never reaches 100%,
  because the random arm is also the control group that keeps the model measurable.

## What has actually been measured

The development log in [CLAUDE.md](CLAUDE.md) is the record, kept newest-first with the reasoning
for each change. The findings that shaped the design:

- **Price momentum carried no edge** on this universe (top-vs-bottom decile spread −0.06%).
  **Order flow did** (+1.05%), which is what the screens rank on.
- **The screen was the largest effect in the system, and it was negative** — a random draw from
  the shortlist underperformed a random draw from outside it by ~4%. A control drawn from inside
  the treatment cannot see the treatment.
- **Overlapping observations inverted a verdict.** Re-measuring a symbol already in flight
  counted one price path dozens of times and punished the arm that concentrates. De-overlapping
  turned "the model is worse than chance" into "the model is indistinguishable from chance".
- **Grade the event you asked about.** Confidence was defined as P(profit after costs) and graded
  as P(+17% target first), so a calibrated model read as overconfident and declined ~95% of
  cycles. The same class of error was later found in the promotion gate itself, which compared a
  72-hour buy-and-hold while every real position exits on a trailing stop.

The recurring lesson, and the reason the tests are written the way they are: **the components
were almost always correct and the wiring was not** — a right function called with the wrong
argument, or called from nowhere at all.

## Running it

```bash
uv sync
uv run pytest                             # no network; httpx MockTransport throughout
uv run python -m trading.preflight        # read-only pre-live check
uv run python -m trading.watch --once     # account status
uv run python -m trading.agent.promotion  # the gate, with the paired CI
```

Configuration lives in `config.yaml` and is read through `trading.config`; `.env` holds secrets
only. Credentials for Binance, Kiwoom, DeepSeek and Telegram are required to run against a live
venue, and none are included here.

## Layout

| path | what it holds |
| --- | --- |
| `trading/agent/` | decision loop, journal, scorer, promotion gate, allocator, offline replays |
| `trading/brokers/` | Binance and Kiwoom clients, shared state contracts, adapters |
| `trading/risk/` | risk gate, sizing, exit policy |
| `trading/accounting/` | cost ledger — fees, API spend, realised P&L |
| `trading/notify/` | Telegram operator surface |
| `tests/` | seam tests: what calls what, with which argument |

## Licence

None granted. Published for reading, not for use.
