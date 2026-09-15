# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
It doubles as the development record: keep the log below current when landing meaningful changes.

## The goal (owner, 2026-09-01; reaffirmed as a prohibition 2026-09-13)

**Switch to Binance mainnet and earn profit there. Promote when measured profit is positive;
stay on testnet and keep learning otherwise.** Every design decision serves that sentence. It is
implemented as the **mainnet gate** (`trading/agent/promotion.py`, thresholds in `promotion:` in
config, rendered in `/status` and `uv run python -m trading.agent.promotion`): closed-trip sample
floor, net P&L after costs > 0, positive avg net per trip, and the model beating its paired random
shadow. The gate measures; the OWNER flips `use_testnet` — after `preflight` and one
`wire_test.py --live` (~$6). Anything benign only on testnet is a mainnet bug: the exit over-sell
of unmanaged balance was exactly that, and exits are now capped at the units this system bought
(`cost_position`; the balance stays the upper bound).

**Owner, 2026-09-13 — “Until positive profit gains, never switch to mainnet.”** Stated after
the status review below found the BOOK profitable while the MODEL measured worse than chance.
This is a standing prohibition, not a restatement: do not propose, recommend or prepare the flip
while the model's edge is unproven — render the gate reading and stop. **The prohibition stands;
its stated basis does not.** The "model measured worse than chance" reading that prompted it was
an artifact of overlapping observations (2026-09-16) — the model is *indistinguishable* from
chance, not inverted. Unproven is still unproven, so nothing about the prohibition changes; but
do not repeat the inverted claim as fact. Note the gap it closes by
hand: the gate's two profit criteria are computed over ALL closed round trips with no arm
attribution, so they can read green on money the RANDOM arm earned (they do today). Only the
shadow/CI criterion currently separates that from a green gate.

## Where things stand (2026-09-16, after the audit)

For two weeks the system measured the wrong things, and every hard verdict it reached —
including the one blocking mainnet — was an artifact. Corrected, the picture is:

- **The model was never worse than chance.** That reading counted one price path up to 117
  times. De-overlapped it is +0.27% vs random, CI −2.09..+2.58, on ~129 real pairs: unproven,
  not inverted.
- **The screen was the largest effect in the system.** A random draw from the menu lost ~4% of
  excess return to a random draw outside it, on Binance and KR alike — and nothing could see it,
  because the model and its control were both drawn from inside it. Both venues now run
  `rank_by: sample` with the change band off; the `screen control` line is the instrument.
- **The model was calibrated and was told it wasn't.** Graded against a +17% barrier it was
  never asked about, it read "overconfident" in every band and declined 95% of cycles.
- **The profit is real but it is not selection.** The random arm earns (+1.40%/trip CRYPTO,
  +0.54% KR paper) from the exit contract, diversification and the absence of a screen. No
  selector — LLM, flow, prior, momentum, deep tier — beats a random draw from its menu with a
  CI above zero. The one significant leaderboard row is negative (the liquidity head loses).
- **"Profitable by model" before any edge exists is the honest target**: result = menu base
  rate + selection edge − costs; the old menu's base rate was −2.6%, the unscreened pool's
  +1.4%. Put the model on that menu and stop mis-grading it, and it trades at the base rate.
- **US cannot realise profit** — no paper venue, no gate. Its verdict can be honest; that is the
  ceiling until either changes.

What opens the gate is unchanged and now unblocked by artifacts: the model, or any selector,
beating its shadow with the CI above zero. The instruments that will speak next, all in
`/status`: screen control (~72h), the model's `since` row (~10 picks on the new menu), the
selector leaderboard and `arm_llm_deep` (each scorer run). If none of them ever clears, the
conclusion sharpens: the profitable strategy here is the unscreened, trail-exited, diversified
book, and the LLM is an overlay holding only the share it earns — which is what the allocator
already does.

## Open owner actions (things no code can do)

Deadlines and owner-only moves, kept here because a newest-first log buries them.

- **The screen change needs its first reading (~72h, so from 2026-09-19).** Both venues now
  run `rank_by: sample` with the change band off. The instrument is the `screen control` line
  in `/status` and `uv run python -m trading.agent.promotion`: if the menu's excess does not
  move toward the pool's, revert `rank_by: sample` → `flow` and the change bands, and the
  2026-09-16 diagnosis was wrong.
- **The screen was measurably destructive; the correction is shipped, not proven.** A RANDOM draw from
  the screen's menu loses 3.93% to a random draw outside it on Binance, 2.98% on KR
  (`screen control` lines in `/status` and `uv run python -m trading.agent.promotion`). Dropping
  the screen hands every slot to a liquidity-only pool — a real decision, not a bug fix, so it
  waits for the owner. Cheapest probe if you want evidence before deciding: the `change 5%+`
  slice is 85 of 127 menu observations at −6.02% median, so capping `max_change_pct` far lower
  tests the mean-reversion reading without abandoning screening.
- **The mainnet deposit is still inert.** The $4,820 the owner sent on 2026-09-02 landed as
  fiat `USD`, not USDT, and this agent spends only the quote asset. Converting it is a manual
  move in the Binance app. Not urgent while the gate is shut — but it is a prerequisite for the
  day it opens, not something to discover that morning.
- **The 20:00 KR session is live in config but unproven on the wire.** The venue routing
  changed with it (rankers to 통합, orders to SOR) and none of it has run against a real
  evening tape — it landed at 21:03 KST on 2026-09-15, after the close. Two things to look at
  on the first evening: does the 15:40–20:00 menu actually differ from the 15:20 one (if the
  rankers still return a frozen tape, `stex_tp: "3"` is not doing what the workbook says), and
  the `kt00018` comparison written next to the value in config — a KRX-scoped positions read
  that hides an NXT fill leaves that position with no stop, quietly.
- **The KR paper account expires 2026-12-01.** That is the hard deadline for a KR verdict.
  KR is currently the worst sleeve by a distance (model −4.21% vs random −2.98%, n=43, and a
  0.14 clear rate against 0.52 on Binance), so it is the sleeve most likely to need a decision
  rather than more time.
- **Watch what the new screen band buys.** Settled and shipped 2026-09-09 (min/max change
  −0.10/0.15), so the system now buys liquid majors on flow rather than microcap breakouts.
  The exit contract was tuned on the OLD population and has NOT been re-measured against this
  one: re-run `uv run python -m trading.agent.exit_eval` once these trips close, and revert
  with `min_change_pct: 0.15` / `max_change_pct: 0.60` if the fills disappoint.

## Development log (newest first)

- **2026-09-17 (overnight, 6/n — closing)** — **What the health pass found, and the state the
  owner wakes to.** Verified live, in the journal and the persisted exit state: every candidate
  carries 27 keys (path, relative strength, vol, range, funding on 22 of 25, `similar_setups` on
  25 of 25, a `market_state` block above the menu); the model's commentary cites breadth, 4h/72h
  momentum, relative strength and "the best similar setups" — and still declines into a
  breadth-0.08 tape, which is the right answer to the information it now has. RAYUSDT was the
  first adoption after the vol stop shipped and persisted at **15.00%** (the cap; a high-vol
  name); the twelve older plans keep their 8% because a stop only ratchets up. **One ordering
  defect fixed**: the regime row was journalled after the slot-cap check, so with the random
  arm at its cap (13 plans, cap 13 at an 11% model share) it appeared once in five cycles — a
  regime series with holes cannot be read back. It now writes after `alloc` and before the cap,
  every cycle. Noted, not a defect: nssm rotates the stderr log on every restart, so a grep of
  the current log misses earlier adoptions — the persisted plans are the record. Gate unchanged
  at 4 of 5 (the model's edge +0.27%, CI straddling zero, n=129); sleeves unchanged; API 2,785
  of 6,000 KRW with the second opinion roughly doubling calls. **Not done tonight, by choice**:
  catalysts (token unlocks, listings — external data, not measurable from the archive); KR
  path/funding features (the Kiwoom screen would need archive bars, and the KR agent has no
  funding source); any menu filter (the best rule measured, `funding>=0 & range>=0.5`, stopped
  a hair short of the CI bar). Everything shipped tonight was validated over six months of
  cross-sections before it reached the model, and every instrument that will confirm or refute
  it live is in `/status`. 396 tests.
- **2026-09-17 (overnight, 5/n)** — **The exit grid read per venue: KR's 4% stop was the worst
  cell on its own board, and a pooled number I logged was KR's, not Binance's.** `exit_eval`
  gained `--market`; the earlier vol-stop grid had pooled 85 KR trips with 97 Binance ones.
  Read alone (72h hold, r:r 2.0, finished column):

      KR   (56 trips, 27 finished)    fixed 4% +1.29%  stops 15  |  vol x 2  +3.33%  stops 1
      CRYPTO (118 trips, 79 finished) fixed 8% −0.53%  stops 22  |  vol x 2  −0.33%  stops 11

  **KR**: the live 4% stop is the worst cell KR has — KR daily vol runs 2–3%, so 4% is about
  1.5σ and fires on noise; every vol multiple roughly doubles the finished net and takes
  stop-outs from 15 to 1–2. Shipped: `exits.markets.KR.vol_multiple: 2.0`, clamped 3–15%,
  the 4% kept as fallback. Small n, large and consistent gap. **CRYPTO**: every contract replays
  slightly negative on the finished column — the replay resolves a stop on the bar's low before
  a target on its high, which is conservative by construction and is why the 09-04 entry says
  to read cells relative to each other, never as P&L — and vol×2 still beats the fixed 8%, by
  0.2pp. The Binance decision stands; **entry 2/n's "+0.475% vs +0.141%, roughly 3×" was the
  pooled grid and is corrected in place.** Also tonight: the case-memory vector gained
  `funding_rate_pct` after re-validation lifted its spread from +3.18% to **+5.08% (CI
  +0.90..+11.22)**; the index rebuilt with nine keys (11,376 cases — names without a perp drop
  out, and carry no `similar_setups`, which is the honest outcome for a hole in the vector).
  The live loop reloads the index by mtime and reads its keys from the file, so no restart
  was needed for the vector. 396 tests.
- **2026-09-17 (overnight, 4/n)** — **Positioning: perp funding, and it says continuation, not
  reversal.** The one positioning signal with six months of free history is the USDT-M
  perpetual funding rate (open interest history stops at 30 days and so cannot be validated to
  this repo's standard; not fetched). New futures data plane: `BinanceEndpoint.host: futures`
  → `broker.binance.futures_data_url`, unsigned only — a signed futures endpoint is refused by
  the client, because this system holds no futures account and must never look as if it might.
  `backfill_funding.py` put the funding at every backtest observation into a side-file (11,385
  rows; names without a perp simply have none) in one call per symbol. **Replayed**:

      decile spreads (excess vs BTC, n=11,126)
        funding_rate_pct     top +1.91%  bottom −0.45%  spread +2.36%  CI +1.33..+3.47  <- excludes 0
        funding_3d_avg_pct   +0.97%  CI −0.28..+2.14
      pick arms (one name vs a random draw from the sample menu)
        funding_high   −0.32%  CI −3.38..+3.55
        funding_low    −3.21%  CI −5.65..−0.60   <- the ONE pick arm on the board that excludes 0
      menu rules (random draw from the menu vs from the pool)
        fund>=0 & range>=0.5   +0.70%  median +0.25%  wins 45/76  CI −0.03..+1.49   <- best rule measured
        funding>=0             +0.06%  (an exclusion alone does nothing)

  **The sign is the opposite of the textbook.** The crowded-long decile keeps going for 72h and
  the most-shorted names keep falling: on this universe at this horizon, positioning is a
  continuation signal, and the contrarian trade — buy the most negative funding — is the only
  single-name rule tonight that loses with a CI excluding zero. Same strength as
  `range_pos_7d` as a decile; as a pick, only the losing side is significant, which is by now
  the pattern of the night (every "buy the weakest" rule loses; no "buy the strongest" pick
  beats a draw). The combined menu `funding>=0 & range>=0.5` is the best rule measured — 45 of
  76 sections, positive median, pool ~51 names — and stops a hair short of the CI bar, so it
  does **not** become a filter; it is the leading candidate for one when the live control can
  confirm it. **Shipped**: `screen.funding_features: true` (ONE `premiumIndex` call per cycle
  for every perp; None where there is no perp; a failed read carries None for all), the
  `funding_low`/`funding_high` arms live, the system prompt says what the field is and what was
  measured, and the decile row reaches `backtest_priors` automatically because its CI excludes
  zero. Not added to the case-memory vector: changing the vector needs a re-validation, and
  the live arms will say first whether it earns it. 396 tests.
- **2026-09-17 (overnight, 3/n)** — **Experiences as cases, not a census.** The owner's
  framing was "the model as an expert, the RAG as experiences" — and the RAG rendered
  experience as "24h-change 0..15%: 47% cleared", which is a census. An expert asks: the
  twenty most similar setups to THIS one — what happened? New `trading/agent/similar.py`
  (`uv run python -m trading.agent.similar`): a standardised feature vector per resolved
  observation (24h change, log turnover, taker flow, 7d return, range position, vol, distance
  from the week's high, volume surge), a nearest-neighbour query, and the neighbours' outcome
  distribution. Pure Python (no numpy in this repo); 13,732 cases, 1.7 MB, the live query is
  25 candidates in about a second. **Validated before it reached the prompt, leave-the-future-
  out** — a query sees only cases that resolved before it opened, enforced per query:

      neighbours' hit rate, top decile vs bottom, realised excess vs BTC
        top  +2.64%   bottom  −0.54%   spread +3.18%   CI +0.48..+6.20   n=1,547   <- excludes 0
      calibration of the score (predicted vs realised hit after costs)
        0.0–0.2 → 0.32 (n=100)   0.2–0.4 → 0.44 (n=582)   0.4–0.6 → 0.46 (n=776)   0.6–0.8 → 0.43 (n=88)

  Monotone at the low end, flat above 0.4: the score is better at naming losers than winners,
  which is consistent with everything else measured tonight (every "buy the weakest" pick
  loses; the bottom decile of range position loses). A comparable spread to `range_pos_7d`'s,
  and since that is one of the eight features it is partly the same information re-expressed;
  the case memory's value is that it combines all eight without a fitted model. **Shipped**:
  `score.similar_k: 30`; each candidate carries `similar_setups` (hit rate after costs, average
  and median return, average excess, n) when it has the full vector, the system prompt says
  what the field is and what was validated, and the scorer rebuilds the index after every
  aggregation so tonight's resolutions are tomorrow's cases. A missing index or a candidate
  with a hole in its vector simply carries nothing — silence, never a filled-in neighbour.
  Prior by the repo's rule: backtest cases, survivorship-biased; the live arms will say whether
  the model uses it. 389 tests.
- **2026-09-17 (overnight, 2/n)** — **The stop now fits the name.** A fixed 8% is a different
  bet on BTC than on a microcap; an expert sets the stop by the name's own volatility. The exit
  grid gained volatility-scaled cells (`exit_eval.vol_multiples`: each trip's stop is k × its
  pre-entry daily vol from 7d of hourly closes, clamped 3–15%) and replayed 182 closed trips
  (113 finished). At the live 72h hold and r:r 2.0, finished column:

      stop         net/trip   stop-outs   trails
      fixed  8%     +0.141%       28        125
      fixed 12%     +0.201%       15        131
      vol x 1.5     +0.473%       19        131
      vol x 2       +0.475%       14        131
      vol x 3       +0.362%       12        132

  Every vol cell beats the fixed stop at **every** hold (24h/48h/72h/96h) — twelve cells, one
  direction, with stop-outs halved and the trail exits untouched. **CORRECTED in entry 5/n**:
  that grid POOLED 85 KR trips with 97 Binance ones, and the +0.475% was carried by KR. Read
  on CRYPTO alone, vol×2 beats the fixed 8% by ~0.2pp (−0.33% vs −0.53% finished, both
  negative in the replay's conservative arithmetic), not 3×. The direction stands; the size in
  this entry does not.
  n_finished 113 sits at the repo's "hundreds" threshold; what carries the decision is the
  consistency and the mechanism, not one cell. **Shipped, Binance only**: `exits.markets.BINANCE
  .vol_multiple: 2.0`, clamped 3–15%, `stop_loss_pct` 8% kept as the FALLBACK. One vol
  definition (`features.daily_vol_from_closes`) serves the grid, the candidate feature and the
  live stop, so the number the model sees, the number the grid measured and the number the
  supervisor sets are the same number. Wiring: `ExitPolicy.plan_for(stop_pct=…)` honours an
  explicit stop and derives target and trail from it, so reward:risk stays a guarantee; the
  supervisor takes an injected `vol_of` reader exactly like `is_dust` (it fetches nothing
  itself), asks it once at adoption, and a failed or empty read means the **fixed** stop, never
  no stop; 0 reads nothing. KR/US were pooled into that grid (85 of the 182 trips are KR) but
  keep their own fixed stops until measured on their own — the mechanism argument is strongest
  where vol disperses most, which is the crypto book. **Live effect at the next adoption**: a
  name with 1% daily vol gets a 3% stop (floor); 3% → 6%; 12% → 15% (cap). Positions already
  under a plan keep it (a stop only ratchets up; re-planning could widen one). Revert:
  `vol_multiple: 0`. 3 exit tests inherit the live grid and now pin `vol_multiples: []`.
- **2026-09-17 (overnight, 1/n)** — **The model gets the path, the market gets a regime gate,
  and both were measured before they shipped.** Owner, before sleeping: "go for fixing all…
  you can change anything to get profit." Two rules held regardless: no mainnet flip, and no
  profit from moving a bar. The audit's last finding was that the model is handed a snapshot
  with no path and no market state and asked a question about the path. **`features.py`** is
  the one definition of the richer set — returns at 1h/4h/24h/72h/7d and the same vs BTC,
  realised vol, distance from the week's high/low, range position, volume surge, taker share at
  24h and 7d, plus a regime (BTC's path, breadth) — computed by the SAME code in the backtest
  and live, so a validation means something. `backfill_features.py` computed it for all 13,801
  backtest observations (side-file; the log stays append-only). **`feature_replay.py`**
  validated every feature three ways over 76 six-month cross-sections before any of it reached
  the prompt:

      DECILE SPREADS (top 10% − bottom 10%, excess vs BTC, n≈12,290)
        range_pos_7d      +2.36%   CI +1.27..+3.77   <- the only one that excludes zero
        ret_4h / ret_72h  +1.34%   from_7d_high +1.10%   vol_ratio +0.99%   (same direction, short)
        taker_share_24h   +0.50%   rs_* ≈ 0   vol_24h ≈ 0
      PICK ARMS (one name vs a random draw from the sample menu): NONE excludes zero, incl.
        near_7d_high +0.08%; every "buy the dip/weakest/most volatile" pick −2 to −3%
      REGIME (pool 72h RAW return by BTC's trailing week)
        up   n=44  +1.58% (median +0.83%)    down n=32  −0.40% (median −0.42%)
        up − down  +1.99%   CI +0.15..+4.09   <- excludes zero

  **Three readings.** Position in the weekly range is information at 72h — a bigger decile
  spread than flow ever had — but as a **portfolio** effect: the single top-range pick does not
  beat a random draw, and a menu restricted to `range>=0.8` reads +1.42% vs the pool with a CI
  straddling zero and a median near 0 while shrinking the pool to 19 names. So it does **not**
  become a filter; it becomes a feature the model sees and a live arm measures. Flow replicates
  weakly. **Relative strength vs BTC carries nothing** at any horizon — worth knowing, since it
  is the first thing a human would ask for. And the regime effect is real and is pure beta: a
  long-only book paying 0.30% a round trip earns +1.6% in BTC-up weeks and loses in BTC-down
  weeks.
  **Shipped.** (1) `screen.path_features: true` — every candidate now carries the feature set
  (25 hourly-kline calls + 1 for BTC per cycle), the prompt gets a `market_state` block above
  the menu, and the four feature arms join `score.arms` (they were registered as no-ops and
  switch on with the flag; one definition, no drift). (2) **`explore.regime_down_multiplier:
  0.25`** — the random arm rolls at a quarter rate when BTC's trailing week is ≤ 0; not zero, so
  the regime line stays measurable; the regime is journalled on every explore cycle before the
  roll. (3) The model's `measured_record` gains `backtest_priors`: only decile rows whose CI
  excludes zero, plus the two regime rows, each with n and provenance — the RAG principle
  applied to the new features. (4) The `arm_*` diagnostic buckets no longer reach the prompt.
  **Caught by test, would have shipped otherwise**: `path_bars: 168` — `ret_7d` divides by the
  close 168 bars back, which needs a 169th bar, so the feature was always None; now 192.
  A prior by the repo's rule (survivorship-biased backtest); the live arms and the regime rows in
  the journal are the confirming measurements. 374 tests.
- **2026-09-16 (arms, six months)** — **No selection rule beats a random draw over six months
  either — and the flow pick is the worst of them by median.** Owner: "proceed". The selector
  arms are pure functions of a cross-section, so `screen_replay` now runs all of them on every
  historical `sample` menu, each pick paired against a random draw from that same menu (its
  mean) — the exact analogue of the live leaderboard's model-vs-shadow pairing. 76 sections:

      arm           edge vs random draw   median    95% CI          wins
      volume_top         -0.20%           +0.24%   -0.91..+0.45     41/76
      flow_top           -1.08%           -1.21%   -2.23..+0.07     29/76
      change_high        +1.25%           -2.23%   -2.94..+6.00     35/76
      change_low         -2.35%           -1.14%   -5.76..+0.74     35/76

  `prior_top` yields nothing by construction: `p_clear` is not a backtest feature, and it was
  fit on these very rows, so a reading would have been in-sample. **The finding that matters
  is `flow_top`**: the one signal this repo ever measured an edge on (+1.05% top-vs-bottom
  DECILE spread, 08-10) loses 1.08% to a random draw when used as "pick the top-flow name",
  29 wins in 76, interval nearly excluding zero on the wrong side — and it was the live board's
  best point estimate (+2.28%, n=62). Six months says that was noise. The decile claim and the
  top-pick claim are different claims; only the first has ever measured true, and the screen
  ranked on the second for a fortnight. `change_high` shows the lottery shape a third time
  (+1.25% mean, −2.23% median). `volume_top` is flat across six months — the live −3.47% was the
  alt-rotation window, as the earlier entry guessed.
  **Where this leaves selection**: the LLM is indistinguishable from chance live (n≈129); five
  deterministic rules are indistinguishable from chance or worse, live and over six months; the
  fitted prior lost to a constant. Nothing tried selects. That is not a dead end — it is the
  measured reason the profit lives in the exit contract, diversification and an unscreened menu,
  and why the allocator holding the model at the share it earns is the right policy rather than
  a compromise. What could still change it: `arm_llm_deep` (live only, days away), and any NEW
  selector, which can now be tried against six months of history in seconds before it costs a
  single live decision. Still a prior; still survivorship-biased. 358 tests.
- **2026-09-16 (replay)** — **The screen's cost is structural, not a bad fortnight.** Owner:
  must we wait 72h? For the verdict-grade live readings, yes — they resolve at the hold
  horizon and a shorter one is a different question. But the 13,801 `backtest` observations
  already on disk carry every feature the screen ranks on at time t (no lookahead) plus the
  72h forward return, with BTCUSDT among them as the paired benchmark. New
  `trading/agent/screen_replay.py` (`uv run python -m trading.agent.screen_replay`) applies
  each menu rule to every historical cross-section exactly as `candidates()` applied it live
  and scores a random draw from each menu against the same section's benchmark. Six months,
  **76 cross-sections**, CRYPTO:

      rule       menu   excess   median   vs pool   95% CI          wins
      old_0909    18    -0.40%   -0.53%   -0.66%   -1.21..-0.08     33/76   <- excludes zero
      old_0830   3.4    +3.98%   -5.47%     —      (n mismatch)      —
      sample      25    +0.20%   -0.24%   -0.06%   -0.43..+0.33     34/76
      pool       162    +0.26%   -0.00%

  **Three readings.** (1) The screen that ran 09-09→09-16 underperformed its own pool with a CI
  excluding zero across six months of regimes — the live fortnight's −3.93% was that structural
  cost in a bad week, not the cost itself. (2) The 08-30 band (15–60% movers) found **~3 names
  a day** and reads +3.98% mean against **−5.47% median** — the same lottery the 09-09 entry
  caught in the backfill, now with the union ranking replayed too. (3) `sample` tracks the
  pool inside the interval, which is what a stride of the pool should do: it neither adds nor
  costs, and that is the claim it was shipped on. **Expectation-setting, stated so it is not
  over-read**: the pool's excess over BTC is **+0.26%** across six months, not the +1.36% of
  the live fortnight — an alt-rotation window. The model's base rate on the new menu should be
  expected near the benchmark, not a point and a half above it. A PRIOR by the repo's rule:
  survivorship-biased (today's pool, not each day's), and the live control on 09-19 remains the
  confirming measurement. No BSTOCKS section qualified: 1,250 opens over 61 names is ~16 per
  cross-section, under `screen_replay_min_group` 30 — the book listed on Binance too recently
  for a six-month replay, and it is paused anyway. 356 tests.
- **2026-09-16 (second opinion)** — **A stronger model, measured rather than assumed.** Owner:
  "proceed" on the one item the previous entry left undone. `agent.tiers.second_opinion: deep`
  asks the v4-pro tier the IDENTICAL question on every decision — same prompt, same menu, same
  instant — and keeps only its `best_candidate`, journalled as `virtual_pick_deep` (+ its
  confidence and tier) and opened by the scorer as **`arm_llm_deep`**, which the selector
  leaderboard picks up by prefix like every other arm. Three invariants, each pinned: the traded
  decision comes from the decide tier ALONE (a deep reply that proposes a BUY changes nothing);
  the second ask is isolated in its own `try`, so the stronger model's availability, latency or
  reply can never touch the decision; and an empty tier means one call, not two. Stashed on the
  agent rather than returned, so the 4-tuple every caller unpacks keeps its shape. **Cost**: one
  extra call per cycle at ~3× flash's token price — a few thousand KRW/day at most against the
  6,000 cap; `/costs` is the instrument. **Not retroactive**, unlike the deterministic arms: it
  needs live decisions, so its first leaderboard row is days away, and it needs `min_shadow_pairs`
  before it says anything. What it will answer is the question the whole "use a better model"
  reflex assumes: whether v4-pro's picks beat v4-flash's on the same menus — on this record the
  flash tier is indistinguishable from chance, so "better" has a measurable meaning here for the
  first time. Two fixtures pin it off where LLM call counts are asserted (`test_measurement`,
  and the new file's own `cfg`, following the repo's one-fixture-per-file pattern). 350 tests.
- **2026-09-16 (model)** — **"Make all three sleeves profitable by models." The model was
  calibrated all along and was being told it was not — so it declined.** The prompt defines
  `confidence` as *the probability that the position ends in profit after costs — by the
  trailing stop, the target, or the time stop*. `_calibration` graded it against
  **`cleared_target`**: reaching the full +17% target before the stop, an event the same prompt
  says ends ~7% of positions. And `best_candidate` carried a THIRD definition ("probability that
  it reaches the target before the stop"). So a calibrated 0.50 stater was told every cycle that
  it hit ~7% — "overconfident" in every band by construction — and did what a well-behaved model
  does with that feedback: it declined **~95% of cycles** (67 orders against 472 random-arm
  attempts), citing its own calibration in so many words. The loop built to correct
  self-censoring was causing it. Regraded on what the prompt actually asks (`scorer.profitable`:
  a stop is a loss whatever the horizon says, a target is profit, a time exit is profit iff it
  cleared the hurdle; the trail is not modelled at resolve time and the docstring says so):

      band          stated    OLD "hit" (target)    NEW hit (profit after costs)
      0.00-0.45      0.33         15/90  17%            30/90  33%   <- calibrated exactly
      0.45-0.55      0.49          0/13   0%             4/13  31%
      0.55-0.65      0.57          1/10  10%             5/10  50%

  The target rate survives as `target_rate` in every row, so the harder event is still visible;
  it is simply no longer what the model is graded on, because it is not what the model was
  asked. The prompt now gives ONE definition, and a test pins that the old phrase is gone.
  **Second trap, the 09-09 one, back**: the model's self-record in the prompt (−2.05% vs
  benchmark, −2.59% on Binance) is entirely from the screen population retired this morning — a
  random draw from it lost 2.57% to benchmark — so the model was discounting itself on a menu it
  no longer chooses from. New `score.model_record_since` (= 2026-09-16): the model's own buckets
  and calibration render an additional **labelled "since" row** once it has `min_bucket_n`
  observations on the current menu, and the block's note says which is which. Nothing is
  hidden; the full-epoch row stays. ~3 days to the first since-row.
  **What this does mechanically, and why it is the honest path to a profitable model sleeve**:
  the model's per-trip result is the menu's base rate plus its selection edge minus costs. The
  menu's base rate was −2.57% (retired this morning; the unscreened pool reads +1.36%); the
  selection edge is ≈0 (leaderboard); and the model traded ~5% of cycles on a calibration that
  said it should not. Fix the menu (done), fix the grader (done), scope the self-record (done),
  and the model trades from a menu whose random draw is positive, at its own base rate — which
  is what "profitable by model" can mean before any edge exists. **KR** is already there
  (+0.64%/trip, n=17) and its 0.45–0.55 band read 0/6 under the old grader. **US** cannot
  realise profit — measurement-only, no paper venue — its virtual verdict is now honestly
  graded, which is the ceiling. **Not done, deliberately**: a second LLM arm on the `deep` tier
  as a virtual pick, to measure whether a stronger model selects better at ~$0.80/day extra —
  it needs a decide-path change and its own day; the three fixes above are the lever with
  evidence behind it. 345 tests.
- **2026-09-16 (arms)** — **The LLM is no longer the only selector on trial.** New
  `score.arms` (`scorer.SELECTORS`): five deterministic selection rules, each a pure function of
  a decision's journalled MENU, opened at scoring time as their own source (`arm_<name>`) and
  paired against the SAME shadow on the SAME menus with the SAME de-overlap rule as the model.
  Because the menu with its features is on every decision record, the arms cost **no slot, no
  dollar, no decide-path change — and they are RETROACTIVE**: the first leaderboard covered the
  whole epoch since 09-02 the hour they were added, instead of the two weeks a new live arm
  would have needed. Rendered in the gate (`ℹ️ selector …`, not a criterion — only the model
  can open the gate) sorted by the CI's LOWER bound, because a point estimate at n≈100 is luck
  not yet ruled out and this repo has already promoted one of those. **First reading**:

      selector          indep n   raw n    edge vs shadow   95% CI
      arm_flow_top          62     860        +2.28%       -2.06..+7.23
      arm_change_low       109    1042        +0.97%       -1.03..+3.04
      arm_change_high      134    1042        +0.91%       -1.78..+3.77
      arm_prior_top         79     817        +0.41%       -3.13..+3.98
      model                129     892        +0.27%       -2.09..+2.58
      arm_volume_top        66     985        -3.47%       -6.55..-0.30   <- the one that excludes 0

  **Nothing selects with a demonstrated edge yet** — every upside interval straddles zero, and
  the sample is 60–130 independent pairs per arm (the de-overlap discards 80–90% of raw rows,
  which is the correct brutality). The one significant result is NEGATIVE: **the most liquid
  name on the menu underperforms a random draw from it by 3.47%**. Two readings of that, both
  stated: it is consistent with the screen finding (the old union scored names UP for being in
  the volume head), and `volume_top` is BTCUSDT on nearly every Binance menu, so over a window
  where alts beat BTC it is partly the window's rotation, not a timeless rule. Same beta caveat
  applies to every arm here: the paired test controls for the MENU, not for the market.
  Worth noticing without over-reading: `change_low` and `change_high` BOTH sit ~+0.9% — the two
  extremes of the change distribution beat the middle — which is what a random-walk menu with a
  trailing exit would produce, and not evidence of a directional signal. `flow_top` has the best
  point estimate and the widest interval; it is the arm to watch, and the 08-10 decile finding
  says it is the one most likely to survive. The board tightens with every scorer run, for free.
  Seams: arm sources are namespaced `arm_` so they stay out of the fit's source list and the
  model's own bucket; ties break on symbol so a re-run reopens the same id; a typo in
  `score.arms` fails a test rather than logging and skipping. The gate render initialises
  `store` on the failure path — a missing `experience.json` raised UnboundLocalError from inside
  `/status` on a fresh checkout. Two mechanics fixtures pin `arms = []` because they count
  observations per decision. 339 tests.
- **2026-09-16 (profit)** — **"Make the trading system profitable by any means." Every honest
  lever, and the three dishonest ones named and refused.** Refused: lowering the hurdle below
  measured cost, dropping the CI requirement, counting the dice's profit as the model's — each
  produces testnet profit that becomes mainnet loss. And no mainnet flip: the owner's rule.
  **Where the money measurably is**, once looked for: (1) the random CRYPTO arm, +6,518 net,
  **+1.40%/trip** (n=64); (2) **the KR paper sleeve, which nobody was counting** — `score
  .trade_markets` is Binance-only because the gate SUMS `pnl_quote` and a KRW trip cannot join a
  USDT sum, but the sleeve report never sums across a sleeve and had simply never listed KR.
  First attributed reading: **KR · random n=68, +5.68M KRW, +0.54%/trip; KR · model n=17,
  +1.77M KRW, +0.64%/trip** — the one venue where the model's traded picks beat the dice per
  trip, on small n. (3) **4 to 7 of 15 slots sat EMPTY on every recent decision.** The model held
  32% of the book and did not use it — 67 orders against 472 random-arm attempts — so a third of
  the book earned nothing while the arm that measures positive was capped at 10 concurrent.
  **Where it measurably is not**: the model's CRYPTO sleeve (−1.03%/trip, n=51) and BSTOCKS,
  negative on every instrument this system has (realised −1.76%/trip n=8, menu −2.07% excess
  n=19, pool −1.21% excess n=42, against CRYPTO's +1.89%).
  **Six changes, each a config line with its reasoning beside it.** `allocator.base_share`
  0.50 → **0.25** and `min_share` 0.15 → **0.05**: the prior was a coin flip and the record is
  not; the model loses nothing measurable because its verdict comes from the virtual pick,
  which costs no slot and no dollar, and it can still climb to 0.85 the moment it earns it.
  Live effect: next-hour share 32% → 27%, converging on **~11%** (target = base − (base −
  min) × penalty × confidence, with confidence now 0.68 on n=68 model trips); random-arm cap
  10 → **13 concurrent**, which is the empty third of the book put to work. `explore.books:
  [CRYPTO]` and `book_slots: {CRYPTO: 25, BSTOCKS: 0}`: BSTOCKS paused in both arms until its
  `screen control` line reads positive — the random arm now draws from 158 names, not 196, and
  the menu is 25 CRYPTO. `pnl.markets` adds KR (per sleeve, in KRW, never pooled; the gate's
  own list is untouched and a test pins that it stays single-currency). **US screen**: min_price
  1 → **5**, change band off, the 등락률상위 ranker dropped — the menu was "thin, low-priced
  momentum junk" in the model's own words and that ranker was the source. US has no random arm
  and so no screen control; this is by analogy and the config says so, with the one backtest
  prior that ever favoured momentum (US 15–40%, +3.93%, n=88) kept in view rather than erased.
  **What the allocator change also did, noted honestly**: pooling KR into the model's arm
  average (percentages only, the 09-13 design) moved it from −1.03% to **−0.615%/trip**, which
  SOFTENS the cut — the model's KR picks are positive and the allocator now sees that. Correct,
  and the reason the share converges near 11% rather than the 5% floor.
  **Re-read, not changed**: the exit grid under the new hurdle. n_finished is now 109 (from 45);
  the live 4320/8%/rr2.0 cell reads +0.181% all / +0.118% finished, 2880/8%/rr2.0 reads +0.232 /
  +0.157, and every 4% stop cell is strongly negative (57 stop-outs). rr 2.0 wins its row
  everywhere. Not decisive between 48h and 72h at this n, and the trail-arm point moved with the
  hurdle (arms at ~+0.9% now, ~+1.5% before) without hurting the grid. Exits stay.
  **US cannot be made profitable by anything in this repo**: it is measurement-only on mainnet
  money behind no gate, and 모의투자 is KR-only. Its menu is now worth measuring; that is the
  ceiling until either the Kiwoom gate opens or a US paper venue exists.
  New seams pinned: a zero-slot book is skipped rather than divided by (the BSTOCKS pause would
  have been a ZeroDivisionError on the first cycle); a paused book is absent from the draw, not
  drawn and refused; `pnl.markets` falls back to the gate's list when empty. 329 tests.
- **2026-09-16 (last)** — **`cleared_hurdle` now follows config instead of the resolve row.**
  The last open item from the morning's audit, and the honest headline is that it was a real
  defect with a small effect. The label is written into each resolve row **at resolve time**, so
  every row resolved before the 09-15 slippage re-measure was graded against the old 0.500% /
  0.600% bar — and this label is the **fit target**, the **bucket clear rate**, and part of what
  the decide prompt reads back. `pnl` and `promotion` already price fees live; `ExperienceScorer
  ._relabel` applies the same principle one layer down. It is **exact, not estimated**:
  `forward_return_pct` is on the resolve row and `book` on the open row, and `_aggregate` merges
  the two before relabelling. `hurdle_pct_at_resolve` keeps the original so the change is
  provable, and `meta.relabelled_against_current_hurdle` reports the flips.
  **Measured effect, stated plainly**: **278 flips in 54,414 resolved rows (0.5%)**, and the
  live arms barely move — 1 of 898 model rows, 9 of 1,042 shadow, 8 of 497 random, 16 of 1,247
  universe. KR and US flip **zero**, because only the Binance books' fees changed. The reason it
  is small is arithmetic: a 0.2pp shift in the bar only flips observations whose forward return
  lands in that 0.2pp band. **The value is that the class of bug is closed**, not that the
  numbers moved: any future `market_fees` edit now propagates to every label instead of leaving
  the corpus graded against a bar that no longer exists.
  **NOT relabelled, and this is a real remaining gap**: `cleared_target` and `outcome` grade the
  price PATH against the exit contract's levels, and the path is not stored — only its
  endpoints. Redoing those needs a re-resolve against the price source, a different and far more
  expensive operation. The staleness is bounded: the hurdle change moved a 100-entry target from
  117.50 to **116.90**, because `min_reward_risk: 2.0` against the 8% stop dominates the target,
  not the hurdle. So `your_calibration` still grades against targets up to 0.5% too high.
  Extracted to its own method specifically so it could be tested — the first two tests written
  for it asserted on config arithmetic and never touched the code path, which is the kind of
  test that passes forever and pins nothing. `tests/test_overlap.py` now drives `_relabel`
  directly, including the boundary (a return EXACTLY at the hurdle has broken even, not won).
  325 tests. Service restarted 01:10; first screen on the new config read 196 in pool → 25
  candidates with no errors.
- **2026-09-16 (later)** — **The screen stopped betting. Both venues.** Owner: "proceed" on the
  finding that a RANDOM draw from the menu loses 3.93% of excess on Binance and 2.98% on KR to a
  random draw outside it. Two changes, each reverting to a config line.
  **(a) The change band is OFF** (`min_change_pct`/`max_change_pct` to 0 on Binance, and the KR
  `max_change_pct: 0.10` with it). On Binance this is measured: `change 5%+` was 85 of 127 menu
  observations at −3.29% excess and **−6.02% median**, and no sub-slice of the menu beat the
  unscreened pool. On KR it is **by analogy only** — the aggregate gap is measured, the per-slice
  decomposition is not — and the config says so, because an inference recorded as a measurement
  is how the 09-09 band got shipped in the first place. Note the band was never what it claimed:
  the filter compares `abs(change_pct)`, so `min_change_pct: -0.10` had been a silent no-op since
  09-09 and a −14% crash was treated exactly like a +14% pump.
  **(b) A new ranker, `sample`, which ranks by NOTHING.** Every ranker this repo has tried picks
  the extreme tail of something, and every tail has now measured worse than the body it came
  from: momentum had no edge (08-10), the fitted prior lost to a constant (09-13), and flow does
  not sort *inside* the menu at all — its clear rate FALLS as flow rises (0.36 / 0.29 / 0.23,
  low to high tertile). `sample` strides across the liquidity-ordered pool so the menu spans the
  one population here that has ever measured positive. The 08-10 decile finding is **not**
  retracted: "top vs bottom decile spreads +1.05%" and "the top 18 are the best 18" are
  different claims, and only the second is what the ranker was doing.
  **The near-miss worth recording**: switching the ranker alone changed **6 of 25 names**. The
  Binance menu is the UNION of a volume head and a move ranking, scored up for appearing in
  both, so the liquidity head wins whatever the move ranker says — the change would have shipped
  as a no-op dressed as a fix. `sample` now bypasses the union and selects alone. Live effect:
  median menu turnover $37.9M → $20.8M, thinnest name $1.30M → $0.56M, **12 of 25 names
  different**, still spanning up to BTC at $1.38B. Every name still clears
  `min_volume_multiple_of_order`, so the thinnest can still absorb the order. On KR the stride
  runs BEFORE the flow enrichment, so it is also cheaper than what it replaces — one ka10061
  call per candidate instead of two.
  **What this is not**: a claim that `sample` is good, only that it stops making a bet the
  record condemns. The screen control (`/status`, `promotion`) is the instrument that will say
  whether it worked, and it needs ~72h of resolutions to speak. If the menu's excess does not
  move toward the pool's, revert both lines and the diagnosis was wrong.
  `tests/test_screen_sample.py` pins the part that matters — that the menu actually changes and
  is not the liquidity head under another name. 320 tests.
- **2026-09-16** — **Three smoking guns. The gate's blocking criterion was an artifact, and
  the screen is the largest measured effect in the system.** Owner: "1. Ridiculous that random
  is profitable, 2. model lose random, something is wrong, 3. learning loop is totally broken.
  Find smoking guns and correct problems not only Binance but also Kiwoom." All three suspicions
  were correct, and they are the same defect family: **nothing was a controlled comparison.**

  **(1) OVERLAPPING OBSERVATIONS — this inverted the verdict.** The live arms re-measure a
  symbol that is still in flight. The model named HEMIUSDT on **117 of 615** Binance decisions
  (19%); each opened its own observation with its own 72h horizon, so ONE price path was scored
  117 times and counted as 117 independent trials. The shadow, drawing uniformly from a 25-name
  menu, spread over **254 distinct symbols against the model's 126** — so the inflation was
  **asymmetric**, and the arm that concentrates was punished for concentrating. The bootstrap
  was then handed a sample size that did not exist and returned a tight, confident, wrong
  interval. De-overlapped (one observation per symbol per horizon):

      venue      raw n   raw model   raw shadow   raw edge  |  indep n   model   shadow    edge
      BINANCE      615      -5.28%       -2.62%     -2.66%  |      68   -2.16%  -3.13%   +0.97%
      US           159      -1.23%       -0.19%     -1.04%  |      38   -0.83%  -1.00%   +0.17%
      KR            96      -1.35%       -1.39%     +0.03%  |      38   -1.89%  -1.43%   -0.45%

  The gate now reads **n=129, model −1.45% vs random −1.73%, edge +0.27%, CI −2.09..+2.58**.
  **This RETRACTS the 2026-09-13 finding** that the model is "worse than chance on n=638, CI
  excludes zero" and everything built on it: the model is **indistinguishable from chance**, not
  inverted, and the corpus is ~129 independent pairs, not 881. This is **methodology trap #2
  from *Research findings*** — the one that produced "+7.4% per trade" and vanished on
  non-overlapping entries — living inside the gate's own blocking criterion for two weeks, on a
  repo that had already paid for the lesson once. `backfill.py` avoids it by construction and
  the `universe` pass opens one observation per symbol at a time; the live picks had no guard,
  and the 2026-09-04 entry explicitly *retracted* the concern ("every decision opens one"),
  reading a correct statement about journalling as a statement about independence. Fixed in
  `scorer.independent()`, applied to buckets and calibration, with a **pair-level** rule inside
  `_pairs` (a pair survives only if BOTH sides are independent — de-overlapping each arm alone
  would drop one side of a decision and silently shrink the comparison). `n_raw` and
  `meta.overlapping_dropped` are reported so the shrinkage is auditable, never silent.
  **Retroactive by design**: the fix is at aggregation, not at open time, so the journal stays a
  complete record and the correction applies to the whole existing corpus immediately.

  **(2) WHY RANDOM IS PROFITABLE: it does not use the screen.** The random arm draws from
  `tradable_pool()` — the tradable universe with strategy filters removed — while the model and
  its shadow are both drawn from `candidates()`, the screen's menu. So the model-vs-shadow test
  could never see the screen: both arms sit inside it. Adding the control group one level up
  (`_screen_control`, rendered in the gate as a non-criterion), identical window, same
  resolution machinery, de-overlapped, excess vs each book's own benchmark:

      BINANCE   menu (a RANDOM draw from the screen)  -2.57%  n=136   clear 0.29
                pool (drawn outside the screen)       +1.36%  n=244   clear 0.52
                universe (broad tradable sample)      +0.96%  n=1088  clear 0.44
      KR        menu                                  -1.22%  n= 56   clear 0.34
                pool                                  +1.76%  n= 41   clear 0.51

  **The screen costs 3.93% on Binance and 2.98% on KR** — an order of magnitude more than any
  model-vs-shadow edge ever measured here, and the answer to "ridiculous that random is
  profitable": the dice are not lucky, they are simply not screened. `shadow` is the honest
  probe because no model touches it. **Same defect on both venues**, which is what the owner
  asked to check. Decomposing the Binance menu: it is not bStocks (CRYPTO −2.85%, BSTOCKS
  −2.07%, both bad); **flow does not sort inside the menu** (low −3.99% / mid −1.82% / high
  −2.74%, with the clear rate FALLING as flow rises, 0.36 → 0.29 → 0.23); and the damage
  concentrates in what already ran — `change 5%+` is **85 of 127 menu observations** at −3.29%
  excess and **−6.02% median**. The screen buys what moved and holds it into mean reversion.
  Also found while reading it: `candidates()` filters on **`abs(change_pct)`**, so
  `min_change_pct: -0.10` is a **silent no-op** (an absolute value is never below a negative
  bound) and the effective band is |change| ≤ 15% — a −14% crash and a +14% pump are treated
  identically, which is not what the 09-09 entry's signed band describes.

  **(3) THE LEARNING LOOP WAS QUOTING INFLATED EVIDENCE BACK TO ITSELF.** Buckets, clear rates
  and `your_calibration` all read the same overlapping rows, so the `measured_record` in the
  decide prompt carried n's that were 3-6x the real sample — the model was being shown its own
  repetitions as independent confirmation. All now de-overlapped at the source.
  **NOT fixed, and stated so it is not mistaken for fixed**: `cleared_hurdle` and `hurdle_pct`
  are baked into each resolve row **at resolve time**, so the existing corpus is still labelled
  against the old 0.500% hurdle — yesterday's slippage correction re-prices `pnl` and
  `promotion` (which compute fees live) but does **not** relabel history. Yesterday's entry said
  those "recompute from config"; for the resolve rows that is **wrong**. Relabelling needs a
  re-resolve pass, which is its own change.

  **Deliberately NOT changed: the screen itself.** The evidence says it is destructive and the
  fix is not obvious — dropping it entirely hands every slot to a liquidity-only pool, which is
  a strategy decision with real money behind it, and the owner's call. It is now permanently on
  trial instead of invisible, which is the actual repair: the system measured the model against
  its control for two weeks while the population handed to both was never on trial at all.
  `tests/test_overlap.py` pins the asymmetry directly (a concentrated and a spread arm with the
  same per-symbol outcomes must compare equal). 314 tests.
- **2026-09-15 (last)** — **The hurdle was wrong by an order of magnitude, and it had been
  teaching the learning loop that trades were losses.** Owner: "proceed" on the fee wall, after
  the API reconciliation showed trading fees at 114x the API spend and 89% of the model sleeve's
  gross. The suspect was the SLIPPAGE half of the hurdle: `commission x 2 + slippage x 2` reads
  0.500% on CRYPTO, of which only 0.200% is real commission — the other **60% was a 15 bps/side
  assumption set for the microcap-breakout population the screen stopped buying on 2026-09-09**.
  New `trading/accounting/slippage.py` (`uv run python -m trading.accounting.slippage`) walks a
  simulated market order through the **mainnet** order book (`depth` is unsigned, so it reads
  the mainnet data plane even on testnet — measuring this against bot-seeded testnet fills would
  answer a different question, the same reason the plane split exists). Round-trip medians at
  today's ~$105 order:

      population              CRYPTO    BSTOCKS    config charged
      model's menu            1.00 bps   1.71 bps   30 / 40 bps
      random arm's pool       4.58 bps   7.23 bps   30 / 40 bps

  **Overstated 30x on the menu and 6.5x on the pool.** Set to **5 bps CRYPTO / 6 bps BSTOCKS** —
  ~2x the RANDOM POOL median, deliberately not the measured value and deliberately not the
  menu's: the pool is what the random arm draws from and it opens most of the entries, and a
  static book snapshot cannot see latency, adverse selection, or the book moving between
  decision and fill. The margin is the same policy the LLM pricing follows — overstate, never
  flatter — and a test pins that the shipped number stays ABOVE the measured cost, because the
  failure mode of getting this wrong in the other direction is reporting profit the account did
  not earn. CRYPTO's hurdle goes **0.500% → 0.300%**, BSTOCKS **0.600% → 0.320%**.
  **This re-prices the ENTIRE record, not just future trades** — `pnl` and `promotion` both
  compute fees live from config — and the gate moved the same evening:

      fees over the epoch   1,601.89 → 958.92 quote   (−643)
      net P&L               +6,066.66 → +6,719.03
      avg net per trip      +0.063% → +0.274%          (4.3x)
      CRYPTO · model        +45.02 → +196.54 net, −1.26%/trip → −1.06%/trip
      CRYPTO · random       +6,140 → +6,603 net, +1.41%/trip → +1.61%/trip

  **What it does NOT do, stated plainly: it does not make the model profitable.** −1.06%/trip is
  still negative, the allocator still reads `strength 0.00` and still hands the book to the dice,
  and the gate's blocking criterion is untouched — the shadow comparison is raw returns, so the
  CI still excludes zero (BINANCE −4.84% vs −2.90%, CI −3.37..−0.41). That is the right outcome:
  a cost correction must not be able to open the gate. **The larger effect is on the LEARNING
  loop, not the P&L report**: `cleared_hurdle` is the fit label and the calibration target, so
  for the whole epoch every bucket clear rate, every `your_calibration` row and the frozen
  prior's own label were computed against a bar twice reality. Those recompute from config and
  are now measured against the real one. Exits move only slightly (net break-even 100.50 → 100.30
  on a 100 entry, target 117.50 → 116.90) because `min_reward_risk: 2.0` against the 8% stop
  dominates the target, not the hurdle. **Deliberately NOT changed**: KR/US slippage (5 bps,
  Kiwoom, not measured by this probe — it prices Binance books only), and `commission_rate`,
  which at 0.1%/side is the standard Binance spot rate and would only fall with a BNB fee
  discount this account has not been verified to hold. Two more tests hardcoded live rates and
  failed on a CORRECT edit (`test_book_hurdles_differ`, and the LLM pricing pair earlier today);
  they read config now and pin the STRUCTURE and ORDERING instead. 308 tests.
- **2026-09-15 (later)** — **The API bill was reconciled against the real console, and it is
  not where the money goes.** Owner pasted the DeepSeek billing page while asking why this is
  taking so long and "wasting funds in real accounts". The paste reconciles to this system
  almost exactly — console **1,434 requests / 23,715,456 tokens** against this ledger's
  **1,422 / 23,525,361** for 2026-08-30 onward (0.99× on both), which identifies that CNY
  100.03 block as the agent's ENTIRE epoch rather than one day. So **real API spend is ¥100
  ≈ $14 total, ~$0.80/day** — and the ledger read ¥175.15, **overstating 1.75×**, which is the
  documented policy (peak cache-miss) working rather than a defect. **Scale, which is the
  point**: trading fees over the same window are **1,602 quote against ~$14 of API — 114×** —
  and they eat **89% of the model sleeve's gross** (+423.83 gross, −378.81 fees, +45.02 net).
  API spend is 0.4% of the gate's P&L figure. Any further work on cost accounting is
  bookkeeping; the fee wall is the money. **Fixed anyway, because stale is wrong**: Flash
  repriced at 12:00 Beijing on **2026-09-10** (USD list: cache hit $0.003, cache MISS $0.15,
  output $0.60 per 1M off-peak, peak double) and `llm.pricing` sat at the old 3.00/9.00 CNY for
  five days. Now **2.05 / 8.18** — peak cache-miss as policy requires, derived from the USD
  notice at DeepSeek's ~6.82 internal ratio (NOT market FX), which re-prices the epoch at
  CNY 150 against the real 100: still conservative, by 1.50× instead of 1.75×. V4 Pro billing
  is explicitly unchanged in the same notice and stays at 9.00/27.00. **Deliberately NOT
  plumbed**: cache-HIT tokens, billed here as misses at 50× the rate, are most of the residual
  gap — the prompt is largely static so DeepSeek caches it — but the client reads
  `prompt_tokens`, not `prompt_cache_hit_tokens`, and adding that refines a $14 line item.
  Two cost tests hardcoded the live rate and failed on a CORRECT price edit; they read the rate
  from config now and pin the CONVERSION (CNY list ÷ usd_cny, never market FX), which is what
  they were always about. 299 tests.
- **2026-09-15** — **The KR day now runs to 20:00, and the clock was the easy half.**
  Owner: "Korean stock markets are extended until 8:00pm. Kiwoom must be adjusted." The
  extension is NOT a longer KRX day — KRX still ends at 15:30 — so the evening window exists
  only on the alternative venue (NXT), and a config that moved `close` alone would have opened
  a session this system could neither see nor reach. Read from the parsed spec workbook, the
  venue parameter has **two incompatible enums** and the config was on the wrong side of one:
  the RANKERS (`ka10032`, `ka90009`) spell it `1:KRX, 2:NXT, 3:통합` while the account calls
  (`ka10075`, `ka10076`) spell it `0:통합, 1:KRX, 2:NXT`. The account calls already sent `"0"`
  (통합, correct); all three rankers sent `"1"` — **KRX only**, which after 15:30 ranks a
  closed tape, so every evening cycle would have screened a frozen 15:30 snapshot and called
  it the day's flow. Now `"3"`. Order routing (`kt10000/1/2/3`, which take `KRX|NXT|SOR`) moves
  `KRX → SOR`: a KRX-routed order simply cannot fill in the evening, and pinning to NXT instead
  would mis-route the 09:00–15:20 session — SOR is the only value correct at every hour of the
  new day. `OrderExecutor.cancel` was sending a **hardcoded** `"KRX"` while `_body` read config
  (invariant #2, and harmless only while the two agreed); it reads `self.exchange` now, because
  a cancel that cannot reach the resting order fails silently and leaves live exposure the
  supervisor believes it has already pulled. **The clock itself gained a break**: `open`/`close`
  have always bounded CONTINUOUS trading rather than the posted day (that is why KR sat at 15:20
  against a 15:30 KRX close — 15:20–15:30 is the closing auction), and the new day has an
  auction plus a venue changeover in the middle of it, which one open/close pair cannot express.
  `Session.breaks` subtracts windows and can only ever subtract — a test pins that a malformed
  break cannot ADD hours. KR reads `09:00–15:20, 15:40–20:00`. **Budget checked, not assumed**:
  at the 900s interval KR goes ~25 → ~42 cycles/day, three venues ~146 → ~165, ~4,100 KRW
  against the 6,000 ceiling — still ~30% headroom, no change needed, stale comment corrected.
  **Deliberately NOT changed, and the one open risk**: `kt00018`/`kt00004` (positions,
  evaluation) take `KRX` or `NXT` and offer **no 통합 value**. If that field filters holdings
  rather than selecting the valuation venue, an evening fill would be invisible to the snapshot
  the exit supervisor reads — a position with no stop, the 2026-08-12 failure class, and it
  would fail QUIETLY. NXT would be strictly worse (it marks the whole book at the thinner
  venue's prices), so it stays on KRX with the check written next to the value: after the first
  SOR fill outside 09:00–15:20, call `kt00018` under both and compare the symbol lists. Not
  verifiable tonight — the change landed at 21:03 KST, past the new close, and a hand-issued
  paper token revokes the running service's. Sessions had **no test coverage at all** before
  this; `tests/test_sessions.py` pins the two-session day, the weekend, the always-open
  short-circuit, and both routing values. 299 tests.
- **2026-09-13 (last)** — **The fitted prior was refit and NOT shipped: on recent data it does
  not beat a constant.** Named the previous entry as the next step if the model's edge stayed
  negative — the artifact was from 09-03, trained on the pre-inversion population, so a refit
  looked overdue on its own. `uv run python -m trading.agent.fit --dry-run` on 10,000 more
  resolved rows (n_train 33,793 → 43,793; `shadow` joins the source list now that those
  observations resolve) reads **worse**, and worse in the way that matters:

      artifact      holdout base   null log-loss   model log-loss   AUC
      09-03 (live)      0.4596         0.6899          0.6888       0.575
      09-13 (refit)     0.4940         0.6931          0.6995       0.562

  The live artifact beats a CONSTANT prediction by 0.0011 of log loss — noise, not a model. The
  refit **loses to a constant by 0.0064**. Since the holdout is the LAST fifth by time, the refit's
  holdout is the recent regime, so the sharpest reading is not "the refit is worse" but **on
  recent data the prior has no edge at all**. Artifact left untouched (`--dry-run` writes
  nothing); `screen.rank_by` stays on `flow`, which is the one signal this repo ever measured an
  edge on. **This retracts a recommendation made the same day**: that the frozen prior was the
  obvious replacement selector because "a ridge logistic over flow and turnover is far likelier
  to beat random than an LLM judging barrier bets". Measured, it is not — it is near-chance too,
  so replacing one near-chance selector with another buys nothing. **Checked and found sound, so
  NOT changed**: `p_clear` reaches the prompt with its own provenance —
  `ScorerModel.describe()` is in the decide payload ([loop.py:383](trading/agent/loop.py#L383))
  carrying `holdout AUC`, and the live journal shows the model discounting it correctly and
  unprompted ("the fitted priors cluster at 0.44-0.50, barely above coin-flip on the 0.6%
  hurdle"). That is the `min_bucket_n` principle — silence over fabricated priors — already
  holding on this channel. No code change; recorded so the refit is not proposed a third time.
- **2026-09-13 (later)** — **The allocator was reading the wrong thermometer, and the branch
  built to cool the model had never once fired.** Owner: "better idea?" — better than making
  per-sleeve P&L a fifth gate criterion, which would change nothing today because the shadow/CI
  criterion already blocks. This does change something, hourly: `plan()` took `avg_net_pct`
  straight from `promotion.evaluate()`, which is **pooled over every closed trip**. So the dice's
  +1.47%/trip scored `profit 0.87` and held the model at **half the book** while the model's own
  trips ran **−0.42%/trip** — and [allocator.py:169](trading/agent/allocator.py#L169), the branch
  whose message reads *"giving the book back to the dice"*, sat unreached for six days. The
  annealing schedule was pinned at `base_share` because the temperature was measured on the wrong
  sleeve. Fixed by pointing BOTH the profit driver and its confidence at the model's own arm:
  `promotion.evaluate` now carries `model_n_trips` / `model_avg_net_pct` from `pnl.evaluate`
  (single source preserved — the allocator's docstring invariant is that the hourly decision and
  the outer verdict can never disagree about what the model earned), and `plan()` reads those.
  **Percentages only**, deliberately: a net-%/trip figure is unit-free and may be pooled across
  books where money may not, so `pnl.evaluate` grew an `arms` section carrying n and
  `avg_net_pct` and no currency total at all. Confidence counts the model's trips too (33, not
  the book's 83) — using the book's sample to license a decision about the model's picks is the
  same category error one level down. **Live effect, immediate**: `profit 0.87 → 0.00`,
  `confidence 0.83 → 0.33`, and the share steps **50% → 45%** this hour, converging on ~40% and
  falling further as the model's sample grows (confidence scales the penalty, so a model that
  keeps losing gets colder the longer it does it — at n=100 the same −0.42%/trip lands at ~21%).
  The arm earning +1.47%/trip gets the book back. **Why this does not blind the gate**: the
  paired corpus that could redeem the model comes from `virtual_pick`, journalled on every decide
  reply whether or not the model trades — it costs no slot and no dollar, so cooling the model
  slows its chance of proving itself not at all. That is the same independence argument the
  allocator already makes about the shadow corpus, and it is the first thing to re-check if
  either is moved. Three regression tests pin it, the one that matters being
  `test_the_dices_profit_buys_the_model_nothing`. 283 tests.
- **2026-09-13** — **The profit was real; it was just not the model's.** A status review asked
  the gate and it read **3 of 5** — trips 83/100, net P&L **+6,195 quote**, **+0.434%/trip**,
  pairs 638 — with the shadow criterion no longer merely unmet but **measurably inverted**:
  model −4.46% vs random −2.33%, **95% CI −3.58..−0.57**, so the interval EXCLUDES zero. The
  model is worse than chance on n=638, and it is not the falling tape doing it: excess return
  against each book's own benchmark over the identical window is **−4.25% for the model vs
  −1.29% for its shadow** (n=585), with a worse hurdle clear rate (0.299 vs 0.338), more
  stop-outs (60% vs 54%) and fewer targets (15% vs 20%). Every axis, same direction.
  **Owner, reading that: "Until positive profit gains, never switch to mainnet."** Recorded as a
  standing prohibition in *The goal* above. Then the sharper question — the gate's two profit
  criteria are pooled over EVERY closed trip with no attribution, so they were green on money
  the model did not earn. Owner: **"daily PnL for each sleeve independantly."**
  New `trading/agent/pnl.py` (`uv run python -m trading.agent.pnl`, rendered in `/status` under
  the gate). A sleeve is a book crossed with the arm that opened the position; attribution is a
  LOOKUP, not an inference, because both arms announce their entries (`order` rows carry the
  model's intent, `explore` rows the dice's), matched nearest-in-time within
  `pnl.match_tolerance_s`. Three decisions worth keeping: events are **not consumed** on match,
  since FIFO splits one buy across several sells and one order legitimately fathers several
  trips; an unmatched trip is **`legacy`**, never folded into an arm, so the failure mode is
  under-claiming; and **nothing is ever summed across a sleeve**, because `pnl_quote` is in the
  venue's own quote currency and adding CRYPTO (USDT) to KR (KRW) is the 2026-09-01 `/costs`
  defect all over again. **The first reading, and the point of the exercise** — all 83 trips
  attributed, the three sleeves summing to exactly the gate's +6,195:

      CRYPTO · model     n=33   net   +351.11 USD   −0.42%/trip
      CRYPTO · random    n=45   net +5,957.49 USD   +1.47%/trip
      BSTOCKS · random   n= 5   net   −113.61 USD   −3.21%/trip

  The model took **40% of the trips and earned 5.7% of the money**, and is NEGATIVE per trip
  while the dice are positive. Note the two figures disagreeing in sign on the model sleeve
  (+351 total, −0.42%/trip): a few large notionals carry it, which is exactly why the gate keeps
  both a money criterion and a per-trip criterion — and why neither alone is a verdict.
  **Deliberately NOT done: this is not yet a gate criterion.** The owner asked for the
  measurement; turning it into a fifth green/red line is the next gradient step and is the
  owner's call, not a side effect of building the report. Today it would change nothing anyway —
  the shadow/CI criterion already blocks. Also re-ran `exit_eval` (the item the 09-09 entry left
  open): at the live 8%/72h/rr-2.0 the contract reads +0.484% all / **+2.509% finished**, and the
  best cell is a WIDER stop (48h/12%/rr-2.0, +2.869% finished) — but `n_finished` is 45 in every
  cell and this repo's own 09-04 rule is to decide from finished columns only in the hundreds,
  so **exits unchanged**. 280 tests.
- **2026-09-09 (later)** — **The screen's momentum gate inverted, on a sample large enough to
  trust.** A 180-day `backfill.py` pass (10,338 observations, 232 symbols) settled the band the
  earlier entry left open, and it settled it against the first reading: at n=48 the contested
  15–40% band scored +15.49% avg / clear 0.500 and looked fine; at **n=171** it reads +7.03%
  avg but **−4.03% median with the worst clear rate of the three** — a lottery, and exactly the
  trap the 09-03 medians and clear rates were added to catch. Read on robust statistics both
  sources now agree:

      band       universe clear   backtest clear   backtest median
      <0%            0.593            0.425            -0.31%
      0..15%         0.470            0.422            -0.34%
      15..40%        0.200            0.404            -4.03%

  So `min_change_pct: 0.15` / `max_change_pct: 0.60` becomes **-0.10 / 0.15**: the gate is now
  permissive over the two bands the record favours and the FLOW ranker — the one signal this
  repo ever measured an edge on (+1.05% spread, vs momentum's −0.06%) — does the selecting
  inside them. The old gate was momentum ("20% beat 10% in every backtest window"), i.e. the
  screen ranked by flow while gating by the thing 2026-08-10 retired as noise. **Live effect,
  measured**: the menu goes **5 candidates → 25**, extended 18–26% gainers give way to liquid
  majors (BTC/ETH/SOL/AVAX/XRP), and taker flow reads 0.485–0.596 — mostly ABOVE parity, where
  every candidate on the old menu sat below it. That was the model's own stated objection,
  verbatim, in every recent decline. **First decision on the new menu**: confidence **0.48**,
  the epoch's highest (0.366 mean; the fixes moved it 0.366 → 0.44 → 0.48), still declining but
  for a THIRD reason — no longer the contract, no longer the menu, but its own
  `your_calibration` row: "this model's calibrated outcomes in this venue have run below its
  stated probabilities". That is the context-RL loop working as designed, and it is
  self-limiting in a way worth remembering: **that calibration was earned in the old regime**,
  on picks drawn from the band just removed, so the model is discounting itself on evidence
  from a menu that no longer exists. It clears as new picks resolve at 72h (~09-12); until
  then, caution from the model is stale evidence, not a signal. Slots read 6 of 15 — the random
  arm has filled 9 since the dust fix, so closed trips (26/100) will finally move. **Not
  measured, and the open risk**: the exit contract was tuned on the OLD population, and nothing
  yet says 8%/72h/trail-at-1.8% is right for liquid majors instead of microcap breakouts —
  re-run `exit_eval` once these trips close. Revert is one line: `min_change_pct: 0.15` /
  `max_change_pct: 0.60`, marked in the config. Also fixed a flaky test added the same day: the
  429 re-sign check compared two query strings, which a millisecond-resolution timestamp can
  legitimately make identical; it counts signings now. 270 tests.
- **2026-09-09** — **The book had wedged itself, and the model was being asked the wrong
  question.** Progress check found the gate at 2 of 4 (net P&L +6,589 quote, +2.836%/trip;
  pairs 2 → **314** in five days — the virtual pick did exactly its job), but **zero model
  entries in 100 decisions** since the allocator landed. Four fixes, each measured first.
  (1) **Dust held 11 of 15 slots.** Every "managed position" was a stop-out remainder worth
  $2.49 in total (PROMUSDT 0.34, AMZNBUSDT 4.4e-16). `_managed_symbols` counted
  `cost_basis > 0`; the supervisor's `_order_dust` correctly refused to adopt them, so
  `exit_policy_BINANCE.json` was `{"plans": {}}`. The two definitions of "held" disagreed and
  the slot side lost — monotone, one slot per stop-out, unfreeable because no order is small
  enough. Same class as the 08-30 seed-balance bug that `cost_basis > 0` was itself the fix
  for; dust keeps a cost basis, so it leaked back in. "Managed" is now what the supervisor
  manages: paid for AND large enough to exit, with an unknown price deliberately NOT read as
  dust so the two seams agree. Live effect: **4 → 15 free slots**. (2) **The prompt described a
  contract that does not exist.** `trade_rules` listed stop / target / time and never mentioned
  the TRAIL, so the model judged every candidate as a "+17.8% before −8%" barrier bet and
  declined 100 cycles running, in so many words. The exit grid — extended here with a
  `reward_risks` axis, because a grid varying only stop and hold could never speak to the one
  knob the model kept citing — says the barrier framing is simply false. Trail exits are now
  counted separately from stop-outs, and at the live contract 44 resolved trips end
  **trail 31, time 9, target 3, hard stop 1**. The trail arms at a **1.8%** gain, not 17.8%:
  the model was estimating a 7% event (its 0.37 was well calibrated — for the wrong event) when
  the real question is "does it run up at all". `trade_rules` now carries the trail and the
  measured exit mix. **`min_reward_risk` deliberately NOT changed**: the new axis says 2.0 is
  the best cell at the live 8%/72h (+2.15% finished vs +1.77% at 0.75) — lowering it fires the
  target more often for less money. The knob the model blamed was not the defect; the
  description of it was. (3) **The 0.45 confidence floor left the prompt.** In code it only ever
  triggered escalation — it has never rejected a trade — but `trade_rules` advertised it as a
  threshold and the model dutifully self-censored against it, on a signal the calibration now
  shows ANTI-predicts across n=238 (0.00–0.45 → 27% hit / +0.60%; 0.45–0.55 → 4% / −0.58%;
  0.55–0.65 → 0% / −3.11%). Escalation is unchanged; the number is simply no longer advertised
  as a gate. (4) **Signed calls broke on non-ASCII symbols.** `_sign` signed a hand-joined
  `k=v` string while httpx transmitted a percent-encoded one, so every signed call naming the
  testnet's `币安人生USDT` failed `-1022` and its cost basis was unreadable for as long as it
  was held; the 429 path then replayed a stale signature after sleeping, giving `-1021`
  (both observed live). The signature now covers `urlencode`'s own output, that string is sent
  verbatim, and a rate-limited retry re-signs instead of replaying. **Confirmed live after the
  restart**: `free_slots` reads 14 (15 minus the entry the random arm made in the same minute),
  the random arm bought UNIUSDT at +0.77% — its first entry in 31 hours, and from outside the
  band the screen imposes on the model — and the decide call raised stated confidence to
  0.43–0.44 (mean was 0.366, epoch max 0.520) while declining on a MARKET judgement
  ("extended 18-26% with weak taker-buy flow"), with no mention of the target or the floor in
  any reply since. The contract complaint that ran for 100 consecutive decisions is gone.
  **The model was then still starved by the screen** — `min_change_pct: 0.15` confined every
  menu to the 15–40% band, and the record first appeared to DISAGREE WITH ITSELF about it: the
  live universe sweep called it the worst band it has (n=16, −5.24%, clear 0.188) while the
  60-day backfill called it fine (n=48, +15.49%, clear 0.500). Settled by evidence rather than
  by waiting — see the next entry. 270 tests.
- **2026-09-07** — **The model's share of the book is now EARNED AUTOMATICALLY**
  (owner: "the model's portion is supposed to increase as profit gains, up to 85%; the
  mechanism must be automatic... the system is a learning system as a whole, context-RL,
  where model is fixed but RAG contains data"). New `trading/agent/allocator.py`
  (`uv run python -m trading.agent.allocator`, rendered in `/status`). **The status check that
  prompted it**: since `promotion.since`, the model had opened **1 position against the random
  arm's 23** — a ~4% share, not the ~50% `explore.entry_pct` implied. Three causes, measured
  from the journal: **281 of 298 decisions saw `free_slots=0`** (the random arm was permitted
  12 of 15 concurrent slots and runs BEFORE the model, so it had first refusal on every slot);
  the model cleared its 0.45 confidence floor on only 20 of 297 decisions (mean confidence
  0.380, max 0.570); and `min_reward_risk: 2.0` against an 8% stop demands a **+17.8% target in
  72h**, which the model declines cycle after cycle in so many words. **Slots, not frequency,
  were the binding constraint** — so the allocator sets BOTH knobs from ONE number:
  `entry_pct = 1 - model_share` and `explore.max_positions = slots x (1 - model_share)`. At
  `base_share` 0.50 that reproduces the old `entry_pct: 0.5`; at `max_share` 0.85 it lands
  exactly on the `floor_pct: 0.15` the config had documented as the manual decay target since
  08-30 — a decay **never once performed by hand** in eight days, which is why it had to become
  code. The driver is asymmetric on purpose: **up** needs realised profit AND a demonstrated
  edge (`min(profit_score, edge_score)`, the edge read from the bootstrap CI's LOWER bound),
  because this repo already measured that profit alone proves nothing in a rising market;
  **down** needs only realised loss, which requires no significance test. Both scale by
  `confidence` = the weakest of trips / shadow pairs / **RAG buckets past `min_bucket_n`** —
  the owner's context-RL point encoded literally: the weights are frozen, so capital tracks
  what the CONTEXT knows. Rate-limited to `max_step` 0.05 per hourly update (50% → 85% takes
  7h, and falls back symmetrically), persisted to `data/allocation.json` so a restart resumes
  the ramp. **Safe because the two random mechanisms are independent**: the gate's paired
  corpus comes from `shadow_random`, journalled per DECISION, not from the random ENTRY arm —
  so handing slots to the model does not slow the gate's slowest criterion at all. `max_share`
  below 1.0 enforces the "ε never reaches zero" invariant in code rather than in a comment.
  Two seam defects found and fixed while wiring it: `_random_positions` was in-memory only, so
  every service bounce silently un-enforced the new slot reserve (now restored from the
  journal); and the `enabled: false` off switch was checked in `maybe_run` but NOT in
  `current`, which is what `run_explore` reads — a disabled allocator would still have
  overridden the hand-tuned config it exists to defer to. **First live effect**: today's
  reading holds at base 0.50 (profit scores 1.00, but the edge CI lower bound is −1.66%), yet
  the explore slot cap tightens 12 → 8, freeing four slots for the model immediately.
  **Deliberately NOT changed**: `min_reward_risk` and the 0.45 confidence floor — both are
  exit-contract/prompt decisions the exit grid does not yet support (see 09-04), and
  calibration currently reads INVERTED (0.00–0.45 → 27%, 0.45–0.55 → 6%, 0.55–0.65 → 0%), so
  the floor is filtering on a signal that anti-predicts. Those are the next two gradient steps
  and they need their own evidence. 266 tests.
- **2026-09-04 (later)** — **"Proceed" on the three noted items; two acted on, one retracted,
  the exits contract deliberately unchanged.** (a) Kiwoom `min_call_interval_s` 0.25 → 0.5:
  771 HTTP 429 retries in one paper day, each a 1s backoff, costs more than pacing. (b) The
  exit grid gets a **finished-only view** (`n_finished`, `avg_net_pct_finished`,
  `finished_hold_minutes`): a trip whose price record ends before the hold was marked at its
  last close and counted like an outcome, so long-hold cells were snapshots of open positions —
  the Sep 2 run read 72h/8% at −0.56% (worst), the Sep 4 rerun +0.44% (best) after a rally,
  with 47–61 open trips in those cells and only 2 trips finished at the grid's 96h. **No exits
  change is supported by that grid yet**; decide from the finished columns once they are in the
  hundreds. (c) Retracted: the virtual pick repeating (ROBOUSDT/TUSDT) does NOT throttle
  paired samples — `_open_pick` keys observations by `source:symbol:ts`, so every decision
  opens one; pairs are simply inside the 72h horizon (56 model + 56 shadow opened since Sep 2,
  first resolutions ~Sep 5). 246 tests.
- **2026-09-04** — **Two seam defects from the day-2 performance check** (owner: "fix").
  Gate reading: n=0 countable trips (all 16 open Binance positions were entered Sep 1, before
  `promotion.since`, and the full book has admitted no entry since — first countable trips
  ~Sep 7–8), pairs n=2, exit grid unchanged. (1) **Every KR paper stop-loss sell was rejected**
  — 29 of 29 on Sep 3, Kiwoom 1517 "ord_qty 정수만 입력가능": exit quantities are floats since
  the 08-31 float-exit fix and the order body sent `str(41.0)`; buys, sized as ints, passed, so
  twelve paper positions (~200M KRW) sat with no working stop and Telegram showed only the buys.
  `OrderExecutor._qty_str` sends whole shares and refuses a fractional quantity loudly. Same
  class as the 2026-08-12 defects: correct component, wrong argument type. (2) **The reasoner
  fallback was not a fallback**: `fallback_tier: fast` equals the decide tier, which `ask`
  refuses to retry, so 3 of 44 decide calls died with ~120k chars of reasoning against the 32k
  cap. New tier `fast_nothink` (same model, `thinking: disabled` — verified live, zero
  reasoning tokens) is the fallback; `TierConfig.thinking` carries the flag to `extra_body`.
  Noted, not changed: 771 Kiwoom 429 retries/day on the per-candidate flow read; the virtual
  pick repeats (ROBOUSDT 13×, TUSDT 11× of 41) so paired observations grow slowly by
  de-overlap. 245 tests.
- **2026-09-03** — **Seven learning-loop fixes, one commit** (owner: "fix the above seven
  items", after the day-1 reading below). Each answers a defect measured on the first epoch
  day. (1) **Measurement decoupled from execution** (`agent.decide_when_full`): a full book
  still asks the model and journals the virtual + shadow picks, only execution is withheld
  (62 "no free slots" cycles had produced zero pairs); the random arm now also runs on the KR
  paper account (`explore.markets`, a Kiwoom `tradable_pool` from 거래대금상위) and never on a
  dry-run venue. (2) **Calibration feedback**: the virtual pick's stated confidence is
  journalled, every resolution grades target-before-stop under the live exit contract, and
  the prompt gets `your_calibration` per confidence band. (3) **Excess return + robust
  stats**: each resolution measures the book's benchmark over the same window
  (`score.benchmarks`: BTCUSDT / SPYBUSDT / 069500 / SPY); buckets carry median and clearance
  rate; the paired comparison carries a seeded bootstrap CI and the gate now requires the CI
  lower bound > 0 (`promotion.require_ci`). (4) **Frozen fitted prior** (`trading/agent/fit.py`,
  `uv run python -m trading.agent.fit`): pure-Python ridge logistic over log turnover, change,
  |change|, flow share, book; artifact `data/scorer_model.json` with its own holdout report;
  candidates carry `p_clear`; `screen.rank_by: model` orders by it (kept on `flow` until the
  report earns the switch). (5) **KR screen on the measured signal**: 외국인기관매매상위
  (ka90009, multi-column ranker) + 거래대금상위, per-candidate net-buy share from ka10061,
  10% change cap, ordered by flow. (6) **Exit counterfactuals** (`trading/agent/exit_eval.py`):
  every closed trip replayed under a hold × stop grid with the live ExitPolicy arithmetic,
  written to `data/exit_eval.json`. (7) **Store pooled across sleeves**: the scorer reads all
  three journals, KR/US resolve from the archive parquet on their own hold horizon
  (`trading/agent/prices.py`, never a Kiwoom call), buckets render pooled and per venue, and
  each venue's prompt sees pooled rows plus its own. Ledger closed trips carry entry/exit
  timestamps. 236 tests.
- **2026-09-02** — **API budget 2,000 → 6,000 KRW/day; owner deposited $4,820 to mainnet.**
  The budget day is UTC (`CostLedger.day()`), and the US session occupies its last 6.5 hours
  (22:30–05:00 KST = 13:30–20:00 UTC) — so US measurement always eats leftovers after Binance
  (24/7) and KR (opens 10 min after the reset) have spent all day. Observed live: Sept 1 UTC
  spend hit 2,006 KRW at 19:09 UTC and the remaining US cycles starved ("daily API budget
  exhausted") — a silent sampling bias that would make the US corpus grow far slower than KR,
  not an outage (exits kept running, by design). Owner: DeepSeek is cheap, the budget must be
  enough → 6,000 covers ~150 cycles/day across all three venues at the measured ~25 KRW/cycle
  with ~60% headroom, and stays a real brake against a runaway loop. Service restarted via
  `data/RESTART`. Separately the owner deposited **$4,820 into the Binance MAINNET account** —
  it landed as fiat asset `USD`, NOT USDT (mainnet reads USD 4,820 + USDT 2,617); the agent
  spends only quote-asset USDT, so converting is an owner action in the Binance app. Recorded
  as a ledger `cash_flow` (+6,651,600 KRW), excluded from every P&L figure. Day 1 of the new
  measurement regime: 28 sprint positions unwinding under the 72h/8%-stop contract (5 stop-outs
  overnight), gate at n=0 since `promotion.since: 2026-09-02`, first countable trips ~Sep 4–5.
- **2026-09-01 (night, +2)** — **KIWOOM KR PAPER TRADING IS ON.** The owner reissued 모의투자
  keys at the portal (account 81336915, tied to the 상시모의투자 account, valid to 2026-12-01)
  and dropped them in `.env`. Verified end-to-end through the real client against
  `mockapi.kiwoom.com`: token issued (`return_code: 0`), cash read 500,000,000 KRW (matches the
  portal), positions empty. Config flipped per the recipe: `use_testnet: true` +
  `allow_orders: true`, `data/RESTART` touched. KR now trades paper in-session; US remains
  measurement-only on mainnet reads (`paper_markets`). Note: issuing a token by hand revokes
  the client's paper token (one token per app key — same rule as mainnet, separate cache file
  `data/kiwoom_token_testnet.json`). The paper account expires 2026-12-01 — three months to
  produce a KR verdict for the gate.
- **2026-09-01 (night, +1)** — **Paper flip scoped per market: 모의투자 is KR-only.** The owner
  confirmed 모의투자 is the same API on a different endpoint (`mockapi.kiwoom.com`) and opened a
  상시모의투자 국내주식 account (81336915, 500M KRW seed, valid through 2026-12-01). The domain
  switch was already wired (`spec.domain(testnet=...)`; every workbook page carries the mock
  domain — US pages too, so the spec CANNOT encode KR-only and `broker.kiwoom.paper_markets`
  does). New seam: `KiwoomConfig.paper(market)`; `run_service._kiwoom_cfg` gives each market its
  own config copy — a non-paper market gets dry_run forced AND `use_testnet` stripped, because
  a US agent following the flip would send reads and token to a mock host that cannot serve it.
  Pinned in `tests/test_kiwoom_paper.py`. Old 모의투자 keys retested against the mock host
  directly: still `8001` — dead on both hosts, and opening the paper account did not revive
  them. Still blocked on the owner reissuing keys at the developer portal.
- **2026-09-01 (night, last)** — **Kiwoom path confirmed by the owner: paper, then mainnet
  through the gate.** Paper mode is now a pure config flip (`use_testnet: true` +
  `allow_orders: true` on reissued 모의투자 keys — the flip recipe is commented in config.yaml);
  until both flags are set, dry_run stays forced. Paper uses a different app key, so it never
  touches the mainnet token. Blocked on the owner reissuing keys at the Kiwoom developer portal.
  Service renamed: `trading-agent` running since 21:44. Next code increment: KR/US observation
  resolution from the archive parquet (the venue's data plane), so tomorrow's measurement
  decisions grade in ~3 trading days.
- **2026-09-01 (night, latest)** — **One service, every venue**: `run_service.py` runs Binance
  24/7 plus Kiwoom KR/US measurement cycles in their own sessions (agents built lazily
  in-session only — after hours the Kiwoom token belongs to the archive downloader). Service
  renamed `trading-agent` (legacy `trading-agent-binance` and its `--broker` flag still work;
  the install script removes the old service). Telegram covers all three markets: `/status`
  appends a KR/US measurement section, `/kiwoom` scopes it — journal-based after hours, never
  a Kiwoom API call from the bot. Kiwoom journals split per surface
  (`journal.kiwoom.KR.jsonl` / `.US.jsonl`).
- **2026-09-01 (night, later)** — **KR/US RAG filled from the ai-trading-history archive**
  (`backfill_kiwoom.py`: offline parquet reads, zero Kiwoom API calls — the archive's downloader
  owns the SINGLE Kiwoom OAuth token and works while markets are closed, so this repo must never
  mint one after hours; its measurement loop runs only in-session, which the market-open gate
  already enforces). 22,385 KR + 16,236 US observations over one year. **Measured finding: the
  KR 외국인/기관 flow signal carries an edge** — high net-buy-share tertile +2.01% vs low +0.64%
  over 3 trading days (n=334/tertile), ~5× the 0.28% KR hurdle, same shape as the Binance
  taker-flow edge. US 15–40% band: +3.93% vs +0.53% baseline (n=88). KR momentum band mild
  (+1.68%). Flow feature is a unit-free net-buy share in [-1,1].
- **2026-09-01 (night)** — **Kiwoom KR/US restored as a measurement-only venue** (owner: apply
  context RL to KR/US, use DeepSeek while cheap). Stack restored from git and adapted to the new
  architecture (shared `state.py` contracts, per-broker journals, sessions back in `AgentConfig`).
  Verified live: mainnet auth, KR universe 2,454 from cache, screen 218→25 candidates,
  measurement cycle journalled to `journal.kiwoom.jsonl`. No orders possible (`allow_orders`
  false + forced dry_run). Next increments: KR/US scorer resolution via Kiwoom chart endpoints,
  KR backfill, a scheduled measurement service.
- **2026-09-01 (evening)** — **Fill sprint ended, measurement regime started** (owner instruction:
  the sprint's reset never came, and its 180-min holds bled ~0.4%/trip on terms that never counted
  toward the verdict). Config: loop 900s, sizing 4% × 15 slots (15, not the pre-sprint 6 — trip
  rate scales with slots), explore 0.5/12/1, exits BINANCE 8% stop / 72h hold.
  `promotion.since: 2026-09-02` — the mainnet-verdict clock starts there. The ~50 sprint
  positions unwind under the new 72h contract; entries pause until managed count < 15.

- **2026-09-01 (latest)** — Historical evidence machinery: `backfill.py` (opens+resolves backtest
  observations from 60 days of mainnet klines in one pass — no lookahead, no overlap, `backtest`
  provenance separate from live buckets) and `replay.py` (model-vs-random on reconstructed menus,
  same prompt and trade-rules contract, `--decisions` billing cap; renders in the gate as a
  labelled PRIOR, never a criterion). Collapses the "which regimes pay" and "does the model beat
  chance" questions from weeks to days; live pairs still decide promotion.
- **2026-09-01 (later)** — The goal above stated by the owner and encoded: mainnet promotion gate
  built (`promotion.py`, `/status` *Mainnet gate* section); exits capped at bought units so a stop
  can never liquidate owner deposits alongside the position. First gate reading: 322 trips,
  aggregate net +544 USDT but −0.274%/trip average, 0 shadow pairs — not yet. Then the **virtual
  pick**: `best_candidate` required on every decide reply so model-vs-random accumulates at
  decision rate (~25× faster), attacking the gate's slowest criterion without spending a token
  or a dollar more.
- **2026-09-01** — Kiwoom side removed; the codebase is Binance-only (see next section). `/costs`
  currency fix: fees and realised P&L recorded in the venue's own currency, converted once;
  realised scoped to flat symbols so open buys stop reading as losses (see *Economics*). Fill
  sprint ongoing (~34 positions, 137+ closed round trips); the expected testnet reset had not
  landed as of 02:47 KST. Corpus: ~489 observations opened, 0 resolved — first 72h resolutions
  possible from 2026-09-02 23:44 KST.
- **2026-08-31** — Fill sprint (owner: maximize closed round trips before the reset). Learning-loop
  seam fixes: managed-only prompt/slots/filters, float exit quantities + dust plans,
  moving-average cost basis, per-broker watcher. v4-flash needs 32k max_tokens.
- **2026-08-30** — Revival under owner constraints: Binance Spot Testnet only, DeepSeek only,
  learn by iterating. Data/trade plane split. Learning loop built: explore arm, shadow pick,
  scorer, `measured_record` retrieval (see *The learning loop*).
- **2026-08-12** — Four exit-path defects fixed after the $436 TUTUSDT loss (see *The wiring is
  what breaks*).
- **2026-08-10** — Research findings: momentum has no edge, taker flow does; the strategy did not
  beat buy-and-hold in the clean test (see *Research findings*). Live KR trading began and ended.

## Two venues, one algorithm (since 2026-09-01 evening)

The Kiwoom (KR/US) side was removed on 2026-09-01 morning and **restored the same evening at
owner instruction as a MEASUREMENT-ONLY venue** — the context-RL loop (decisions, virtual and
shadow picks, observations) runs on KR/US menus without a single order, "using DeepSeek while
it is still cheap." Hard constraints: the Kiwoom account is LIVE MAINNET MONEY that has passed
no gate — `broker.kiwoom.allow_orders: false` and `--broker kiwoom` forces `dry_run`; there is
no working paper trading until 모의투자 keys are reissued at the developer portal (the old pair
fails 8001 on BOTH hosts — verified against `mockapi.kiwoom.com` directly). 모의투자 is the
same API on the mock endpoint, KR-only: `paper_markets` scopes the flip, US never leaves the
mainnet host. Kiwoom returns
auth failures as HTTP 200 with a non-zero `return_code` — `_issue_token` checks the body.
Each broker journals separately (`journal.jsonl` = Binance, `journal.kiwoom.KR.jsonl` /
`.US.jsonl`, one definition in `AppConfig.journal_for`), because observations resolve against
the venue's own price source. Since 2026-09-03 the scorer reads every venue's journal and
resolves KR/US from the archive's parquet bars (`trading/agent/prices.py`, hourly when it
covers the window, else daily) — never a Kiwoom chart call, which after hours would revoke
the downloader's token. The experience store is POOLED across sleeves with the venue as a
label: pooled buckets inform every venue, per-venue buckets show which sleeve earned them. DART and the KR flow capture remain removed; git history
before the morning commit retains them. The spec-RAG (`trading/rag/`) is back: deterministic
workbook parsing at build time, `catalog_prompt`/`get(api_id)` at run time, no embeddings.

## Invariants

These are decisions, not preferences. Violating one is a bug even if the code runs.

1. **Broker records are the single source of truth.** Positions, cash and open orders come from a
   live broker read, never from locally accumulated state. Snapshots have no setters by design.
   Anything persisted locally is a derived cache for audit and analytics; if it disagrees with the
   broker, the broker is right. `state.reconcile_before_order` forces a fresh read on the order path.
2. **No hardcoded parameters.** Tunables live in `config.yaml` and are read through `trading.config`.
   If you are about to write a literal timeout, limit, API id or model name into a module, add it to
   the YAML instead. `.env` holds secrets only and is never mirrored into the YAML.
3. **The model proposes, deterministic code disposes.** An LLM must never be the last thing before an
   order. Order endpoints are refused unless `allow_orders` is explicitly enabled, and the risk gate
   belongs in front of that switch.

## Economics — the thing that decides whether this works

Profit is `realised P&L − trading fees − API spend`. All three are recorded in `data/ledger.jsonl`
by `trading/accounting/costs.py`, so break-even is measured, not assumed (`/costs` on Telegram, or
`CostLedger.breakeven()`).

**The book decides the hurdle**: a round trip costs 0.500% of notional on the crypto book and
0.600% on bStocks (0.1% commission each side plus assumed slippage). That is the bar every trade
must clear before it earns anything, and it is unaffected by model quality.
`adapter.fee_market(symbol)` resolves it per symbol. API spend adds a fixed daily floor.

Model prices in `llm.pricing` are estimates — **verify them against the providers' pricing pages**,
because every break-even figure derives from them. Unknown models are billed at zero and warn.

**The ledger records money in the venue's own currency and converts once** (fixed 2026-09-01).
Every fee book carries a `currency` (`FeeConfig.currency`; USDT is treated as USD), `record_trade`
and `record_realised` convert to KRW at write time, and legacy rows convert on read — only for
markets with an explicit `market_fees` entry, so KR-era rows that were genuinely KRW stay
untouched. Before this, USDT figures sat under KRW labels and `/costs` under-reported Binance
fees by the full FX rate. Related: **realised P&L is computed over FLAT symbols only**
(`TradingAgent._flat_traded_symbols`) — Binance reconstructs it from myTrades cash flow, which
reads every still-open buy as a loss; the unscoped figure once showed a −21,820 "realised loss"
that was mostly committed cash, and the daily-loss cap reads the same number.

## Commands

```
uv sync                                   # create/refresh .venv from uv.lock
uv run pytest                             # 350 tests, no network (httpx MockTransport)
uv run python scripts/wire_test.py        # dry run; --live sends ONE ~$6 order
uv run pytest tests/test_risk_gate.py -k concentration
uv run ruff check . --fix && uv run ruff format .

uv run python -m trading.preflight        # READ-ONLY pre-live check; run before trading
uv run python -m trading.llm.check        # every LLM tier reachable?
uv run python -m trading.watch --once     # print account status
uv run python -m trading.watch            # serve Telegram commands
uv run python -m trading.agent.scorer     # standalone scoring pass (RAG build step)
uv run python -m trading.agent.scorer --venue KR   # ...and render KR's prompt block
uv run python -m trading.agent.fit        # refit the frozen prior (writes data/scorer_model.json)
uv run python -m trading.agent.exit_eval  # exit counterfactual grid over closed trips
uv run python -m trading.agent.promotion  # the mainnet gate, with the paired CI
uv run python -m trading.agent.allocator  # the model's earned share of the book
uv run python -m trading.agent.pnl        # daily realised P&L per sleeve (never pooled)
uv run python -m trading.accounting.slippage  # re-measure the hurdle's slippage from the mainnet book
uv run python -m trading.agent.screen_replay   # menu rules replayed over the backtest corpus (a prior)
```

Tests must stay hermetic: fixtures pin `use_testnet`, `allow_orders` and the risk limits rather than
inheriting `config.yaml`, so a live-config change can never silently alter what a test asserts.

On Windows the console is cp949: prefix with `PYTHONUTF8=1` or output is mojibake. `tzdata` is a
real dependency, not incidental — Windows ships no zoneinfo database.

## Architecture

**One client, config-driven endpoints.** `BinanceClient.call(name, params)` reads the endpoint
registry from `config.yaml` (`broker.binance.endpoints`), signs what needs signing, throttles, and
normalises bare-list responses to `{"rows": [...]}`. Adding a call is a config edit.

**Telegram is the operator surface** (`@hjeong_trading_agent_bot`, token `TRADING_AGENT_BOT_TOKEN`).
`trading/watch.py` serves `/status`, `/positions`, `/cash`, `/pnl`, `/costs`, `/halt`, `/resume` and
can push reports on an interval. Inbound chat is untrusted: `poll()` drops any update whose chat id
is not in `TELEGRAM_ALLOWED_IDS` — dropped silently, and the offset still advances so a stranger
cannot wedge the loop. The watcher constructs its client with `allow_orders=False`; monitoring must
not trade.

**Layout** — `trading/agent/` loop, journal, scorer; `trading/brokers/binance/` client, account,
universe, symbols; `trading/brokers/state.py` shared state contracts; `trading/brokers/adapters.py`
the adapter seam; `trading/risk/` gate, sizing, exits, rungs; `trading/accounting/` cost ledger;
`trading/notify/` Telegram + status rendering; `trading/llm/` provider access; `trading/config.py`
YAML + `.env`.

## Exits are derived from the cost hurdle

This is the system's own organising idea, and the exit levels follow from it rather than from
R-multiples off an arbitrary stop (`trading/risk/exits.py`):

- **A position is not flat at its entry price.** Net break-even is entry × (1 + round-trip rate) plus
  that position's share of API spend. Selling above entry but below that is a loss, and the policy
  refuses to call it a win.
- **Reward:risk is guaranteed, not hoped for.** A hurdle-only target is dangerous — `min_reward_risk`
  widens the target so risk and reward scale together.
- **The stop only ratchets up.** `tighten_stop` refuses to widen, in code.
- **The trail arms only past net break-even**, never from entry — otherwise cost drag itself triggers stop-outs.
- **Time is a cost.** A position that has not cleared its hurdle in `max_hold_minutes` is closed.

Two structural rules, both consequences of this system's own design rather than convention:

1. **The supervisor never calls a model.** The decision loop is bounded by a daily API budget and will
   stop mid-session by design, so an exit that needs the model alive is not an exit.
2. **A halt blocks new risk, never risk reduction.** The kill switch, the per-order sizing cap, the
   per-cycle cap and the daily order budget all exempt sells. Each of those is an *entry* control; applying
   it to an exit would make a position larger than the cap impossible to close — the control would trap
   you in the very position it existed to bound.

## The wiring is what breaks — not the components

All four defects that reached production were **correct components that nothing called,
or called with the wrong argument**. Every one had passing unit tests before the seam was checked.
The fixes below were verified in code and by regression tests on 2026-08-12.

- **`run_exits` is now called before the halt gate in `run_cycle()`.** Exits are evaluated on every pass
  before new entries are considered, so stop, target, trail and time-stop can act when the market moves.
- **`ExitPolicy.hurdle` passes the market into the ledger.** Binance exits use the Binance hurdle instead
  of the old KR default; the live reproduction was `0.2800%` vs `0.6000%`.
- **The daily-loss cap no longer applies to sells.** A breached loss cap stops new risk, but does not trap
  an open position that must be closed.
- **`BinanceAdapter.holdings()` now populates `avg_price` from `cost_basis`**, not the live mark. A stop that
  trails downward cannot fire if it is computed from the falling price on each cycle.

The tests cover the seam, not only the helper in isolation. `tests/test_exits.py` checks that `run_cycle`
invokes `run_exits` before the halt check and that Binance exits use the Binance hurdle; `tests/test_risk_gate.py`
asserts that a breached loss cap still permits an exit; `tests/test_binance.py` asserts the cost-basis-based
`avg_price` path.

Cost of learning this: **$436 on TUTUSDT**, closed 2026-08-12 at −13.7% against an 8% stop.

## The universe funnel

The universe is the whole market, fetched from the broker (`exchangeInfo`), never a hand-kept list:
~485 tradable USDT pairs across two books. That is too many to prompt, so every cycle runs

    universe (~485) → flow-ranked screens per book → top N candidates → model → risk gate → broker

Screening is deterministic and happens **before** any model call, which bounds cost: context size is
a function of `screen.candidates`, not market size. The model only ever sees the shortlist plus
current holdings, and `_parse` discards any symbol it was not offered — that is what stops a
hallucinated ticker from reaching the gate.

## Testnet operating mode (since 2026-08-30)

Owner constraints: Binance Spot **Testnet only**, **DeepSeek only**, learn by iterating.

- **The data plane and trade plane are split** (`BinanceClient.data_url` / `trade_url`). The
  testnet's books are thin and bot-seeded, so its tickers, klines and taker-flow are noise — and
  order flow is the one signal this project ever measured an edge on. Unsigned market data therefore
  **always reads mainnet**; `use_testnet` moves only signed traffic (account, orders) to
  `testnet.binance.vision`. The seam is pinned by `test_testnet_splits_data_and_trade_planes`.
- Testnet keys are `BINANCE_TESTNET_API_KEY`/`SECRET_KEY` in `.env` — verified working. The testnet
  lists bStocks pairs too, but only ~1,382 pairs total, a strict subset of mainnet; `tradable_pool`
  intersects the data-plane universe with `client.trade_plane_symbols()` (cached per process; an
  unavailable fetch degrades to no filter and the order fails loudly). Quantisation filters still
  come from mainnet exchangeInfo; a filter mismatch fails loudly at order time.
- **The testnet seeds ~480 asset balances the system never bought.** Cost-basis reads are cached per
  `(symbol, quantity)` — a fill or deposit changes the balance, which is what invalidates. Seed
  balances are refused management (no cost basis → no stop worth the name). The **slot limit counts
  MANAGED positions** (`TradingAgent._managed_count`: holdings with a cost basis) — counting raw
  balances once filled every slot with seeds and silently disabled both entry arms.
- **The decide prompt carries managed holdings only, contract-first.** Embedding the raw positions
  snapshot blew the payload past its 20k truncation guard, which silently cut `trade_rules` — the
  payoff contract — off the END of the prompt: the model declined cycle after cycle with
  "trade_rules not supplied", journalled as considered judgement. Critical fields come FIRST so any
  future overflow truncates detail, never the contract.
- **Pick filters use MANAGED symbols, never raw balances.** `_managed_symbols` (cost basis > 0) is
  the one definition of "held" for slots, shadow and explore alike — filtering by raw balance made
  the shadow pick always None and biased the explore corpus toward unseeded coins, silently.
- **Exit quantities are floats, and dust closes its own plan.** `int(sig.quantity)` truncated a
  0.34-unit exit to 0, which the gate refused as non-positive. The supervisor takes an
  `is_dust(symbol, qty, price)` predicate from the loop (venue lot rules live in the adapter): a
  plan whose holding cannot form a valid order (e.g. remainder under the $5 minNotional) is closed
  with one log line, and dust is never adopted.
- **`cost_basis` is a moving-average book, walked oldest-first.** The old newest-first walk reset on
  each sell and kept walking, so sell-then-rebuy wiped the rebuy's basis to 0 — a real position held
  with no stop. Sells now reduce the tracked position at its average cost, and a sell larger than
  the tracked position (seed units no fill paid for) just flattens it. One reconcile caveat remains,
  accepted deliberately: plan quantity follows the broker, so a managed symbol that also has a seed
  balance is exited in full when its stop fires — broker records are the single source of truth.
- **Testnet balances reset periodically.** A discontinuous equity jump is a reset, not P&L — same
  discipline as owner deposits. Testnet fees are zero, but the ledger keeps charging the mainnet
  hurdle; otherwise every result overstates by exactly the margin that killed trades live.
- **All three LLM tiers are DeepSeek, on the documented V4 ids**: `fast` is deepseek-v4-flash (the
  decide tier), `deep`/`escalation` are deepseek-v4-pro. The legacy aliases still answer but bill at
  undocumented rates. **This account is billed in CNY**, and DeepSeek's CNY list is NOT market-FX of
  its USD list (fixed ~6.82 internal ratio vs ~7.15 market), so the DeepSeek entries in
  `llm.pricing` carry CNY prices with `currency: CNY` and the ledger converts at `llm.usd_cny`.
  Rates are PEAK cache-miss deliberately — the ledger overstates spend rather than flattering it.
  API spend is the only real money this system spends.
- **The bot is the only window into the testnet account** — the Binance app cannot display it.
  `BinanceStatusReporter` (`trading/notify/status.py`) renders the full picture: USDT cash, equity,
  each managed position with its committed exit plan read read-only via `exit_state_path()`, open
  orders, seed balances collapsed to one unmanaged line, the day's DeepSeek spend per model against
  `max_api_krw_per_day`, and the learning corpus. `/positions` and `/cash` answer scoped sections.
- **The service**: `install_nssm_service.ps1` (run as Administrator) installs
  `trading-agent-binance` running `run_service.py --broker binance`. `BinanceWatcher`'s
  `/halt`–`/resume` act on the per-venue `HALT.BINANCE` (the file the gate actually checks), and
  `/resume` warns if the global `data/HALT` is still set. Seam tests: `tests/test_watch.py`.

## The learning loop (built 2026-08-30) — explore, score, retrieve

**The principle (named by the owner, 2026-09-01): context RL — reinforcement learning with a
frozen policy.** The model's weights never change; policy improvement is the growth of the
measured record it reads at decision time. ε-greedy with two twists: exploration updates a
ledger, not the policy (auditable, revertible, immune to reward hacking), and ε never reaches
zero because the random arm's second job — the control group that keeps the model permanently
verifiable — outlives its first. Two loops: the fast frozen inner policy learning in context,
and the slow human outer loop taking evidence-driven gradient steps on the config, each committed
to git with its reasoning. The mainnet gate is the outer loop's convergence test.

**The second framing (named by the owner, 2026-09-07): expanded simulated annealing.** Random
trading is exploration, model trading is exploitation, and ε is a TEMPERATURE — hot means mostly
random moves, cold means mostly greedy ones. Since the allocator
(`trading/agent/allocator.py`) that temperature moves with measured performance: more profit →
colder → more model; less profit → hotter → more random. Read the allocator as an annealing
schedule and its shape follows; three departures from the textbook are deliberate and are the
reason the module is not ten lines:

1. **It anneals on RESULTS, not on a clock.** Classical SA cools as a function of iteration
   count, indifferent to how it is doing — which is exactly what the old hand-turned
   `explore.entry_pct` was, and why it sat at 0.5 for eight days without once being decayed.
   This is the adaptive variant, with reheating: the share falls again when realised P&L turns
   negative.
2. **The temperature never reaches zero, by construction** (`max_share: 0.85`). SA cools toward
   T→0 because its objective is fixed and known, so near the optimum randomness only costs.
   Here the random arm has a second job SA's randomness does not: it is the CONTROL GROUP that
   makes the model verifiable at all. Annealing it away would destroy the only measurement that
   can open the mainnet gate. This system deliberately never fully anneals, because it is not
   only searching — it is measuring.
3. **Cooling requires beating the random arm, not merely a higher objective value.** In a rising
   market the dice make money too, so profit alone says the market rose, not that the model
   picks well — the +96% window in *Research findings* is the measured version of that mistake.
   The temperature therefore drops on `min(profit_score, edge_score)` with the edge read from
   the paired CI's LOWER bound. Asymmetric on purpose: going down needs only realised loss,
   which requires no significance test.

Markets are also non-stationary where SA's landscape is fixed, which is the deeper reason the
floor and the reheating both matter: a fully-annealed policy would be stuck exploiting a regime
that has already ended.

The system learns through a measured-aggregates RAG, never by adapting the model online. Three
arms produce observations, one scorer grades them, and the decide prompt retrieves only what has
earned statistical standing:

- **The exploration arm** (`explore` in config, `TradingAgent.run_explore`): with probability
  `entry_pct` per cycle, one random small entry from `BinanceScreen.tradable_pool()` — the
  tradable universe with **strategy filters removed** (liquidity, lot rules and the stablecoin
  exclusion still apply; the screen's momentum/flow bounds deliberately do not, or the screen's
  own thresholds could never be falsified). No model call, so it costs no tokens and runs even on
  cycles the model skips. Everything downstream is the normal machinery — sizer, gate, executor,
  exit supervisor: exploration changes who proposes, never what disposes. Decay `entry_pct`
  toward `floor_pct` by hand as the corpus fills — never to zero, or model-vs-chance stops being
  measurable.
- **The shadow pick**: every decision record journals the full candidate MENU plus one random
  symbol from that same menu (`shadow_random`) — never traded, resolved identically, the model's
  paired chance baseline.
- **The virtual pick** (2026-09-01): the decide contract requires `best_candidate` on EVERY reply,
  declines included — the model's top-ranked name, journalled as `virtual_pick`, scored as a
  `model` observation, never traded. Without it the model-vs-random corpus grew only on the rare
  cycles the model traded (~2/day); with it, at decision rate (~50/day) — the mainnet gate's
  slowest criterion collects in days instead of weeks. Same anti-hallucination rule as intents:
  off the menu, discarded.
- **The scorer** (`trading/agent/scorer.py`, interval-gated inside `run_cycle` after exits, or
  standalone `uv run python -m trading.agent.scorer`): opens one observation per tradable symbol
  at a time (de-overlap is methodology trap #2), resolves forward returns at the 72h horizon from
  MAINNET klines (testnet fills are fantasy prices), and aggregates into `data/experience.json` —
  buckets by source/book/change-band/flow-tertile, each with its n, plus the paired
  model-vs-shadow summary.
- **Retrieval** (`experience_block`): buckets below `score.min_bucket_n` never render; an
  unfilled store contributes nothing to the prompt — silence, never fabricated priors. Qualifying
  rows appear as `measured_record` in the decide prompt, always with their n.

The seams are pinned in `tests/test_explore.py`.

**Closed trades are scored from the ledger, not from fills.** The prices the ledger records are
data-plane reference prices — mainnet by construction since the plane split — so FIFO-pairing its
own BUY/SELL records per symbol yields mark-to-mainnet round trips with no extra network. Orphan
sells (seed liquidations with no recorded buy) pair with nothing; `score.trade_since` keeps the
live-mainnet era out of testnet statistics — the ledger spans both epochs and mixing them is
exactly the false signal the scorer exists to prevent. `/status` shows the corpus filling
(*Learning* section: observations opened/resolved, buckets past the gate, model-vs-random).

**The exploration decay is no longer manual (2026-09-07).** `trading/agent/allocator.py` computes
the model's share of the book every hour from the measured record and derives BOTH exploration
knobs from it (`entry_pct = 1 - share`, `max_positions = slots x (1 - share)`), ramping 0.50 →
0.85 as realised profit AND a demonstrated edge arrive, and falling back when they do not. This
is the outer loop's gradient step taken automatically: the frozen policy earns capital as the
measured record it reads grows. ε still never reaches zero — `max_share` 0.85 is what enforces
it. The manual knobs survive as the fallback under `allocator.enabled: false`.

**Since 2026-09-03 the loop also measures itself, not only its picks:**

- **A full book still measures.** `agent.decide_when_full` keeps the decide call, the virtual
  pick and the shadow pick running when no slot is free; only execution is withheld (verdicts
  carry "no free slots (measurement only)"). The random arm honours `explore.markets` and
  never runs on a dry-run venue.
- **Calibration.** `virtual_confidence` is journalled; each resolution records `outcome`
  (target / stop / time under the venue's live exit contract) and `cleared_target`; the
  scorer aggregates hit rates per `score.confidence_bands`; the prompt renders them as
  `your_calibration`. The 0.45 floor is now a measured boundary.
- **Excess return and robust statistics.** Every resolution measures `score.benchmarks[book]`
  over the identical window (`excess_return_pct`). Buckets carry `median_return_pct`,
  `clear_rate`, `avg_excess_pct`. The paired model-vs-shadow summary carries a seeded
  percentile-bootstrap CI (`ci_low`/`ci_high`) and `model_wins`; the gate's shadow criterion
  requires the lower bound above zero (`promotion.require_ci`).
- **The frozen fitted prior** (`trading/agent/fit.py`). Ridge logistic regression by Newton's
  method in pure Python over `FEATURES` (log turnover, change, |change|, flow share, a
  missing-flow flag, book one-hots), label `cleared_hurdle`, trained on non-model sources only
  (model picks are selected by the thing being measured), holdout = the LAST fifth by time.
  Artifact `data/scorer_model.json` carries n, base rate, holdout AUC, log-loss and
  calibration deciles. The running system only reads it: candidates get `p_clear`
  (journalled too, so the prior's own calls resolve), `screen.rank_by: model` orders the move
  ranking by it. Refitting is a deliberate command plus a commit.
- **Exit counterfactuals** (`uv run python -m trading.agent.exit_eval`). Every closed trip
  since `promotion.since` (or `--since`) is replayed from the venue's own price record under
  `exit_eval.holds_minutes × stops_pct` with `ExitPolicy` itself (config copy, two knobs
  overridden). Output `data/exit_eval.json`; the owner edits `exits` with the reasoning
  committed. Approximation stated in the module: hourly bars, stop on the low before target
  on the high, trail on closes.

**Fill sprint (2026-08-31, owner instruction)**: ahead of an expected testnet reset the config
temporarily optimises for CLOSED round trips — `explore.entry_pct: 1.0`, `entries_per_cycle: 5`,
`max_positions: 30`, `sizing.fraction: 0.05`, `exits.markets.BINANCE.max_hold_minutes: 180`,
`agent.loop_interval_s: 300`. Restore the marked "was" values after the reset.

## Providers

DeepSeek and Qwen are called **directly**, not through Groq/OpenRouter/GitHub Models (those keys in
`.env` are inactive). Both speak the OpenAI-compatible protocol, so one client covers them and the
difference is a base URL under `llm.providers` plus a model name under `llm.tiers`. Call sites name a
tier (`fast`, `deep`, `escalation`), never a model — swapping providers is a config edit.

`uv run python -m trading.llm.check` pings every tier and exits non-zero if any is unreachable. Run it
after touching a key or endpoint.

**DashScope is region-partitioned.** A Qwen key is valid on exactly one host and the other returns
`401 invalid_api_key` — indistinguishable from a bad key. This account's key is mainland
(`dashscope.aliyuncs.com`); international keys use `dashscope-intl.aliyuncs.com`. Diagnose a Qwen 401
by trying the other host before assuming the key is wrong. The key is read from any of
`DASHSCOPE_API_KEY` / `QWEN_API_KEY` / `ALIBABA_API_KEY` / `TONGYI_API_KEY` (this account uses
`ALIBABA_API_KEY`).

**`.env` is a symlink to `C:\Users\hjeong\OneDrive\.env`** — one shared secrets file across all the
user's projects. Writing to it changes every project, so edit deliberately and back up first.

## Research findings (2026-08-10) — read before changing the strategy

Measured, not assumed. Each test invalidated the previous one's optimism, so the
methodology notes matter as much as the numbers.

**Price momentum has no predictive power here.** Over 1,000 daily bars on 10 majors,
the forward-5-day return spread between the top and bottom decile of 3-day price change
was **−0.06%** — noise. The original screen ranked on exactly this.

**Order flow does.** The same test on 5-day average taker-buy share (kline field 9 /
field 5) gave a **+1.05% spread** (+1.58% top decile vs +0.53% bottom) — roughly twice
the round-trip cost. The Binance screen ranks on this (`screen.use_flow`).

**The trading premise did not beat buy-and-hold** in the one clean test: non-overlapping
trades, train/test split, benchmark over the identical window. Over 2.7 years the strict
configuration (thr 20% / tgt 25%) returned −1% against −12% for holding — better, but not
profitable. Loose filters were far worse than holding everywhere.

Three methodology traps that produced false positives, all of which looked convincing:

1. **Dropping unresolved trades.** Counting only positions that hit a barrier discards
   the boring, cost-bleeding ones and inflated the win rate from 39% to 54%.
2. **Overlapping positions.** Entering every qualifying hour with a 72h hold counts one
   rally as ~72 independent trades. Produced "+7.4% per trade" that vanished entirely
   once entries were made non-overlapping.
3. **No benchmark.** A long-only rule in a +96% window looks brilliant and still loses
   to doing nothing. Always compare against buy-and-hold over the identical period.

**Measured 2026-09-16 (the audit).** Three findings of the same grade as the ones above,
each with its own methodology lesson:

- **The model does not beat a random pick from its own shortlist — and does not lose to one.**
  +0.27% vs shadow, CI −2.09..+2.58, n≈129 independent pairs. The earlier "−2.66%, CI excludes
  zero" was **trap #2 again**: live picks re-measured a symbol mid-flight, and the concentrated
  arm was punished for concentrating. De-overlap before every statistic, pairs included.
- **The screen cost ~4% of excess return.** A random draw from the menu vs a random draw from
  outside it, identical window and machinery. Every ranker tried selects an extreme tail, and
  every tail measured worse than its body. **Trap #4, new**: a control drawn from inside the
  treatment cannot see the treatment — the shadow measured the model against the menu while the
  menu itself was never on trial.
- **Grade what you asked.** Confidence was defined as P(profit after costs) and graded as
  P(+17% target before stop), so a calibrated model read as overconfident by construction and
  self-censored. **Trap #5, new**: a feedback loop grading a different event from the one it
  requests will drive the agent away from the behaviour it exists to encourage.

Still open: whether ANY selector beats chance — five deterministic arms and a second LLM now
run against the same shadow, for free, and the leaderboard in `/status` is the answer as it
forms.

## One venue, two books

- **Binance is two books over one balance.** CRYPTO (~420) and BSTOCKS (~66) share one USDT wallet,
  so they are one agent choosing across the union, never two racing for the same cash. bStocks are
  identified by the `TRD_GRP_261` permission tag — a symbol-suffix rule wrongly matches BNB/SHIB/ARB.
- **The book decides the hurdle**, not the agent: crypto 0.500%, bStocks 0.600%.
  `adapter.fee_market(symbol)` resolves it per symbol.
- **The kill switch is per venue.** `data/HALT.BINANCE` halts entries; bare `data/HALT` halts everything.
- **The gate validates against the market it was built for**, not `agent.market`.

Traps found live and worth not re-discovering:

- `0` in the risk config means *unlimited* to the gate but reads as *"nothing allowed"* to a model.
  The prompt renders meaning (`_describe_limits`), never the raw sentinel.
- `TradeIntent` is `slots=True`, so `__dict__` raises; journal writes use `dataclasses.asdict`.
- `str(0.00001)` is `"1e-05"`, which Binance rejects — quantities use `format(q, "f")`.
- A Binance holding under a resting stop shows `free: 0`; a sell must cancel the resting order first.
- `Decimal("None")` raises `InvalidOperation` (an `ArithmeticError`, not `ValueError`).

## Owner deposits and withdrawals

The owner moves money in and out, so the account balance changes for two unrelated
reasons: trading, and external flows. Two rules keep them from being confused.

**Every equity read is point-in-time, from a fresh broker snapshot.** Nothing caches a
balance and nothing stores an inception equity. That is why a deposit correctly makes the
next order larger and a withdrawal makes it smaller, with no stale figure in between —
sizing, `max_position_pct` and the daily-loss check are all levels, never deltas.

**Never compute performance as an equity difference.** `(equity_now − equity_then)` reads
a deposit as a spectacular gain and a withdrawal as a catastrophic loss. Performance is
realised P&L (broker-reported) minus fees minus API spend — which is flow-invariant.
Flows are journalled separately via `CostLedger.record_cash_flow` and are excluded from
every P&L figure; use them only to build a time-weighted return if one is ever needed.

## The reasoner returns empty content — check this before diagnosing "no trade"

A DeepSeek reasoning model can spend its entire budget on reasoning and return **zero
tokens of content** (reproduced 2026-08-10: 29,751 reasoning chars against an 8,192
`max_tokens` cap → empty content; v4-flash hit the same trap 2026-08-31, fixed by a 32k cap).
`LLMClient.ask` returns `content or ""`, `_parse` finds no JSON, one warning is logged, and
the cycle reports **0 intents** — indistinguishable in the journal from a considered
decision not to trade.

An agent that silently reports "no trade" when it never received an answer is worse than
one that errors. Empty content WITH non-empty `reasoning_content` is a distinct, detectable
state: log it as truncation and retry on `llm.fallback_tier` (which must be a non-reasoning
model), never fold it into a decision.
