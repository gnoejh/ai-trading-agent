# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
It doubles as the development record: keep the log below current when landing meaningful changes.

## The goal (owner, 2026-09-01)

**Switch to Binance mainnet and earn profit there. Promote when measured profit is positive;
stay on testnet and keep learning otherwise.** Every design decision serves that sentence. It is
implemented as the **mainnet gate** (`trading/agent/promotion.py`, thresholds in `promotion:` in
config, rendered in `/status` and `uv run python -m trading.agent.promotion`): closed-trip sample
floor, net P&L after costs > 0, positive avg net per trip, and the model beating its paired random
shadow. The gate measures; the OWNER flips `use_testnet` — after `preflight` and one
`wire_test.py --live` (~$6). Anything benign only on testnet is a mainnet bug: the exit over-sell
of unmanaged balance was exactly that, and exits are now capped at the units this system bought
(`cost_position`; the balance stays the upper bound).

## Development log (newest first)

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
Each broker journals separately (`journal.jsonl` = Binance, `journal.kiwoom.jsonl`), because
observations resolve against the venue's own price source. Not built yet for KR/US: the
scorer's resolution path (Kiwoom chart endpoints) and backfill — decisions journalled now
resolve retroactively once it exists. DART and the KR flow capture remain removed; git history
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
uv run pytest                             # 153 tests, no network (httpx MockTransport)
uv run python scripts/wire_test.py        # dry run; --live sends ONE ~$6 order
uv run pytest tests/test_risk_gate.py -k concentration
uv run ruff check . --fix && uv run ruff format .

uv run python -m trading.preflight        # READ-ONLY pre-live check; run before trading
uv run python -m trading.llm.check        # every LLM tier reachable?
uv run python -m trading.watch --once     # print account status
uv run python -m trading.watch            # serve Telegram commands
uv run python -m trading.agent.scorer     # standalone scoring pass (RAG build step)
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

Still open: the exploration decay schedule (manual by design — lower `explore.entry_pct` toward
`floor_pct` once the corpus says what the model is worth).

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

Untested and next: whether the model beats a random pick from its own shortlist (the
learning loop is accumulating exactly this).

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
