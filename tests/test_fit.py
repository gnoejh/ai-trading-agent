"""The frozen fitted prior: fit offline, read at run time, rank by it on request."""

from __future__ import annotations

import json
import random
from types import SimpleNamespace

import pytest

from trading.agent.fit import FEATURES, ScorerModel, features_of, fit_logistic, train
from trading.config import load_config


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.score.observations = str(tmp_path / "observations.jsonl")
    c.fit.model = str(tmp_path / "model.json")
    c.fit.min_rows = 50
    c.fit.iterations = 25
    return c


def _synthetic(n=600, seed=3):
    """Flow share drives the label; volume and change are noise."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        flow = rng.uniform(-1, 1)
        p = 1 / (1 + pow(2.718281828, -3.0 * flow))
        cleared = rng.random() < p
        ts = f"2026-0{1 + i // 200}-{1 + (i % 28):02d}T00:00:00+00:00"
        oid = f"universe:S{i}:{ts}"
        rows.append(
            {
                "kind": "open",
                "id": oid,
                "source": "universe",
                "symbol": f"S{i}",
                "ts": ts,
                "price": 1.0,
                "book": "CRYPTO",
                "change_pct": rng.uniform(-5, 5),
                "quote_volume": rng.uniform(1e5, 1e8),
                "taker_share": round(flow, 3),
            }
        )
        rows.append(
            {
                "kind": "resolve",
                "id": oid,
                "ts": ts,
                "forward_return_pct": 1.0,
                "cleared_hurdle": cleared,
            }
        )
    return rows


def test_fit_recovers_the_signal_and_writes_a_frozen_artifact(cfg):
    with open(cfg.score.observations, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in _synthetic()) + "\n")
    report = train(cfg)
    assert report["fitted"] and report["n_train"] == 480 and report["n_holdout"] == 120
    assert report["holdout_auc"] > 0.75, "flow drives the label; the fit must find it"
    model = ScorerModel.load(cfg.fit.model)
    assert model is not None
    flow_w = model.weights[FEATURES.index("flow")]
    assert flow_w > 0.5 and abs(model.weights[FEATURES.index("change")]) < flow_w / 3
    high = model.p_clear(
        {"taker_share": 0.9, "quote_volume": 1e6, "change_pct": 0, "book": "CRYPTO"}
    )
    low = model.p_clear(
        {"taker_share": -0.9, "quote_volume": 1e6, "change_pct": 0, "book": "CRYPTO"}
    )
    assert high > 0.75 and low < 0.25
    # Reading a candidate row (screen vocabulary) works too.
    assert model.p_clear({"taker_buy_share": 0.9, "quote_volume": 1e6, "change_pct": 0}) == high


def test_too_few_rows_refuses_to_fit(cfg):
    with open(cfg.score.observations, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in _synthetic(n=20)) + "\n")
    assert not train(cfg)["fitted"]
    assert ScorerModel.load(cfg.fit.model) is None, "no artifact, no prior"


def test_model_picks_are_never_training_rows(cfg):
    rows = _synthetic(n=200)
    for r in rows:
        if r["kind"] == "open":
            r["source"] = "model"
    with open(cfg.score.observations, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    assert not train(cfg)["fitted"], "selected-by-the-model rows are not evidence about features"


def test_artifact_with_a_different_feature_set_is_ignored(cfg, tmp_path):
    (tmp_path / "model.json").write_text(
        json.dumps({"features": ["other"], "weights": [1], "bias": 0, "means": [0], "stds": [1]}),
        encoding="utf-8",
    )
    assert ScorerModel.load(cfg.fit.model) is None


def test_features_are_bounded_and_missing_flow_is_a_flag():
    v = dict(zip(FEATURES, features_of({"change_pct": 900, "quote_volume": -5}), strict=True))
    assert v["change"] == 100 and v["log_volume"] == 0 and v["flow_missing"] == 1 and v["flow"] == 0
    assert all(v[f"book_{b}"] == 0 for b in ("CRYPTO", "BSTOCKS", "KR", "US"))


def test_logistic_fit_is_deterministic():
    x = [[float(i % 3), float(i % 5)] for i in range(60)]
    y = [1 if (i % 3) > (i % 5) / 2 else 0 for i in range(60)]
    assert fit_logistic(x, y, l2=0.5) == fit_logistic(x, y, l2=0.5)


def test_binance_screen_ranks_by_the_prior_when_configured(cfg, tmp_path):
    from trading.brokers.binance.universe import BinanceScreen

    artifact = {
        "features": list(FEATURES),
        "weights": [0.0] * len(FEATURES),
        "bias": 0.0,
        "means": [0.0] * len(FEATURES),
        "stds": [1.0] * len(FEATURES),
        "meta": {},
    }
    artifact["weights"][FEATURES.index("change")] = -1.0  # prefers the SMALLER move
    (tmp_path / "model.json").write_text(json.dumps(artifact), encoding="utf-8")
    cfg.agent.screen.rank_by = "model"
    cfg.agent.screen.use_flow = False
    cfg.agent.screen.min_change_pct = 0.0
    cfg.agent.screen.max_change_pct = 0.0
    cfg.agent.screen.book_slots = {"CRYPTO": 1}
    cfg.agent.screen.min_volume_multiple_of_order = 0
    cfg.agent.screen.book_min_quote_volume = {}
    cfg.agent.screen.min_quote_volume = 0
    rows = [
        {"symbol": "BIGUSDT", "lastPrice": "1", "priceChangePercent": "40", "quoteVolume": "1e6"},
        {"symbol": "SMALLUSDT", "lastPrice": "1", "priceChangePercent": "2", "quoteVolume": "1e6"},
    ]
    client = SimpleNamespace(
        call=lambda name, params=None: SimpleNamespace(body={"rows": rows}),
        trade_plane_symbols=lambda: set(),
    )
    universe = SimpleNamespace(
        symbols={"BIGUSDT", "SMALLUSDT"},
        rules_for=lambda s: None,
        book_of=lambda s: "CRYPTO",
    )
    screen = BinanceScreen(client, universe, cfg)
    assert screen.rank_by() == "model" and screen.prior() is not None
    pool = screen.tradable_pool(0.0)
    assert screen._rank_move(pool)[0]["symbol"] == "SMALLUSDT"
    assert all("p_clear" not in c for c in screen.candidates(0.0)), (
        "the loop stamps p_clear, not the screen"
    )
    # Without an artifact the same config falls back to the flow/change rank.
    cfg.fit.model = str(tmp_path / "missing.json")
    screen = BinanceScreen(client, universe, cfg)
    assert screen.prior() is None
    assert screen._rank_move(pool)[0]["symbol"] == "BIGUSDT"
