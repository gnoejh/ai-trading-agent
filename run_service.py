"""Service entry point: one process, every venue.

    uv run python run_service.py

* **Binance** trades continuously on the measurement regime (testnet).
* **Kiwoom KR and US** run MEASUREMENT-ONLY cycles while their own session is
  open — decisions, virtual and shadow picks, no orders (dry_run is forced and
  `broker.kiwoom.allow_orders` is false). Outside their sessions the Kiwoom
  agents are never even constructed, and no Kiwoom API is touched: the
  ai-trading-history downloader owns the single Kiwoom OAuth token after hours.
* **Operator control.** Serves `/status`, `/costs`, `/halt`, `/resume` between
  cycles, covering all three markets.

Installed as the `trading-agent` service (install_nssm_service.ps1). The old
`trading-agent-binance` name and its `--broker binance` argument are accepted
for compatibility and mean the same thing: run everything.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from trading.agent.loop import TradingAgent
from trading.config import config
from trading.notify.telegram import TelegramNotifier
from trading.watch import build_watcher

log = logging.getLogger("service")

IDLE_POLL_S = 20.0
KIWOOM_MARKETS = ("KR", "US")


def restart_requested(cfg) -> bool:
    """Consume the restart sentinel, if present.

    Returning True asks main() to exit; NSSM then restarts the process with the
    config re-read from disk. The file is deleted BEFORE exiting so a failed
    restart cannot leave the service in a reboot loop.
    """
    path = Path(cfg.service.restart_file)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError as exc:
        log.error("could not clear %s (%s); refusing to restart-loop", path, exc)
        return False
    return True


def _kiwoom_agent(cfg, market: str, telegram) -> TradingAgent:
    """A measurement-only Kiwoom agent, built lazily while its session is open.

    dry_run is forced in the agent's own config copy — this venue is live
    mainnet money that has passed no gate, and it exists here to measure.
    """
    mcfg = cfg.model_copy(deep=True)
    mcfg.agent.market = market
    mcfg.agent.dry_run = True
    return TradingAgent(mcfg, notifier=telegram, broker="kiwoom")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    # Accepted for compatibility with the old per-broker install; every value
    # now means "run all venues".
    ap.add_argument("--broker", default="all", choices=["all", "binance", "kiwoom"])
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = config()
    telegram = TelegramNotifier()
    # Receiving is exclusive to one service; sending is not. This single
    # service owns commands under either its new or its legacy owner name.
    owns_commands = cfg.notify.telegram.command_owner in ("all", "binance", "trading-agent")
    if args.broker != "all":
        log.info("--broker %s is legacy; this service runs every venue", args.broker)

    mode = "DRY RUN" if cfg.agent.dry_run else "LIVE"
    testnet = cfg.broker.binance.use_testnet
    telegram.send(
        f"🟢 trading-agent service started — *{mode}*{' (testnet)' if testnet else ''}\n"
        "markets: BINANCE 24/7 · KR/US measurement in-session\n"
        f"sizing: `{cfg.sizing.mode}`, max {cfg.sizing.max_positions} position(s)"
    )
    log.info("service start: mode=%s markets=BINANCE+%s", mode, "/".join(KIWOOM_MARKETS))

    binance: TradingAgent | None = None
    kiwoom: dict[str, TradingAgent] = {}
    open_now: set[str] = set()
    watcher = build_watcher(notifier=telegram)

    while True:
        try:
            if restart_requested(cfg):
                # Distinguishable in the log from a crash: SCM reports both as
                # "terminated unexpectedly", so a boot without this line above
                # it is a real failure worth investigating.
                log.info("%s", cfg.service.graceful_exit_log)
                telegram.send("♻️ restarting to pick up configuration changes")
                return 0

            if binance is None:
                binance = TradingAgent(cfg, notifier=telegram, broker="binance")
            result = binance.run_cycle()
            log.info("BINANCE %s", result.summary())
            if result.intents or result.exits or result.errors:
                telegram.send("BINANCE " + result.summary())

            for market in KIWOOM_MARKETS:
                if not cfg.agent.is_open(market):
                    if market in open_now:
                        open_now.discard(market)
                        log.info("%s session closed", market)
                        telegram.send(f"⏹️ {market} session closed (measurement paused)")
                    continue
                if market not in open_now:
                    open_now.add(market)
                    log.info("%s session open", market)
                    telegram.send(f"▶️ {market} session open — measurement cycles begin")
                agent = kiwoom.get(market)
                if agent is None:
                    # Constructed only in-session: building a client mints the
                    # shared Kiwoom token, which after hours belongs to the
                    # archive downloader.
                    agent = kiwoom[market] = _kiwoom_agent(cfg, market, telegram)
                r = agent.run_cycle()
                log.info("%s %s", market, r.summary())
                if r.errors:
                    telegram.send(f"{market} " + r.summary())

            # Answer commands while waiting out the interval, so /halt is
            # honoured within seconds rather than at the next cycle.
            deadline = time.monotonic() + cfg.agent.loop_interval_s
            while time.monotonic() < deadline and not Path(cfg.service.restart_file).exists():
                remaining = max(1, int(min(IDLE_POLL_S, deadline - time.monotonic())))
                if not owns_commands:
                    time.sleep(remaining)
                    continue
                for msg in telegram.poll(timeout_s=remaining):
                    telegram.send(watcher.handle(msg.text), chat_id=msg.chat_id)

        except KeyboardInterrupt:
            telegram.send("⏹️ trading-agent service stopping")
            return 0
        except Exception:
            log.exception("cycle failed; continuing")
            time.sleep(IDLE_POLL_S)


if __name__ == "__main__":
    raise SystemExit(main())
