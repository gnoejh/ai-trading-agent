"""The 모의투자 flip is scoped per market.

Kiwoom paper trading is the same API on a different endpoint
(mockapi.kiwoom.com) — but it serves KR only. The workbook prints a mock
domain on every page, US included, so the spec index cannot encode that;
`broker.kiwoom.paper_markets` does. These tests pin the seam: with the flip
on (`use_testnet` + `allow_orders`), KR trades paper against the mock host
while US keeps dry_run forced and its reads on the mainnet host — a US agent
following the flip would query a mock host that cannot serve it.
"""

from __future__ import annotations

from run_service import _kiwoom_cfg
from trading.config import KiwoomConfig, load_config


def _kcfg(**overrides) -> KiwoomConfig:
    return KiwoomConfig(**overrides)


def test_flip_off_is_paper_nowhere():
    kcfg = _kcfg(use_testnet=False, allow_orders=False)
    assert not kcfg.paper("KR")
    assert not kcfg.paper("US")


def test_use_testnet_alone_is_not_paper():
    """Measurement config with a stray use_testnet must not arm orders."""
    kcfg = _kcfg(use_testnet=True, allow_orders=False)
    assert not kcfg.paper("KR")


def test_flip_on_is_kr_only_by_default():
    kcfg = _kcfg(use_testnet=True, allow_orders=True)
    assert kcfg.paper("KR")
    assert not kcfg.paper("US"), "모의투자 does not serve US"


def test_paper_markets_governs():
    kcfg = _kcfg(use_testnet=True, allow_orders=True, paper_markets=["KR", "US"])
    assert kcfg.paper("US")


def _flip_cfg(*, use_testnet: bool, allow_orders: bool):
    cfg = load_config()
    cfg.broker.kiwoom.use_testnet = use_testnet
    cfg.broker.kiwoom.allow_orders = allow_orders
    cfg.broker.kiwoom.paper_markets = ["KR"]
    cfg.agent.dry_run = False
    return cfg


def test_service_scopes_the_flip_per_market():
    cfg = _flip_cfg(use_testnet=True, allow_orders=True)

    kr = _kiwoom_cfg(cfg, "KR")
    assert kr.agent.market == "KR"
    assert not kr.agent.dry_run
    assert kr.broker.kiwoom.use_testnet, "KR paper reads the mock account"
    assert kr.broker.kiwoom.allow_orders

    us = _kiwoom_cfg(cfg, "US")
    assert us.agent.market == "US"
    assert us.agent.dry_run, "US stays measurement-only"
    assert not us.broker.kiwoom.use_testnet, "US reads must stay on the mainnet host"
    assert not us.broker.kiwoom.allow_orders

    # The original config is untouched — each market gets its own copy.
    assert cfg.broker.kiwoom.use_testnet
    assert cfg.broker.kiwoom.allow_orders


def test_adapter_hands_its_config_to_the_client():
    """The scoped copy must reach the wire, or the scoping is decoration.

    Found live on the first paper night: `KiwoomClient(Market(market))` with no
    cfg re-read the GLOBAL config and sent US quote traffic to the mock host —
    the per-market scoping existed but nothing carried it to the client.
    """
    from trading.brokers.adapters import KiwoomAdapter

    cfg = _flip_cfg(use_testnet=True, allow_orders=True)

    us = KiwoomAdapter("US", _kiwoom_cfg(cfg, "US"))
    assert us.client.kcfg.use_testnet is False, "US reads must stay on the mainnet host"
    assert us.client.allow_orders is False

    kr = KiwoomAdapter("KR", _kiwoom_cfg(cfg, "KR"))
    assert kr.client.kcfg.use_testnet is True, "KR paper reads the mock host"


def test_service_forces_dry_run_when_flip_is_off():
    cfg = _flip_cfg(use_testnet=False, allow_orders=False)
    for market in ("KR", "US"):
        mcfg = _kiwoom_cfg(cfg, market)
        assert mcfg.agent.dry_run
        assert not mcfg.broker.kiwoom.allow_orders
