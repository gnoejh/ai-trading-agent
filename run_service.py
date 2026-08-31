"""Service entry point: trade continuously, serve Telegram commands between cycles.

    uv run python run_service.py

One process, two responsibilities:

* **Trading.** Runs :class:`TradingAgent` cycles. Binance never closes, so the
  loop simply paces itself on `agent.loop_interval_s`.
* **Operator control.** Serves `/status`, `/costs`, `/halt`, `/resume` between
  cycles, so the account is reachable from a phone while unattended.
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
    ap.add_argument("--broker", default="binance", choices=["binance"])
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
    testnet = cfg.broker.binance.use_testnet
    telegram.send(
        f"🟢 trading-agent service started — *{mode}*{' (testnet)' if testnet else ''}\n"
        f"broker: {args.broker}\n"
        # Backticks, not bare text: `full_balance` contains an underscore, which
        # Telegram's legacy Markdown reads as an unclosed italic and rejects.
        f"sizing: `{cfg.sizing.mode}`, max {cfg.sizing.max_positions} position(s)\n"
        f"sessions: 24/7"
    )
    log.info("service start: broker=%s mode=%s", args.broker, mode)

    # Built lazily so a Telegram-only stretch costs nothing; kept for the life
    # of the process because constructing an agent authenticates and loads the
    # universe.
    agent: TradingAgent | None = None
    # EVERY service gets a watcher. A broker-conditional guard here once meant a
    # service polled commands and dropped them silently.
    watcher = build_watcher(args.broker, notifier=telegram)

    while True:
        try:
            if restart_requested(cfg):
                # Distinguishable in the log from a crash: SCM reports both as
                # "terminated unexpectedly", so a boot without this line above it
                # is a real failure worth investigating.
                log.info("%s", cfg.service.graceful_exit_log)
                telegram.send("♻️ restarting to pick up configuration changes")
                return 0

            if agent is None:
                agent = TradingAgent(cfg, notifier=telegram, broker=args.broker)

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
                    telegram.send(watcher.handle(msg.text), chat_id=msg.chat_id)

        except KeyboardInterrupt:
            telegram.send("⏹️ trading-agent service stopping")
            return 0
        except Exception:
            log.exception("cycle failed; continuing")
            time.sleep(IDLE_POLL_S)


if __name__ == "__main__":
    raise SystemExit(main())
