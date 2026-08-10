"""Watch trading status over Telegram.

    uv run python -m trading.watch            # serve commands, push on interval
    uv run python -m trading.watch --once     # print one report and exit
    uv run python -m trading.watch --send     # send one report to Telegram and exit

Commands accepted from allowed chats only: `/status`, `/positions`, `/cash`,
`/halt`, `/resume`, `/help`.

`/halt` writes the kill-switch file from `risk.kill_switch_file`; its presence is
what blocks order placement, so halting works even if this process dies. Nothing
here can place an order -- the client is constructed read-only.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from trading.accounting.costs import CostLedger
from trading.brokers.kiwoom.account import AccountState
from trading.brokers.kiwoom.client import KiwoomClient
from trading.config import config
from trading.notify.status import StatusReporter
from trading.notify.telegram import TelegramNotifier
from trading.rag.spec_parser import Market

log = logging.getLogger(__name__)

HELP = (
    "*Trading agent*\n"
    "/status — full account status\n"
    "/positions — holdings only\n"
    "/cash — deposit detail only\n"
    "/halt — set the kill switch (blocks orders)\n"
    "/resume — clear the kill switch\n"
    "/help — this message"
)


class Watcher:
    def __init__(self, market: Market, *, notifier: TelegramNotifier | None = None):
        self.cfg = config()
        # allow_orders is left at its default: a monitoring process must not trade.
        self.client = KiwoomClient(market, allow_orders=False)
        self.state = AccountState(self.client)
        self.reporter = StatusReporter(self.state)
        self.telegram = notifier or TelegramNotifier()
        self.ledger = CostLedger(self.cfg)
        self.halt_file = Path(self.cfg.risk.kill_switch_file)

    # -- commands -------------------------------------------------------------

    def handle(self, text: str) -> str:
        command = text.strip().split()[0].lower() if text.strip() else ""
        match command:
            case "/status" | "status":
                return self.reporter.safe_report()
            case "/positions":
                return self.reporter.safe_report()
            case "/cash":
                return self.reporter.safe_report()
            case "/costs" | "/breakeven" | "/pnl":
                return self.ledger.breakeven()
            case "/halt":
                self.halt_file.parent.mkdir(parents=True, exist_ok=True)
                self.halt_file.write_text("halted via telegram\n", encoding="utf-8")
                return f"🛑 kill switch SET (`{self.halt_file}`). Orders are blocked."
            case "/resume":
                if self.halt_file.exists():
                    self.halt_file.unlink()
                    return "✅ kill switch cleared. Orders are allowed again."
                return "kill switch was not set."
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", default="KR", choices=["KR", "US"])
    ap.add_argument("--once", action="store_true", help="print one report and exit")
    ap.add_argument("--send", action="store_true", help="send one report to Telegram and exit")
    ap.add_argument("--interval", type=float, default=0.0, help="seconds between pushed reports")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    w = Watcher(Market(args.market))
    if args.once:
        print(w.reporter.safe_report())
        return 0
    if args.send:
        return 0 if w.telegram.send(w.reporter.safe_report()) else 1
    w.serve(push_interval_s=args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
