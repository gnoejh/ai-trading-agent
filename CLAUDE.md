# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Invariants

These are decisions, not preferences. Violating one is a bug even if the code runs.

1. **Broker records are the single source of truth.** Positions, cash and open orders come from a
   live broker read, never from locally accumulated state. `AccountState` has no setters by design.
   Anything persisted locally is a derived cache for audit and analytics; if it disagrees with the
   broker, the broker is right. `state.reconcile_before_order` forces a fresh read on the order path.
2. **No hardcoded parameters.** Tunables live in `config.yaml` and are read through `trading.config`.
   If you are about to write a literal timeout, limit, API id or model name into a module, add it to
   the YAML instead. `.env` holds secrets only and is never mirrored into the YAML.
3. **KR and US are separate markets.** Kiwoom exposes them as distinct API surfaces sharing only a
   host and the OAuth token — different URL trees (`/api/dostk/*` vs `/api/us/*`), different realtime
   endpoints, separate entitlements. `Market` is an enum, a client is bound to exactly one, and a
   cross-market API id raises. Never widen this to a string filter.
4. **The model proposes, deterministic code disposes.** An LLM must never be the last thing before an
   order. Order endpoints are refused unless `allow_orders` is explicitly enabled, and the risk gate
   belongs in front of that switch.

## Economics — the thing that decides whether this works

Profit is `realised P&L − trading fees − API spend`. All three are recorded in `data/ledger.jsonl`
by `trading/accounting/costs.py`, so break-even is measured, not assumed (`/costs` on Telegram, or
`CostLedger.breakeven()`).

At the configured KR rates a **round trip costs ~0.28% of notional** (0.015% commission each side,
0.15% 거래세 on the sell, 5bps assumed slippage each side). That is the hurdle every trade must clear
before it earns anything, and it is unaffected by model quality. API spend adds a fixed daily floor
that matters disproportionately at this account size (~4.8M KRW equity). When tuning `max_orders_per_day`,
remember each order is a fixed drag: 6 orders/day is already ~0.6% of traded notional in costs.

Model prices in `llm.pricing` are estimates — **verify them against the providers' pricing pages**,
because every break-even figure derives from them. Unknown models are billed at zero and warn.

## Commands

```
uv sync                                   # create/refresh .venv from uv.lock
uv run pytest                             # 146 tests, no network (httpx MockTransport)
uv run python scripts/wire_test.py        # dry run; --live sends ONE ~$6 order
uv run pytest tests/test_risk_gate.py -k concentration
uv run ruff check . --fix && uv run ruff format .

uv run python -m trading.preflight        # READ-ONLY pre-live check; run before trading
uv run python -m trading.llm.check        # every LLM tier reachable?
uv run python -m trading.watch --once     # print account status
uv run python -m trading.watch            # serve Telegram commands
uv run python -m trading.rag.build_index  # rebuild data/specs/kiwoom.json from the workbook
```

Tests must stay hermetic: fixtures pin `use_testnet`, `allow_orders` and the risk limits rather than
inheriting `config.yaml`, so a live-config change can never silently alter what a test asserts.

On Windows the console is cp949 and the corpus is Korean: prefix with `PYTHONUTF8=1` or output is
mojibake. `tzdata` is a real dependency, not incidental — Windows ships no zoneinfo database.

## Architecture

**RAG runs at build time, not on the trading hot path.** The vendor workbooks are perfectly regular,
so spec extraction is deterministic parsing with no model involved (`rag/spec_parser.py`). Retrieval
is two-stage: `catalog_prompt(market)` renders all ~200 APIs for one market in ~2.2k tokens so the
model picks ids by reading the menu, then `get(api_id)` is a dict lookup. There is no embedding
similarity anywhere, so there is no recall risk on the field tables. `SpecRouter` discards any id the
model returns that is not in the market's catalog.

**One client covers ~300 endpoints.** Every Kiwoom REST call is `POST {domain}{url}` with `api-id` in
the header and `cont-yn`/`next-key` for continuation, so `KiwoomClient.call()` takes the URL, required
fields and field lengths from the parsed spec rather than from hand-written wrappers. Request bodies
are validated against the spec *before* the network call.

**Telegram is the operator surface** (`@hjeong_trading_agent_bot`, token `TRADING_AGENT_BOT_TOKEN`).
`trading/watch.py` serves `/status`, `/positions`, `/cash`, `/halt`, `/resume` and can push reports on
an interval. Inbound chat is untrusted: `poll()` drops any update whose chat id is not in
`TELEGRAM_ALLOWED_IDS` — dropped silently, and the offset still advances so a stranger cannot wedge
the loop. The watcher constructs its client with `allow_orders=False`; monitoring must not trade.
Status labels are pulled from the parsed spec's Korean field names, so reports survive schema changes.

**Layout** — `trading/rag/` parse, store, route; `trading/brokers/kiwoom/` client + `AccountState`;
`trading/notify/` Telegram + status rendering; `trading/llm/` provider access; `trading/config.py`
YAML + `.env`. Not built yet: the risk gate, market-data ingest, and the realtime websocket client.

## Exits are derived from the cost hurdle

This is the system's own organising idea, and the exit levels follow from it rather than from
R-multiples off an arbitrary stop (`trading/risk/exits.py`):

- **A position is not flat at its entry price.** Net break-even is entry × (1 + round-trip rate) plus
  that position's share of API spend. Selling above entry but below that is a loss, and the policy
  refuses to call it a win.
- **Reward:risk is guaranteed, not hoped for.** A hurdle-only target is dangerous — with a 3% stop and
  a 0.28% hurdle, a 4× target is +1.1% against −3%, a 0.34 ratio needing a ~75% hit rate. `min_reward_risk`
  widens the target so risk and reward scale together (currently ~+6.8% vs −3.0%, ratio 2.0, break-even
  win rate 33%).
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
  of the KR default; the live reproduction was `0.2800%` vs `0.6000%`.
- **The daily-loss cap no longer applies to sells.** A breached loss cap stops new risk, but does not trap
  an open position that must be closed.
- **`BinanceAdapter.holdings()` now populates `avg_price` from `cost_basis`**, not the live mark. A stop that
  trails downward cannot fire if it is computed from the falling price on each cycle.

The tests cover the seam, not only the helper in isolation. `tests/test_exits.py` checks that `run_cycle`
invokes `run_exits` before the halt check and that Binance exits use the Binance hurdle; `tests/test_risk_gate.py`
asserts that a breached loss cap still permits an exit; `tests/test_binance.py` asserts the cost-basis-based
`avg_price` path.

Cost of learning this: **$436 on TUTUSDT**, closed 2026-08-12 at −13.7% against an 8% stop.

## Sessions: KR then US, same process

Sessions are declared per market in **the exchange's own timezone** (`agent.sessions`), so DST is
resolved by the zone database rather than by fixed KST offsets — the US open is 23:30 KST under EST
but 22:30 under EDT, and hardcoding either would trade an hour wrong for half the year. KR runs
09:00–15:20 Asia/Seoul, US 09:30–15:55 America/New_York. `AgentConfig.open_market()` returns whichever
is trading; the two never overlap.

## The universe funnel

The universe is the whole market, fetched from the broker (`ka10099`), never a hand-kept list.
`ka10099` returns **4,293 KR rows**: 거래소 917 + 코스닥 1,820 common stocks, plus ETF 1,160 / ETN 361 /
리츠 23, which are different instruments. After filtering to common stock with `auditInfo == 정상`,
the tradable universe is **~2,483**. That is far too many to quote or prompt, so every cycle runs

    universe (~2,483) → broker ranking screens → top N candidates (25) → model → risk gate → broker

Screening is deterministic and happens **before** any model call, which bounds cost: context size is
a function of `screen.candidates`, not market size. The model only ever sees the shortlist plus
current holdings, and `_parse` discards any symbol it was not offered — that is what stops a
hallucinated ticker from reaching the gate.

## Credentials status (verified 2026-08-10)

`KIWOOM_TESTNET_APP_KEY` / `KIWOOM_TESTNET_SECRET_KEY` **do not authenticate** — both hosts return
`8001 App Key와 Secret Key 검증에 실패했습니다`. The mainnet pair works, but only against
`api.kiwoom.com`; `mockapi.kiwoom.com` correctly rejects it with `8030 투자구분이 달라` (wrong
investment type). So with `use_testnet: true` the agent cannot authenticate at all, and paper trading
is unavailable until 모의투자 keys are reissued at the Kiwoom developer portal.

Kiwoom returns auth failures as **HTTP 200 with a non-zero `return_code`**, so `raise_for_status()`
does not catch them; `_issue_token` checks the body explicitly.

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

## The workbooks

`KIWOOM_API.xlsx` is the authoritative endpoint reference — consult it (or the parsed index) rather
than hand-writing request models. Sheet 0 is the catalog; each of the other 338 documents one API.
Specs are keyed by the `API ID` inside the sheet, **not** by sheet name: REST sheets are named by bare
id (`ka10001`) but realtime sheets are named `이름(id)` (`주문체결(00)`), and one substitutes `|` for `/`.
The error-code table has no API ID and is reached via the `공통` alias. Realtime types are identified
by a `wss://` domain, not by id shape.

`KIS_API.xlsx` is the same format but keys its sheets by Korean API name, so reusing the parser for
KIS needs a name-based resolver. `KIWOOM_API.pdf` covers the same surface in prose.

## Research findings (2026-08-10) — read before changing the strategy

Measured, not assumed. Each test invalidated the previous one's optimism, so the
methodology notes matter as much as the numbers.

**Price momentum has no predictive power here.** Over 1,000 daily bars on 10 majors,
the forward-5-day return spread between the top and bottom decile of 3-day price change
was **−0.06%** — noise. The original screen ranked on exactly this.

**Order flow does.** The same test on 5-day average taker-buy share (kline field 9 /
field 5) gave a **+1.05% spread** (+1.58% top decile vs +0.53% bottom) — roughly twice
the round-trip cost. The Binance screen now ranks on this (`screen.use_flow`).

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

Untested and next: whether the model beats a random pick from its own shortlist, and
whether the KR flow signals (외국인/기관 순매수, 거래원별 매매, 프로그램매매) — all already
parsed in the spec index, all free — carry the same edge as taker flow does on Binance.

## Two brokers, one agent

`TradingAgent(broker="kiwoom"|"binance")` builds a `BrokerAdapter`
(`trading/brokers/adapters.py`). Everything downstream — gate, sizer, exit supervisor, cost ledger —
is venue-agnostic; the adapter absorbs what differs: Kiwoom quotes one symbol per call while Binance
returns all prices in one, KRX trades whole shares while Binance quantises per symbol, KR has a
session while crypto does not.

Kiwoom and Binance are **separate accounts and separate agents**. Run them as two services:
`--broker kiwoom` / `--broker binance`.

- **Binance is two books over one balance.** CRYPTO (423) and BSTOCKS (66) share one USDT wallet, so
  they are one agent choosing across the union, never two racing for the same cash. bStocks are
  identified by the `TRD_GRP_261` permission tag — a symbol-suffix rule wrongly matches BNB/SHIB/ARB.
- **The book decides the hurdle**, not the agent: crypto 0.500%, bStocks 0.600%, KR 0.280%, US 0.130%.
  `adapter.fee_market(symbol)` resolves it per symbol.
- **The kill switch is per venue.** `data/HALT.<MARKET>` halts one; bare `data/HALT` halts everything.
- **The gate validates against the market it was built for**, not `agent.market` — that config field
  still reads `KR` while a Binance gate is `BINANCE`, and comparing to it rejected every Binance order.

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
a ₩1M deposit as a spectacular gain and a withdrawal as a catastrophic loss. Performance is
realised P&L (broker-reported) minus fees minus API spend — which is flow-invariant.
Flows are journalled separately via `CostLedger.record_cash_flow` and are excluded from
every P&L figure; use them only to build a time-weighted return if one is ever needed.

## The reasoner returns empty content — check this before diagnosing "no trade"

`deepseek-reasoner` (the `deep` tier) can spend its entire budget on reasoning and
return **zero tokens of content**. Reproduced 2026-08-10 on a ~4,000-char prompt:

    content len   :      0
    reasoning len : 29,751     (3.6x the configured max_tokens)
    max_tokens    :  8,192

`LLMClient.ask` returns `content or ""`, `_parse` finds no JSON, one warning is
logged, and the cycle reports **0 intents**. That is indistinguishable in the
journal from a considered decision not to trade — so some declines attributed to
judgment were actually truncation.

An agent that silently reports "no trade" when it never received an answer is
worse than one that errors. Empty content WITH non-empty `reasoning_content` is a
distinct, detectable state: log it as truncation and retry on another tier, never
fold it into a decision. `fast` (deepseek-chat) answered the same prompts cleanly
and far cheaper; it is a serious candidate for the decide tier.

## DART filing polarity — read the verb, not the noun

Korean filings invert meaning on a suffix: 체결(conclude) vs 해지(terminate),
취득(acquire) vs 처분(dispose), 결정(decide) vs 철회(withdraw). An earlier keyword
map scored `자기주식취득신탁계약해지` — a buyback ENDING — as bullish, because it
matched on `자기주식취득`. Inverted forms must be matched BEFORE their base forms;
`CATALYSTS` in `trading/info/dart.py` is an ordered list for exactly that reason.

Ownership filings (주식등의대량보유상황보고서, 임원ㆍ주요주주…, 최대주주변경) are
tagged NEU and never signed: the direction is in the document body, not the title.
A 대량보유 filing covers accumulation and disposal alike.

The classification was produced by asking the trader model rather than by hand —
it caught the 해지/체결 inversion and refused to sign the ownership classes, both
of which a string-matching approach got wrong.
