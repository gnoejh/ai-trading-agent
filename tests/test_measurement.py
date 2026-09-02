"""Measurement decoupled from execution, and the KR screen on its measured signal.

Seams pinned here (each was a defect seen live on the first epoch day):

* A FULL book still asks the model and journals the virtual + shadow picks;
  only execution is withheld (62 "no free slots" cycles produced zero pairs).
* The random arm never runs on a dry-run venue and honours `explore.markets`.
* The Kiwoom screen reads multi-column flow rankings, quotes the names it
  learns that way, caps the 24h change, measures each candidate's net-buy
  share and orders by it — and exposes a strategy-free tradable pool so the
  KR paper account can explore.
* Candidates carry the frozen prior's `p_clear`; the decision record carries
  the virtual pick's stated confidence.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.test_explore import POOL, ApproveAll, DummyTelegram, StubAdapter, observation
from trading.agent.loop import TradingAgent
from trading.agent.universe import Screen
from trading.config import FlowFeature, Ranker, RankerColumn, ScreenMarket, load_config


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
    c.fit.model = str(tmp_path / "no_model.json")
    c.explore.enabled = True
    c.explore.entry_pct = 1.0
    c.explore.seed = 7
    c.explore.markets = []
    c.sizing.mode = "fixed_fraction"
    c.sizing.fraction = 0.04
    c.sizing.max_positions = 2
    c.agent.skip_decide_if_unchanged = False
    c.accounting.max_api_krw_per_day = 0
    return c


class ScriptedLLM:
    """Answers every decide with a fixed reply; counts the calls."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def ask(self, prompt, *, system=None, tier=None):
        self.calls += 1
        return self.reply


def _agent(cfg, adapter=None):
    agent = TradingAgent(
        cfg, notifier=DummyTelegram(), broker="binance", adapter=adapter or StubAdapter()
    )
    agent.gate = ApproveAll()
    return agent


def _journal(cfg):
    return [json.loads(x) for x in open(cfg.agent.journal, encoding="utf-8")]


REPLY = (
    '{"intents": [{"side": "BUY", "symbol": "AAAUSDT", "quantity": 0, "limit_price": null,'
    ' "reason": "r", "confidence": 0.61}],'
    ' "best_candidate": {"symbol": "BBBUSDT", "confidence": 0.52}, "commentary": "c"}'
)


class MenuAdapter(StubAdapter):
    """A stub whose screen offers POOL as the menu and reports two managed holdings."""

    def __init__(self, held: int):
        super().__init__()
        self._held = held

    def candidates(self, order_size=0.0):
        return [dict(e) for e in POOL]

    def holdings(self, snapshot):
        return {f"H{i}USDT": {"quantity": 1, "cost_basis": 1.0} for i in range(self._held)}

    def prices(self, symbols):
        return {s: 1.0 for s in symbols}


def test_full_book_still_measures_but_never_executes(cfg):
    """Every slot taken: the model is still asked, the virtual and shadow
    picks are journalled with the stated confidence, no explore, no order."""
    adapter = MenuAdapter(held=cfg.sizing.max_positions)
    agent = _agent(cfg, adapter)
    agent.llm = ScriptedLLM(REPLY)
    result = agent.run_cycle()
    assert agent.llm.calls == 1, "a full book must not skip the question"
    assert result.sent == 0 and adapter._executor.executed == []
    records = _journal(cfg)
    assert not [r for r in records if r["kind"] == "cycle_skipped"]
    [decision] = [r for r in records if r["kind"] == "decision"]
    assert decision["virtual_pick"] == "BBBUSDT"
    assert decision["virtual_confidence"] == 0.52
    assert decision["free_slots"] == 0 and decision["market"] == "BINANCE"
    assert decision["shadow_random"] in {e["symbol"] for e in POOL}
    [verdict] = decision["verdicts"]
    assert not verdict["approved"] and "measurement only" in verdict["reasons"][0]
    assert not [r for r in records if r["kind"] == "explore"], "no slot, no random entry"


def test_full_book_skip_is_still_available_by_config(cfg):
    cfg.agent.decide_when_full = False
    agent = _agent(cfg, MenuAdapter(held=cfg.sizing.max_positions))
    agent.llm = ScriptedLLM(REPLY)
    agent.run_cycle()
    assert agent.llm.calls == 0
    assert [r["reason"] for r in _journal(cfg) if r["kind"] == "cycle_skipped"] == ["no free slots"]


def test_free_slot_executes_as_before(cfg):
    adapter = MenuAdapter(held=0)
    agent = _agent(cfg, adapter)
    agent.llm = ScriptedLLM(REPLY)
    result = agent.run_cycle()
    assert result.approved == 1
    symbols = [v.intent.symbol for v in adapter._executor.executed]
    assert "AAAUSDT" in symbols, "with room, the approved intent is sent"


def test_explore_never_runs_on_a_dry_run_venue(cfg):
    agent = _agent(cfg)
    agent.executor.dry_run = True
    assert agent.run_explore(observation(), free_slots=3) == 0
    assert agent.executor.executed == []


def test_explore_honours_the_venue_list(cfg):
    cfg.explore.markets = ["KR"]
    agent = _agent(cfg)
    assert agent.run_explore(observation(), free_slots=3) == 0
    cfg.explore.markets = ["KR", "BINANCE"]
    assert agent.run_explore(observation(), free_slots=3) == 1


# -- the KR screen -------------------------------------------------------------


class FakeKiwoom:
    """Answers the three KR endpoints the screen uses."""

    def __init__(self):
        self.calls = []
        self.store = SimpleNamespace(get=lambda api_id: SimpleNamespace(required_body=list))
        self.market = "KR"

    def call(self, api_id, body):
        self.calls.append((api_id, dict(body)))
        match api_id:
            case "ka10032":  # turnover ranking: one conventional list
                rows = [
                    {
                        "stk_cd": "005930",
                        "stk_nm": "삼성전자",
                        "cur_prc": "-70000",
                        "flu_rt": "-1.20",
                        "now_trde_qty": "1000000",
                        "trde_prica": "70000",
                    },
                    {
                        "stk_cd": "000660",
                        "stk_nm": "SK하이닉스",
                        "cur_prc": "+200000",
                        "flu_rt": "+2.50",
                        "now_trde_qty": "500000",
                        "trde_prica": "100000",
                    },
                    {
                        "stk_cd": "999999",
                        "stk_nm": "급등주",
                        "cur_prc": "+5000",
                        "flu_rt": "+23.00",
                        "now_trde_qty": "9000000",
                        "trde_prica": "45000",
                    },
                ]
                return SimpleNamespace(body={"trde_prica_upper": rows})
            case "ka90009":  # four rankings per row
                rows = [
                    {
                        "for_netprps_stk_cd": "035420",
                        "for_netprps_stk_nm": "NAVER",
                        "for_netprps_amt": "+300",
                        "orgn_netprps_stk_cd": "005930",
                        "orgn_netprps_stk_nm": "삼성전자",
                        "orgn_netprps_amt": "+250",
                        "for_netslmt_stk_cd": "000660",
                        "orgn_netslmt_stk_cd": "000660",
                    },
                ]
                return SimpleNamespace(body={"frgnr_orgn_trde_upper": rows})
            case "ka10001":  # quote
                code = body["stk_cd"]
                return SimpleNamespace(
                    body={
                        "cur_prc": "+180000" if code == "035420" else "0",
                        "flu_rt": "+0.80",
                        "trde_qty": "400000",
                    }
                )
            case "ka10061":  # investor totals over the window
                code = body["stk_cd"]
                net = {
                    "035420": ("100", "50", "-150"),
                    "005930": ("-40", "-10", "50"),
                    "000660": ("-100", "-100", "100"),
                }[code]
                frg, org, ind = net
                return SimpleNamespace(
                    body={
                        "stk_invsr_orgn_tot": [{"frgnr_invsr": frg, "orgn": org, "ind_invsr": ind}]
                    }
                )
        raise AssertionError(api_id)


def _kr_screen(cfg, client):
    cfg.agent.screen.candidates = 25
    cfg.agent.screen.KR = ScreenMarket(
        min_price=1000,
        max_change_pct=0.10,
        rank_by="flow",
        rankers=[
            Ranker(api_id="ka10032", params={}),
            Ranker(
                api_id="ka90009",
                params={},
                columns=[
                    RankerColumn(
                        code="for_netprps_stk_cd",
                        name="for_netprps_stk_nm",
                        amount="for_netprps_amt",
                    ),
                    RankerColumn(
                        code="orgn_netprps_stk_cd",
                        name="orgn_netprps_stk_nm",
                        amount="orgn_netprps_amt",
                    ),
                ],
            ),
        ],
        pool_rankers=[Ranker(api_id="ka10032", params={})],
        flow=FlowFeature(api_id="ka10061", params={}, lookback_days=7),
    )
    universe = SimpleNamespace(
        market="KR",
        codes={"005930", "000660", "035420", "999999"},
        name_of=lambda code: "",
        exchange_of=lambda code: "",
    )
    return Screen(client, universe, cfg)


def test_kr_screen_ranks_on_net_buy_flow_and_caps_the_change(cfg):
    client = FakeKiwoom()
    screen = _kr_screen(cfg, client)
    menu = screen.candidates()
    symbols = [c["symbol"] for c in menu]
    assert "999999" not in symbols, "a +23% mover is over the 10% cap"
    assert "035420" in symbols, "a name known only from the flow ranking is quoted in"
    naver = next(c for c in menu if c["symbol"] == "035420")
    assert naver["price"] == 180000.0 and naver["book"] == "KR"
    # Net-buy shares: NAVER (100+50)/300 = +0.5, 삼성 (-40-10)/100 = -0.5,
    # 하이닉스 (-100-100)/300 = -0.67.
    assert symbols == ["035420", "005930", "000660"], "ordered by flow share"
    assert naver["taker_buy_share"] == pytest.approx(0.5)
    assert next(c for c in menu if c["symbol"] == "005930")["net_buy_amount"] == 250.0
    assert all("quote_volume" in c for c in menu)


def test_kr_quotes_are_cached_for_the_observe_step(cfg):
    client = FakeKiwoom()
    screen = _kr_screen(cfg, client)
    screen.candidates()
    quotes_before = sum(1 for a, _ in client.calls if a == "ka10001")
    assert screen.quote("035420")["price"] == 180000.0
    assert sum(1 for a, _ in client.calls if a == "ka10001") == quotes_before, "served from cache"


def test_kr_tradable_pool_has_no_strategy_bounds(cfg):
    screen = _kr_screen(cfg, FakeKiwoom())
    pool = {e["symbol"]: e for e in screen.tradable_pool(0.0)}
    assert "999999" in pool, "the random arm samples the +23% mover too"
    assert pool["999999"]["book"] == "KR" and pool["999999"]["price"] == 5000.0
    assert pool["005930"]["quote_volume"] == pytest.approx(70000.0 * 1_000_000)


# -- the fitted prior at the seam ------------------------------------------------


def test_candidates_carry_the_prior_when_an_artifact_exists(cfg, tmp_path):
    from trading.agent.fit import FEATURES

    artifact = {
        "features": list(FEATURES),
        "weights": [0.0] * len(FEATURES),
        "bias": 0.0,
        "means": [0.0] * len(FEATURES),
        "stds": [1.0] * len(FEATURES),
        "meta": {"n_train": 10, "fitted_at": "2026-09-03T00:00:00+00:00", "holdout_auc": 0.5},
    }
    path = tmp_path / "model.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    cfg.fit.model = str(path)
    agent = _agent(cfg, MenuAdapter(held=0))
    obs = agent.observe()
    assert all(c["p_clear"] == pytest.approx(0.5) for c in obs["candidates"])
    prompt = agent._prompt(obs)
    assert '"fitted_prior"' in prompt and '"p_clear"' in prompt
