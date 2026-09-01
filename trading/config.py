"""Configuration: `config.yaml` for parameters, `.env` for secrets.

The split is the one declared at the top of `.env`. Nothing tunable should be a
literal in code -- add it to `config.yaml` and read it through :func:`config`.
Secrets are never read from the YAML.
"""

from __future__ import annotations

import datetime as dt
import functools
from pathlib import Path

import yaml
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_PATH = Path("config.yaml")


# -- config.yaml ------------------------------------------------------------


class StateConfig(BaseModel):
    source_of_truth: str = "broker"
    max_staleness_s: float = 5.0
    reconcile_before_order: bool = True


class TierConfig(BaseModel):
    provider: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 2048


class ProviderConfig(BaseModel):
    base_url: str


class TokenPrice(BaseModel):
    """Price per 1,000,000 tokens, in `currency`.

    DeepSeek publishes separate USD and CNY lists at a fixed internal ratio
    (~6.82) that is NOT the market rate, and this account is billed in CNY —
    so CNY-billed models must be priced from the CNY list, not converted USD.
    """

    input: float = 0.0
    output: float = 0.0
    currency: str = "USD"  # USD | CNY


class FeeConfig(BaseModel):
    commission_rate: float = 0.0
    # Kept for venues that tax sells (KR did); Binance books leave it at zero.
    sell_tax_rate: float = 0.0
    slippage_bps: float = 0.0
    # The currency a fee on this market is DENOMINATED in — the venue's quote
    # currency (USDT is treated as USD). The ledger converts to KRW at write
    # time; without this, USDT fee figures were stored under KRW labels and
    # /costs under-reported Binance fees by the full FX rate.
    currency: str = "USD"  # USD | KRW

    def round_trip_rate(self) -> float:
        """Fraction of notional consumed by one buy + one sell."""
        slip = self.slippage_bps / 10_000
        return self.commission_rate * 2 + self.sell_tax_rate + slip * 2


class AccountingConfig(BaseModel):
    ledger: str = "data/ledger.jsonl"
    max_api_krw_per_day: float = 0.0
    fees: FeeConfig = Field(default_factory=FeeConfig)
    # Venues differ structurally, not just numerically: KR pays a 0.15% transaction
    # tax on sells that Binance does not, so one shared fee model would misprice
    # every break-even on one venue or the other.
    market_fees: dict[str, FeeConfig] = Field(default_factory=dict)
    report_currency: str = "KRW"

    def fees_for(self, market: str | None = None) -> FeeConfig:
        if market and str(market) in self.market_fees:
            return self.market_fees[str(market)]
        return self.fees


class LLMConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    pricing: dict[str, TokenPrice] = Field(default_factory=dict)
    usd_krw: float = 1380.0
    # Market rate for converting CNY-priced calls; the KRW the ledger reports is
    # native CNY / usd_cny * usd_krw, i.e. what the bill actually costs in won.
    usd_cny: float = 7.15
    default_tier: str = "fast"
    # Used when a tier returns no content at all. Must be a NON-reasoning model:
    # the failure mode being recovered from is a reasoning budget overrun.
    fallback_tier: str = "fast"
    tiers: dict[str, TierConfig] = Field(default_factory=dict)
    timeout_s: float = 60.0
    max_retries: int = 2

    def provider(self, name: str) -> ProviderConfig:
        try:
            return self.providers[name]
        except KeyError:
            raise KeyError(
                f"unknown llm provider {name!r}; known: {sorted(self.providers)}"
            ) from None

    def tier(self, name: str | None = None) -> TierConfig:
        name = name or self.default_tier
        try:
            return self.tiers[name]
        except KeyError:
            raise KeyError(f"unknown llm tier {name!r}; known: {sorted(self.tiers)}") from None


class TelegramConfig(BaseModel):
    enabled: bool = True
    api_base: str = "https://api.telegram.org"
    parse_mode: str = "Markdown"
    timeout_s: float = 15.0
    poll_timeout_s: int = 30
    max_message_chars: int = 4096
    # Telegram allows ONE long-poll consumer per bot token. With a service per
    # venue they steal each other's updates (409 Conflict) and a /halt can reach
    # the wrong agent -- or none. Exactly one service owns the command surface;
    # the others send only.
    command_owner: str = "binance"


class NotifyConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class RiskConfig(BaseModel):
    enabled: bool = True
    max_order_value_krw: float = 0.0
    max_position_pct: float = 0.0
    max_daily_loss_krw: float = 0.0
    # Unit-free alternative. The *_krw limits are compared against whatever
    # currency the venue reports in -- Binance answers in USDT, so a 150,000 "KRW"
    # cap reads as $150,000 there and can never fire. A fraction of equity is
    # correct on every venue.
    max_daily_loss_pct: float = 0.0
    max_orders_per_cycle: int = 3
    max_orders_per_day: int = 20
    kill_switch_file: str = "data/HALT"


class SizingConfig(BaseModel):
    """How much of the balance a single entry commits."""

    mode: str = "full_balance"
    fraction: float = 1.0
    cash_field: str = "ord_alow_amt"
    cash_fallback_field: str = "entr"
    reserve_pct: float = 0.005
    max_positions: int = 1
    lot_size: int = 1


class EntryRungs(BaseModel):
    count: int = 4
    spacing_pct: float = 0.005
    start_offset_pct: float = 0.0
    weights: list[float] = Field(default_factory=list)


class ExitRungs(BaseModel):
    count: int = 3
    first_hurdle_multiple: float = 2.0
    spacing_hurdle_multiple: float = 2.0
    weights: list[float] = Field(default_factory=list)


class RungsConfig(BaseModel):
    """Laddered entries and exits instead of one all-in order."""

    enabled: bool = True
    entry: EntryRungs = Field(default_factory=EntryRungs)
    exit: ExitRungs = Field(default_factory=ExitRungs)


class ServiceConfig(BaseModel):
    """Restart-by-exit: NSSM revives the process, so no service rights are needed."""

    restart_file: str = "data/RESTART"
    graceful_exit_log: str = "graceful restart requested"


class MarketExits(BaseModel):
    """Per-venue exit levels. Volatility is not portable between venues."""

    stop_loss_pct: float | None = None
    target_hurdle_multiple: float | None = None
    min_reward_risk: float | None = None
    max_hold_minutes: float | None = None


class ExitConfig(BaseModel):
    """Exit levels expressed as multiples of the round-trip cost hurdle."""

    enabled: bool = True
    check_interval_s: float = 30.0
    stop_loss_pct: float = 0.03
    target_hurdle_multiple: float = 4.0
    min_reward_risk: float = 2.0
    trail_arm_hurdle_multiple: float = 2.0
    trail_give_back: float = 0.4
    max_hold_minutes: float = 360.0
    state: str = "data/exit_policy.json"
    exits_allowed_under_halt: bool = True
    # A 17.8% target in 72h is routine on crypto and impossible on a US large cap.
    # Applying one set of levels everywhere made the mandate unreachable on the
    # calmer venues, and the trader correctly declined every cycle because of it.
    markets: dict[str, MarketExits] = Field(default_factory=dict)

    def for_market(self, market: str | None) -> ExitConfig:
        """This config with any per-venue overrides applied."""
        over = self.markets.get(str(market)) if market else None
        if over is None:
            return self
        merged = self.model_copy(deep=True)
        for field, value in over.model_dump(exclude_none=True).items():
            setattr(merged, field, value)
        return merged


class AgentTiers(BaseModel):
    decide: str = "deep"
    escalate_on_low_confidence: str = "escalation"
    confidence_floor: float = 0.6


class ScreenMarket(BaseModel):
    min_change_pct: float = 0.0
    max_change_pct: float = 0.0
    # Per market so a venue can set a floor in its own currency.
    min_price: float = 0.0


class ScreenConfig(BaseModel):
    """Narrows the full market to the handful the model is allowed to consider."""

    candidates: int = 25
    min_price: float = 0.0
    # Liquidity floor for venues that list microcaps. Without it the universe
    # includes symbols whose whole daily turnover is smaller than one order.
    min_quote_volume: float = 0.0
    # Minimum 24h move to qualify at all. The backtest was unambiguous that a
    # strict threshold beat a loose one everywhere (20% > 10% in every window).
    min_change_pct: float = 0.0
    # Upper bound too. Ranking on |change| alone selects for EXHAUSTION -- it
    # surfaces the top of a +172% day, which the trader then correctly refuses as
    # "already extremely extended". A move must be live, not finished.
    max_change_pct: float = 0.0
    # Liquidity floor expressed as a multiple of the ORDER, not as a fixed number.
    # With full-balance sizing the order grows with the account, so a fixed floor
    # silently becomes too permissive as the balance grows.
    min_volume_multiple_of_order: float = 0.0
    # Pegged assets dominate volume rankings and have no directional opportunity.
    exclude_assets: list[str] = Field(default_factory=list)
    # Slots per book. Without this the deepest book takes every slot: crypto's
    # median turnover is ~6x the LARGEST bStock, so bStocks never rank.
    book_slots: dict[str, int] = Field(default_factory=dict)
    book_min_quote_volume: dict[str, float] = Field(default_factory=dict)
    # Order flow, not price momentum. See BinanceScreen._flow for the measurement.
    use_flow: bool = False
    flow_interval: str = "1d"
    flow_lookback: int = 5
    flow_pool_per_book: int = 30
    BINANCE: ScreenMarket | None = None


class AgentConfig(BaseModel):
    enabled: bool = True
    market: str = "BINANCE"
    dry_run: bool = True
    loop_interval_s: float = 900.0
    skip_decide_if_unchanged: bool = True
    journal: str = "data/journal.jsonl"
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    always_open: list[str] = Field(default_factory=lambda: ["BINANCE"])

    def is_open(self, market: str, now: dt.datetime | None = None) -> bool:
        """Crypto never closes; a market not in always_open never trades."""
        return str(market) in self.always_open

    tiers: AgentTiers = Field(default_factory=AgentTiers)


class BinanceEndpoint(BaseModel):
    path: str
    method: str = "GET"
    signed: bool = False
    order: bool = False


class BinanceMarket(BaseModel):
    """A book sharing the Binance connection and balance."""

    quote_asset: str = "USDT"
    # bStocks carry this permission tag. A symbol-suffix rule would wrongly match
    # BNB, SHIB, ARB and CKB, which are ordinary coins.
    permission_tag: str = ""
    exclude_permission_tag: str = ""


class BinanceConfig(BaseModel):
    use_testnet: bool = False
    # Operator's local timezone: cycle stamps and daily rollovers render in it.
    timezone: str = "Asia/Seoul"
    timeout_s: float = 15.0
    recv_window_ms: int = 5000
    min_call_interval_s: float = 0.1
    retry_backoff_s: float = 2.0
    allow_orders: bool = False
    endpoints: dict[str, BinanceEndpoint] = Field(default_factory=dict)
    markets: dict[str, BinanceMarket] = Field(default_factory=dict)

    def endpoint(self, name: str) -> BinanceEndpoint:
        try:
            return self.endpoints[name]
        except KeyError:
            raise KeyError(
                f"unknown binance endpoint {name!r}; known: {sorted(self.endpoints)}"
            ) from None

    def market(self, name: str) -> BinanceMarket:
        try:
            return self.markets[str(name)]
        except KeyError:
            raise KeyError(f"no binance market {name!r}; known: {sorted(self.markets)}") from None


class BrokerConfig(BaseModel):
    binance: BinanceConfig = Field(default_factory=BinanceConfig)


class ExploreConfig(BaseModel):
    """The random exploration arm — ε in an explore/exploit split.

    Random entries fill the experience corpus with ground truth the model arm
    cannot provide: they sample the TRADABLE universe (liquidity and lot rules
    apply; the screen's strategy bounds deliberately do not), so the screen's
    own thresholds stay falsifiable. `entry_pct` is meant to be decayed by the
    operator toward `floor_pct`, never to zero: without a live random arm,
    model-vs-chance stops being measurable.
    """

    enabled: bool = False
    entry_pct: float = 0.5  # probability per cycle of attempting random entries
    floor_pct: float = 0.15  # documented floor for manual decay; not enforced in code
    max_positions: int = 4  # cap on concurrently open random-arm entries
    entries_per_cycle: int = 1  # entries attempted per cycle once the pct roll passes
    seed: int = 0  # 0 = OS entropy; set for a reproducible sequence


class ScoreConfig(BaseModel):
    """Offline outcome scoring that fills `experience` — the RAG build step."""

    enabled: bool = True
    horizon_minutes: int = 4320  # forward-return horizon; matches max_hold
    interval_minutes: int = 60  # how often the in-service scorer pass runs
    min_bucket_n: int = 10  # buckets below this never render into a prompt
    max_opens_per_run: int = 150  # spreads the initial universe sweep's kline calls
    observations: str = "data/observations.jsonl"
    experience: str = "data/experience.json"
    # Ledger trades at or after this date (ISO) count as closed-trade outcomes;
    # empty = all. Exists because the ledger spans epochs (live mainnet KR, then
    # testnet) and closed-trade stats must not mix them.
    trade_since: str = ""
    trade_markets: list[str] = Field(default_factory=lambda: ["CRYPTO", "BSTOCKS", "BINANCE"])
    # Historical backfill/replay (provenance-separated backtest evidence).
    backfill_days: int = 60
    replay: str = "data/replay.jsonl"
    replay_summary: str = "data/replay_summary.json"


class PromotionConfig(BaseModel):
    """The mainnet gate: promote when measured profit is positive, stay otherwise.

    The gate only measures and reports (`trading/agent/promotion.py`, the
    *Mainnet gate* section of /status). Flipping `use_testnet` is the owner's
    deliberate config edit, made by reading that report.
    """

    min_closed_trades: int = 100
    min_shadow_pairs: int = 30
    # Measurement epoch start (ISO date). Empty = score.trade_since. Bump this
    # past regime changes (a sprint, a reset) so the verdict reflects the
    # configuration that would actually trade on mainnet.
    since: str = ""


class AppConfig(BaseModel):
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    accounting: AccountingConfig = Field(default_factory=AccountingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    exits: ExitConfig = Field(default_factory=ExitConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    rungs: RungsConfig = Field(default_factory=RungsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    explore: ExploreConfig = Field(default_factory=ExploreConfig)
    score: ScoreConfig = Field(default_factory=ScoreConfig)
    promotion: PromotionConfig = Field(default_factory=PromotionConfig)


def load_config(path: str | Path = CONFIG_PATH) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return AppConfig(**raw)


@functools.cache
def config() -> AppConfig:
    """Process-wide config. Call :func:`load_config` directly in tests."""
    return load_config()


# -- .env (secrets only) ----------------------------------------------------


class TelegramSecrets(BaseSettings):
    """This agent's own bot. Each subsystem in `.env` has a separate token."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    bot_token: str = Field("", alias="TRADING_AGENT_BOT_TOKEN")
    # Comma-separated chat ids permitted to command the agent.
    allowed_ids_raw: str = Field("", alias="TELEGRAM_ALLOWED_IDS")

    @property
    def allowed_ids(self) -> set[int]:
        return {int(p) for p in self.allowed_ids_raw.replace(" ", "").split(",") if p}


class BinanceSecrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    api_key: str = Field("", alias="BINANCE_MAINNET_API_KEY")
    secret_key: str = Field("", alias="BINANCE_MAINNET_SECRET_KEY")
    rest_url: str = Field("https://api.binance.com", alias="BINANCE_MAINNET_REST_URL")
    testnet_api_key: str = Field("", alias="BINANCE_TESTNET_API_KEY")
    testnet_secret_key: str = Field("", alias="BINANCE_TESTNET_SECRET_KEY")
    testnet_rest_url: str = Field(
        "https://testnet.binance.vision", alias="BINANCE_TESTNET_REST_URL"
    )

    def credentials(self, *, testnet: bool) -> tuple[str, str]:
        if testnet:
            return self.testnet_api_key, self.testnet_secret_key
        return self.api_key, self.secret_key

    def base_url(self, *, testnet: bool) -> str:
        return (self.testnet_rest_url if testnet else self.rest_url).rstrip("/")


class LLMSecrets(BaseSettings):
    """API keys only. Endpoints live in `config.yaml` under `llm.providers`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    deepseek_api_key: str = Field(
        "", validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DEEPSEEK_KEY")
    )
    # Qwen is served by Alibaba DashScope; the key ships under several names
    # depending on where it was issued, so accept all of them.
    qwen_api_key: str = Field(
        "",
        validation_alias=AliasChoices(
            "DASHSCOPE_API_KEY", "QWEN_API_KEY", "QWEN_KEY", "TONGYI_API_KEY", "ALIBABA_API_KEY"
        ),
    )

    def key_for(self, provider: str) -> str:
        match provider:
            case "deepseek":
                return self.deepseek_api_key
            case "qwen":
                return self.qwen_api_key
        raise KeyError(f"unknown llm provider {provider!r}")
