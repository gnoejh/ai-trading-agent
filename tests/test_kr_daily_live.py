"""Live KR daily features from the archive, and per-venue priors in the prompt.

The Kiwoom screen must attach the SAME daily definitions the KR replay
validated, from archive bars, with no broker call; a name without bars carries
None and a missing benchmark skips features for everyone. And a KR prompt must
read KR's replay, never crypto's -- the sign of ret_5d is opposite between
them, so showing the wrong venue's prior would be worse than none.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from trading.agent.backfill_features_kr import DBar
from trading.agent.features import DAILY_PATH_KEYS, DAILY_RS_KEYS


def _bars(n, step):
    return [
        DBar(i * 86_400_000, 100 + i * step, 101 + i * step, 99 + i * step, 100 + i * step, 1000.0)
        for i in range(n)
    ]


def _screen(monkeypatch, tmp_path, bars_by_symbol):
    import trading.agent.backfill_features_kr as bf
    from trading.agent.universe import Screen
    from trading.config import load_config

    monkeypatch.setattr(
        bf, "_daily", lambda root, venue_dir, symbol: bars_by_symbol.get(symbol, [])
    )
    cfg = load_config()
    cfg.score.kiwoom_archive = str(tmp_path)
    cfg.agent.screen.market("KR").daily_features = True
    screen = Screen.__new__(Screen)
    screen.cfg = cfg
    screen.market = "KR"
    return screen


def test_daily_features_and_regime_are_attached_from_archive_bars(monkeypatch, tmp_path):
    bars = {"069500": _bars(40, 0.1), "STRONG": _bars(40, 1.0), "WEAK": _bars(40, -0.5)}
    screen = _screen(monkeypatch, tmp_path, bars)
    selected = [{"symbol": "STRONG"}, {"symbol": "WEAK"}, {"symbol": "NOBARS"}]
    screen._attach_daily_features(selected)
    strong, weak, nobars = selected
    assert set(DAILY_PATH_KEYS + DAILY_RS_KEYS) <= set(strong)
    assert strong["ret_5d"] > 0 > weak["ret_5d"]
    assert strong["rs_5d"] > 0 > weak["rs_5d"]
    assert all(nobars[k] is None for k in DAILY_PATH_KEYS + DAILY_RS_KEYS)
    assert strong["market_state"]["bench_ret_5d"] > 0 and "breadth_5d" in strong["market_state"]


def test_a_missing_benchmark_skips_everyone(monkeypatch, tmp_path):
    screen = _screen(monkeypatch, tmp_path, {"STRONG": _bars(40, 1.0)})
    selected = [{"symbol": "STRONG"}]
    screen._attach_daily_features(selected)
    assert all(selected[0][k] is None for k in DAILY_PATH_KEYS + DAILY_RS_KEYS)
    assert "market_state" not in selected[0]


def test_kr_prompt_reads_kr_priors_not_cryptos(tmp_path):
    from trading.agent.scorer import experience_block
    from trading.config import load_config

    cfg = load_config()
    cfg.score.experience = str(tmp_path / "experience.json")
    cfg.score.feature_replay_output = str(tmp_path / "feature_replay.json")
    (tmp_path / "experience.json").write_text(json.dumps({"buckets": [], "calibration": []}))
    crypto = {
        "deciles": [
            {
                "feature": "range_pos_7d",
                "ci_low": 1.0,
                "ci_high": 3.0,
                "top_decile_excess_pct": 1.6,
                "bottom_decile_excess_pct": -0.7,
                "n": 12290,
            }
        ],
        "regime": [],
    }
    kr = {
        "deciles": [
            {
                "feature": "ret_5d",
                "ci_low": -1.85,
                "ci_high": -0.55,
                "top_decile_excess_pct": 0.1,
                "bottom_decile_excess_pct": 1.29,
                "n": 21837,
            }
        ],
        "regime": [],
    }
    (tmp_path / "feature_replay.json").write_text(json.dumps(crypto))
    (tmp_path / "feature_replay_kr.json").write_text(json.dumps(kr))
    b_kr = experience_block(cfg, venue="KR")["backtest_priors"]
    b_bn = experience_block(cfg, venue="BINANCE")["backtest_priors"]
    assert list(b_kr) == ["backtest decile ret_5d"]
    assert list(b_bn) == ["backtest decile range_pos_7d"]


def test_the_kr_flag_is_on_and_the_arms_are_registered():
    from trading.agent.scorer import SELECTORS
    from trading.config import load_config

    cfg = load_config()
    assert cfg.agent.screen.market("KR").daily_features is True
    for arm in ("ret_5d_low", "ret_5d_top", "range20_low"):
        assert arm in SELECTORS and arm in cfg.score.arms
    assert SimpleNamespace  # keep the import honest for the stub above
