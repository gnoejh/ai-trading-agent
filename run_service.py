"""Service entry point: trade the open session, watch Telegram the rest of the time.

    uv run python run_service.py

One process, two responsibilities:

* **Trading.** Runs :class:`TradingAgent` cycles for whichever market is open.
  Sessions are declared per exchange in its own timezone, so this switches from
  KR to US on its own and needs no wall-clock arithmetic here.
* **Operator control.** Serves `/status`, `/costs`, `/halt`, `/resume` between
  cycles, so the account is reachable from a phone while unattended.

Nothing is scheduled by a cron: the loop asks the session which market is open
each pass and sleeps until one is. When neither is, it idles cheaply — no broker
calls, no tokens — and keeps answering Telegram.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from trading.agent.loop import TradingAgent
from trading.config import config
from trading.notify.telegram import TelegramNotifier
from trading.rag.spec_parser import Market
from trading.watch import Watcher

log = logging.getLogger("service")

IDLE_POLL_S = 20.0


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


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--broker", default="kiwoom", choices=["kiwoom", "binance"])
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = config()
    telegram = TelegramNotifier()
    # Receiving is exclusive to one service; sending is not.
    owns_commands = args.broker == cfg.notify.telegram.command_owner
    if not owns_commands:
        log.info(
            "not the telegram command owner (%s); send-only", cfg.notify.telegram.command_owner
        )

    mode = "DRY RUN" if cfg.agent.dry_run else "LIVE"
    telegram.send(
        f"🟢 trading-agent service started — *{mode}*\n"
        f"sizing: {cfg.sizing.mode}, max {cfg.sizing.max_positions} position(s)\n"
        f"sessions: {', '.join(cfg.agent.sessions)}"
    )
    log.info("service start: mode=%s sessions=%s", mode, list(cfg.agent.sessions))

    agents: dict[str, TradingAgent] = {}
    watchers: dict[str, Watcher] = {}
    current: str | None = None

    while True:
        try:
            if restart_requested(cfg):
                # Distinguishable in the log from a crash: SCM reports both as
                # "terminated unexpectedly", so a boot without this line above it
                # is a real failure worth investigating.
                log.info("%s", cfg.service.graceful_exit_log)
                telegram.send("♻️ restarting to pick up configuration changes")
                return 0

            market = "BINANCE" if args.broker == "binance" else cfg.agent.open_market()

            if market is None:
                # Closed everywhere. Stay responsive on Telegram, spend nothing.
                if current is not None:
                    log.info("%s session closed", current)
                    telegram.send(f"⏹️ {current} session closed")
                    current = None
                watcher = watchers.get("KR") or watchers.setdefault(
                    "KR", Watcher(Market.KR, notifier=telegram)
                )
                if owns_commands:
                    for msg in telegram.poll(timeout_s=int(IDLE_POLL_S)):
                        telegram.send(watcher.handle(msg.text), chat_id=msg.chat_id)
                else:
                    time.sleep(IDLE_POLL_S)
                continue

            if market != current:
                log.info("%s session open", market)
                telegram.send(f"▶️ {market} session open")
                current = market

            if market not in agents:
                # Built lazily: constructing an agent authenticates and loads the
                # universe, which is wasted work for a market that never opens.
                market_cfg = cfg.model_copy(deep=True)
                market_cfg.agent.market = market
                agents[market] = TradingAgent(market_cfg, notifier=telegram, broker=args.broker)
                if args.broker == "kiwoom":
                    watchers[market] = Watcher(Market(market), notifier=telegram)

            agent = agents[market]
            result = agent.run_cycle()
            log.info("%s", result.summary())
            if result.intents or result.exits or result.errors:
                telegram.send(result.summary())

            # Answer commands while waiting out the interval, so /halt is honoured
            # within seconds rather than at the next cycle.
            deadline = time.monotonic() + cfg.agent.loop_interval_s
            while time.monotonic() < deadline and not Path(cfg.service.restart_file).exists():
                remaining = max(1, int(min(IDLE_POLL_S, deadline - time.monotonic())))
                if not owns_commands:
                    time.sleep(remaining)
                    continue
                for msg in telegram.poll(timeout_s=remaining):
                    watcher = watchers.get(market)
                    if watcher:
                        telegram.send(watcher.handle(msg.text), chat_id=msg.chat_id)

        except KeyboardInterrupt:
            telegram.send("⏹️ trading-agent service stopping")
            return 0
        except Exception:
            log.exception("cycle failed; continuing")
            time.sleep(IDLE_POLL_S)


if __name__ == "__main__":
    raise SystemExit(main())
