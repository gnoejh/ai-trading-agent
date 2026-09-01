"""Exploration-arm and scorer tests.

The exploration arm changes WHO proposes, never what disposes: random entries
must flow through the same sizer, gate and executor as model picks, sample only
the tradable pool, and be journaled so the scorer can grade them. The scorer
turns journal + observations into sample-gated aggregates, and the prompt block
must stay silent until a bucket earns its n.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
from types import SimpleNamespace

import pytest

from trading.accounting.costs import CostLedger
from trading.agent.loop import TradingAgent
from trading.agent.scorer import ExperienceScorer, experience_block
from trading.brokers.state import Snapshot
from trading.config import load_config

POOL = [
    {"symbol": "AAAUSDT", "book": "CRYPTO", "price": 2.0, "change_pct": 1.0, "quote_volume": 9e6},
    {"symbol": "BBBUSDT", "book": "CRYPTO", "price": 5.0, "change_pct": -3.0, "quote_volume": 8e6},
    {"symbol": "CCCUSDT", "book": "CRYPTO", "price": 10.0, "change_pct": 90.0, "quote_volume": 7e6},
]


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.accounting.ledger = str(tmp_path / "ledger.jsonl")
    c.agent.journal = str(tmp_path / "journal.jsonl")
    c.exits.state = str(tmp_path / "exits.json")
    c.risk.kill_switch_file = str(tmp_path / "HALT")
    c.score.enabled = False
    c.score.observations = str(tmp_path / "observations.jsonl")
    c.score.experience = str(tmp_path / "experience.json")
    c.score.min_bucket_n = 10
    c.explore.enabled = True
    c.explore.entry_pct = 1.0  # always attempt: the roll is not under test
    c.explore.max_positions = 4
    c.explore.entries_per_cycle = 1
    c.explore.seed = 7
    c.sizing.mode = "fixed_fraction"
    c.sizing.fraction = 0.04
    c.sizing.max_positions = 6
    return c


def snap(cash=10_000.0):
    return Snapshot(
        market="CRYPTO",
        taken_at=dt.datetime.now(dt.UTC),
        positions={"rows": []},
        cash={"ord_alow_amt": cash, "entr": cash},
        open_orders={},
        evaluation={},
    )


class StubExecutor:
    dry_run = False

    def __init__(self):
        self.executed = []

    def execute(self, verdict):
        self.executed.append(verdict)
        return {"orderId": 1, "executedQty": str(verdict.intent.quantity)}


class StubAdapter:
    market = "BINANCE"

    def __init__(self, pool=POOL):
        self.screen = SimpleNamespace(
            tradable_pool=lambda order_size=0.0: list(pool),
            _flow=lambda symbols: {},
        )
        self.client = None
        self.universe = SimpleNamespace(symbols=set())
        self._executor = StubExecutor()
        self._state = SimpleNamespace(reconcile=lambda **kw: snap(), client=None)

    def state(self):
        return self._state

    def executor(self, gate):
        return self._executor

    def candidates(self, order_size=0.0):
        return []

    def prices(self, symbols):
        return {}

    def rules_for(self, symbol):
        return None

    def holdings(self, snapshot):
        return {}

    def fee_market(self, symbol):
        return "CRYPTO"

    def realised_pnl(self, symbols=None):
        return None


class ApproveAll:
    halted = False

    def evaluate(self, intent):
        return SimpleNamespace(approved=True, reasons=[], intent=intent)

    def evaluate_all(self, intents):
        return [self.evaluate(i) for i in intents]


class DummyTelegram:
    def __init__(self):
        self.sent = []

    def send(self, text, **kw):
        self.sent.append(text)
        return True


def make_agent(cfg, adapter=None):
    agent = TradingAgent(
        cfg, notifier=DummyTelegram(), broker="binance", adapter=adapter or StubAdapter()
    )
    agent.gate = ApproveAll()
    return agent


def observation(holdings=None, candidates=None):
    return {
        "snapshot": snap(),
        "candidates": candidates or [],
        "quotes": {},
        "prices": {},
        "holdings": holdings or {},
        "tradable": [],
    }


# -- exploration arm ----------------------------------------------------------


def test_explore_buys_from_the_pool_through_the_normal_machinery(cfg, tmp_path):
    agent = make_agent(cfg)
    sent = agent.run_explore(observation(), free_slots=3)
    assert sent == 1
    [verdict] = agent.executor.executed
    assert verdict.intent.symbol in {e["symbol"] for e in POOL}
    assert verdict.intent.quantity > 0, "the sizer, not the arm, decides quantity"
    records = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    explore = [r for r in records if r["kind"] == "explore"]
    assert explore and explore[0]["sent"] is True
    assert explore[0]["entry"]["symbol"] == verdict.intent.symbol


def test_explore_batches_distinct_entries_per_cycle(cfg):
    """The fill sprint buys several random entries per cycle -- distinct
    symbols, each through the full sizer/gate/executor path."""
    cfg.explore.entries_per_cycle = 3
    agent = make_agent(cfg)
    assert agent.run_explore(observation(), free_slots=5) == 3
    symbols = [v.intent.symbol for v in agent.executor.executed]
    assert len(set(symbols)) == 3, "no symbol twice in one batch"


def test_explore_never_buys_what_is_already_held(cfg):
    agent = make_agent(cfg, StubAdapter(pool=[POOL[0]]))
    held = {"AAAUSDT": {"quantity": 1, "avg_price": 2.0, "cost_basis": 2.0}}
    assert agent.run_explore(observation(holdings=held), free_slots=3) == 0
    assert agent.executor.executed == []


def test_explore_disabled_is_a_noop(cfg):
    cfg.explore.enabled = False
    agent = make_agent(cfg)
    assert agent.run_explore(observation(), free_slots=3) == 0
    assert agent.executor.executed == []


def test_explore_respects_its_own_position_cap(cfg):
    agent = make_agent(cfg)
    randoms = {f"R{i}USDT" for i in range(cfg.explore.max_positions)}
    agent._random_positions = set(randoms)
    held = {s: {"quantity": 1, "cost_basis": 2.0} for s in randoms}
    assert agent.run_explore(observation(holdings=held), free_slots=3) == 0


def test_shadow_pick_comes_from_the_offered_menu(cfg):
    agent = make_agent(cfg)
    candidates = [{"symbol": s} for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")]
    held = {"AAAUSDT": {"quantity": 1, "cost_basis": 2.0}}
    pick = agent._shadow_pick(observation(holdings=held, candidates=candidates))
    assert pick in {"BBBUSDT", "CCCUSDT"}, "shadow must come from the menu, never a held name"


def test_seed_balances_block_neither_shadow_nor_explore(cfg):
    """Found live 2026-08-31: filtering picks on RAW balances excluded every
    seeded symbol -- 48 decisions produced zero shadow picks, and the random
    arm sampled only coins too new to be seeded. A basis-less balance is not
    a held position."""
    agent = make_agent(cfg)
    seeds = {e["symbol"]: {"quantity": 100, "cost_basis": 0} for e in POOL}
    obs = observation(holdings=seeds, candidates=[{"symbol": e["symbol"]} for e in POOL])
    assert agent._shadow_pick(obs) in {e["symbol"] for e in POOL}
    assert agent.run_explore(obs, free_slots=3) == 1


def test_seed_balances_do_not_consume_position_slots(cfg):
    """Found live 2026-08-30: the slot limit counted the testnet's ~480 seed
    balances as positions (6 - 482 = 0 slots) and silently disabled both entry
    arms. Slots count MANAGED positions -- holdings with a cost basis."""
    seeds = {f"S{i}USDT": {"quantity": 100, "cost_basis": 0} for i in range(482)}
    managed = {"BTCUSDT": {"quantity": 1, "cost_basis": 70000.0}}
    assert TradingAgent._managed_count({**seeds, **managed}) == 1
    assert TradingAgent._managed_count(seeds) == 0


def test_prompt_survives_482_seed_balances(cfg):
    """Found live 2026-08-31: the raw positions snapshot (482 seed rows) blew
    the prompt past its 20k truncation guard and silently cut trade_rules off
    the end -- the model declined every cycle with 'trade_rules not supplied'.
    The prompt carries managed holdings only, contract-first."""
    agent = make_agent(cfg)
    holdings = {f"SEED{i}USDT": {"quantity": 18446, "cost_basis": 0} for i in range(482)}
    holdings["BTCUSDT"] = {"quantity": 0.5, "cost_basis": 70000.0}
    obs = observation(holdings=holdings, candidates=[{"symbol": "AAAUSDT", "price": 2.0}])
    prompt = agent._prompt(obs)
    assert '"trade_rules"' in prompt, "the contract must never be truncated away"
    assert prompt.index('"trade_rules"') < prompt.index('"candidates"'), (
        "contract before detail: overflow must eat detail first"
    )
    assert "BTCUSDT" in prompt and "SEED47USDT" not in prompt
    assert '"unmanaged_balances": 482' in prompt
    assert len(prompt) < 20000


def test_cycle_seams_for_explore_and_scorer():
    """House rule: the wiring is what breaks. Explore must run BEFORE the
    model-only guards (its cheapest cycles are the ones the model skips), and
    the scorer after exits."""
    src = inspect.getsource(TradingAgent.run_cycle)
    assert src.index("self.run_explore(") < src.index('"api budget"')
    assert src.index("self.run_exits(") < src.index("self.scorer.maybe_run()")
    assert "shadow_random" in inspect.getsource(TradingAgent.run_cycle), (
        "the decision record must carry the shadow pick"
    )


# -- scorer -------------------------------------------------------------------


class FakeClient:
    def __init__(self, bars=None):
        self.bars = bars or []
        self.kline_calls = []

    def call(self, name, params=None):
        if name == "klines":
            self.kline_calls.append(params)
            return SimpleNamespace(body={"rows": self.bars})
        return SimpleNamespace(body={"rows": []})


def make_scorer(cfg, bars=None, pool=POOL):
    screen = SimpleNamespace(
        tradable_pool=lambda order_size=0.0: list(pool),
        _flow=lambda symbols: {},
    )
    return ExperienceScorer(FakeClient(bars), screen, CostLedger(cfg), cfg)


def test_scorer_opens_one_observation_per_symbol(cfg):
    scorer = make_scorer(cfg)
    first = scorer.run_once()
    assert first["opened_universe"] == len(POOL)
    second = scorer.run_once()
    assert second["opened_universe"] == 0, "an open observation must not be reopened"


def test_scorer_resolves_after_horizon_and_gates_the_block(cfg, tmp_path):
    opened = (
        dt.datetime.now(dt.UTC) - dt.timedelta(minutes=cfg.score.horizon_minutes + 5)
    ).isoformat()
    obs = {
        "kind": "open",
        "id": f"universe:AAAUSDT:{opened}",
        "source": "universe",
        "symbol": "AAAUSDT",
        "ts": opened,
        "price": 100.0,
        "book": "CRYPTO",
        "change_pct": 1.0,
        "quote_volume": 9e6,
        "taker_share": None,
    }
    (tmp_path / "observations.jsonl").write_text(json.dumps(obs) + "\n", encoding="utf-8")
    bars = [[0, "100", "120", "90", "110", "0"]]
    scorer = make_scorer(cfg, bars=bars, pool=[])
    stats = scorer.run_once()
    assert stats["resolved"] == 1
    data = json.loads((tmp_path / "experience.json").read_text(encoding="utf-8"))
    universe = next(b for b in data["buckets"] if b["label"] == "universe picks")
    assert universe["n"] == 1 and universe["cleared"] == 1  # +10% clears a 0.5% hurdle
    assert experience_block(cfg) is None, "n=1 must stay below the sample gate"
    cfg.score.min_bucket_n = 1
    block = experience_block(cfg)
    assert block and "universe picks" in block["record"]


def test_scorer_opens_model_and_shadow_picks_from_the_journal(cfg, tmp_path):
    ts = dt.datetime.now(dt.UTC).isoformat()
    decision = {
        "ts": ts,
        "kind": "decision",
        "candidates": POOL,
        "shadow_random": "AAAUSDT",
        "verdicts": [
            {"intent": {"symbol": "BBBUSDT", "side": "BUY"}, "approved": True, "reasons": []}
        ],
    }
    (tmp_path / "journal.jsonl").write_text(json.dumps(decision) + "\n", encoding="utf-8")
    scorer = make_scorer(cfg, pool=[])
    stats = scorer.run_once()
    assert stats["opened_journal"] == 2  # one model pick, one shadow pick
    assert scorer.run_once()["opened_journal"] == 0, "journal re-reads must be idempotent"


def test_experience_block_absent_without_a_store(cfg):
    assert experience_block(cfg) is None


def test_closed_trades_pair_fifo_and_skip_orphans_and_old_epochs(cfg, tmp_path):
    """Ledger prices are data-plane (mainnet) reference prices, so round trips
    paired from the ledger are mark-to-mainnet with no extra network. Orphan
    sells (seed liquidations) and pre-epoch trades must contribute nothing."""
    cfg.score.trade_since = "2026-08-30"
    trades = [
        # Pre-epoch round trip: excluded entirely.
        {
            "ts": "2026-08-11T01:00:00",
            "kind": "trade",
            "symbol": "OLDUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "market": "CRYPTO",
        },
        {
            "ts": "2026-08-11T02:00:00",
            "kind": "trade",
            "symbol": "OLDUSDT",
            "side": "SELL",
            "quantity": 1,
            "price": 200,
            "market": "CRYPTO",
        },
        # Orphan sell (seed balance liquidation): pairs with nothing.
        {
            "ts": "2026-08-30T12:00:00",
            "kind": "trade",
            "symbol": "TUTUSDT",
            "side": "SELL",
            "quantity": 18446,
            "price": 0.035,
            "market": "CRYPTO",
        },
        # A real testnet round trip: +10% clears the 0.5% crypto hurdle.
        {
            "ts": "2026-08-30T13:00:00",
            "kind": "trade",
            "symbol": "AAAUSDT",
            "side": "BUY",
            "quantity": 10,
            "price": 100,
            "market": "CRYPTO",
        },
        {
            "ts": "2026-08-30T14:00:00",
            "kind": "trade",
            "symbol": "AAAUSDT",
            "side": "SELL",
            "quantity": 10,
            "price": 110,
            "market": "CRYPTO",
        },
    ]
    (tmp_path / "ledger.jsonl").write_text(
        "\n".join(json.dumps(t) for t in trades) + "\n", encoding="utf-8"
    )
    scorer = make_scorer(cfg, pool=[])
    outcomes = scorer._trade_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0]["symbol"] == "AAAUSDT"
    assert outcomes[0]["cleared_hurdle"] is True
    assert outcomes[0]["forward_return_pct"] == pytest.approx(10.0)


# -- virtual picks (the mainnet gate's evidence accelerator) ------------------


def test_parse_extracts_best_candidate_from_the_menu(cfg):
    """The virtual pick obeys the same anti-hallucination rule as intents:
    a symbol the model was not shown is discarded, never scored."""
    from types import SimpleNamespace

    from trading.agent.loop import TradingAgent

    stub = SimpleNamespace(market="BINANCE")
    raw = (
        '{"intents": [], "best_candidate": {"symbol": "BBBUSDT", "confidence": 0.4},'
        ' "commentary": "declining"}'
    )
    intents, commentary, best = TradingAgent._parse(stub, raw, {"AAAUSDT", "BBBUSDT"}, {})
    assert intents == [] and best == "BBBUSDT"

    hallucinated = raw.replace("BBBUSDT", "EVILUSDT")
    _, _, best = TradingAgent._parse(stub, hallucinated, {"AAAUSDT", "BBBUSDT"}, {})
    assert best is None, "an unoffered best_candidate must be dropped"


def test_scorer_opens_the_virtual_pick_on_a_decline(cfg, tmp_path):
    """A decline used to contribute nothing to model-vs-random; the virtual
    pick makes every decision a paired measurement."""
    ts = dt.datetime.now(dt.UTC).isoformat()
    decision = {
        "ts": ts,
        "kind": "decision",
        "candidates": POOL,
        "shadow_random": "AAAUSDT",
        "virtual_pick": "BBBUSDT",
        "verdicts": [],  # the model declined to trade
    }
    (tmp_path / "journal.jsonl").write_text(json.dumps(decision) + "\n", encoding="utf-8")
    scorer = make_scorer(cfg, pool=[])
    stats = scorer.run_once()
    assert stats["opened_journal"] == 2, "virtual pick + shadow, even with no trade"
    opened = [
        json.loads(line) for line in (tmp_path / "observations.jsonl").read_text().splitlines()
    ]
    sources = {(r["source"], r["symbol"]) for r in opened if r["kind"] == "open"}
    assert ("model", "BBBUSDT") in sources and ("shadow", "AAAUSDT") in sources


def test_cycle_journals_the_virtual_pick():
    import inspect

    from trading.agent.loop import TradingAgent

    assert "virtual_pick" in inspect.getsource(TradingAgent.run_cycle), (
        "the decision record must carry the model's virtual pick"
    )
