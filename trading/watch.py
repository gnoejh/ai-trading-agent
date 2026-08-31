"""Watch trading status over Telegram.

    uv run python -m trading.watch                     # serve commands, push on interval
    uv run python -m trading.watch --once              # print one report and exit
    uv run python -m trading.watch --send              # send one report to Telegram and exit

Commands accepted from allowed chats only: `/status`, `/positions`, `/cash`,
`/pnl`, `/costs`, `/halt`, `/resume`, `/help`.

`/halt` writes a kill-switch file; its presence is what blocks order placement,
so halting works even if this process dies. The watcher writes the PER-VENUE
`HALT.BINANCE`, matching the gate's convention -- the file the gate checks is
the file the phone command must set and clear. Nothing here can place an order:
the client is constructed read-only.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from trading.accounting.costs import CostLedger
from trading.brokers.binance.account import BinanceAccountState
from trading.brokers.binance.client import BinanceClient
from trading.brokers.binance.universe import BinanceUniverse
from trading.config import AppConfig, config
from trading.notify.status import BinanceStatusReporter
from trading.notify.telegram import TelegramNotifier

log = logging.getLogger(__name__)

HELP = (
    "*Trading agent*\n"
    "/status — full account status\n"
    "/positions — holdings only\n"
    "/cash — deposit detail only\n"
    "/pnl — trading P&L, realised and unrealised\n"
    "/costs — fees, API spend, break-even arithmetic\n"
    "/halt — set the kill switch (blocks orders)\n"
    "/resume — clear the kill switch\n"
    "/help — this message"
)


class BinanceWatcher:
    """The operator's command surface over the Binance account.

    Monitoring must not trade: the client is built with allow_orders=False, and
    a second Binance client is safe (HMAC signing, no OAuth token to revoke).

    `/halt` and `/resume` act on the PER-VENUE kill switch (`HALT.BINANCE`),
    matching the gate's convention.
    """

    def __init__(
        self,
        *,
        cfg: AppConfig | None = None,
        client: BinanceClient | None = None,
        notifier: TelegramNotifier | None = None,
    ):
        self.cfg = cfg or config()
        books = list(self.cfg.broker.binance.markets)
        self.client = client or BinanceClient(books[0], cfg=self.cfg, allow_orders=False)
        universe = BinanceUniverse(self.client, books, self.cfg)
        self.state = BinanceAccountState(self.client, universe, self.cfg)
        self.ledger = CostLedger(self.cfg)
        # The Binance app cannot show a testnet account, so this report is the
        # operator's only window -- it renders exit-plan detail and the day's
        # DeepSeek burn, not raw fields.
        self.reporter = BinanceStatusReporter(self.state, self.cfg, ledger=self.ledger)
        self.telegram = notifier or TelegramNotifier()
        base = Path(self.cfg.risk.kill_switch_file)
        self.halt_file = base.with_name(f"{base.name}.BINANCE")

    # -- commands -------------------------------------------------------------

    def handle(self, text: str) -> str:
        command = text.strip().split()[0].lower() if text.strip() else ""
        match command:
            case "/status" | "status":
                return self.reporter.safe_report()
            case "/positions":
                return self.reporter.safe_report(sections=("positions",))
            case "/cash":
                return self.reporter.safe_report(sections=("cash",))
            case "/pnl":
                return self.reporter.safe_report(sections=("positions", "pnl"))
            case "/costs" | "/breakeven":
                return self.ledger.breakeven()
            case "/halt":
                self.halt_file.parent.mkdir(parents=True, exist_ok=True)
                self.halt_file.write_text("halted via telegram\n", encoding="utf-8")
                return f"🛑 kill switch SET (`{self.halt_file}`). Orders are blocked."
            case "/resume":
                if self.halt_file.exists():
                    self.halt_file.unlink()
                    reply = "✅ kill switch cleared. Orders are allowed again."
                else:
                    reply = "kill switch was not set."
                # Clearing the venue file while the GLOBAL halt is set would report
                # "orders allowed" untruthfully -- the gate still blocks everything.
                global_halt = Path(self.cfg.risk.kill_switch_file)
                if global_halt.exists():
                    reply += (
                        f"\n⚠️ global kill switch `{global_halt}` is still set"
                        " and blocks all venues."
                    )
                return reply
            case "/help" | "/start":
                return HELP
        return f"unknown command {command!r}\n\n{HELP}"

    # -- loops ----------------------------------------------------------------

    def serve(self, push_interval_s: float = 0.0) -> None:
        """Answer commands, optionally pushing a status report on an interval."""
        if not self.telegram.configured:
            raise RuntimeError("telegram not configured; check TRADING_AGENT_BOT_TOKEN in .env")
        log.info("watching; allowed chats: %s", sorted(self.telegram.allowed))
        next_push = time.monotonic() + push_interval_s if push_interval_s else None

        while True:
            for msg in self.telegram.poll():
                log.info("command from %s: %s", msg.chat_id, msg.text)
                self.telegram.send(self.handle(msg.text), chat_id=msg.chat_id)
            if next_push and time.monotonic() >= next_push:
                self.telegram.send(self.reporter.safe_report())
                next_push = time.monotonic() + push_interval_s


# The single watcher class doubles as the generic name for callers that predate
# the Binance-only refactor.
Watcher = BinanceWatcher


def build_watcher(
    broker: str = "binance",
    market: str | None = None,
    *,
    cfg: AppConfig | None = None,
    notifier: TelegramNotifier | None = None,
) -> BinanceWatcher:
    """The service's one seam for constructing the command surface.

    run_service once built a watcher only for one broker, so the other service
    polled Telegram commands and silently dropped them -- the classic
    correct-component-never-called defect. Every service routes through here,
    and the seam is covered by tests/test_watch.py.
    """
    if str(broker).lower() != "binance":
        raise KeyError(f"unknown broker {broker!r}; known: binance")
    return BinanceWatcher(cfg=cfg, notifier=notifier)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--broker", default="binance", choices=["binance"])
    ap.add_argument("--once", action="store_true", help="print one report and exit")
    ap.add_argument("--send", action="store_true", help="send one report to Telegram and exit")
    ap.add_argument("--interval", type=float, default=0.0, help="seconds between pushed reports")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    w = build_watcher(args.broker)
    if args.once:
        print(w.reporter.safe_report())
        return 0
    if args.send:
        return 0 if w.telegram.send(w.reporter.safe_report()) else 1
    w.serve(push_interval_s=args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
