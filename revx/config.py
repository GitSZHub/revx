"""Load config.toml into frozen dataclasses. All numbers are Decimal or int.

Secrets are not part of Config: the API key is read from the environment
(via python-dotenv) only at the moment the client is built.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.toml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    private_key_path: Path
    timeout_seconds: int
    max_consecutive_errors: int
    daily_order_budget: int


@dataclass(frozen=True)
class PathsConfig:
    ledger: Path
    backups: Path
    halt_file: Path


@dataclass(frozen=True)
class LoopConfig:
    interval_seconds: int
    unfilled_cancel_minutes: int


@dataclass(frozen=True)
class StrategyConfig:
    pairs: tuple[str, ...]
    candle_interval_minutes: int
    tranche_eur: Decimal
    max_open_lots: int
    max_open_lots_per_pair: int
    bollinger_period: int
    bollinger_stddev: Decimal
    atr_period: int
    spacing_atr_mult: Decimal
    vol_window: int
    vol_lookback_days: int
    vol_max_percentile: Decimal
    signal: str
    band_width_pct: Decimal
    band_max_lots: int
    breakout_atr_mult: Decimal
    breakout_cooldown_candles: int

    @property
    def vol_lookback_candles(self) -> int:
        return self.vol_lookback_days * 1440 // self.candle_interval_minutes


@dataclass(frozen=True)
class ExitConfig:
    min_profit: Decimal
    trail_atr_mult: Decimal
    partial_pct: Decimal


@dataclass(frozen=True)
class Config:
    dry_run: bool
    api: ApiConfig
    paths: PathsConfig
    loop: LoopConfig
    strategy: StrategyConfig
    exit: ExitConfig
    reconcile_tolerance_pct: Decimal
    reconcile_abs_eur: Decimal
    reconcile_abs_crypto: Decimal
    dry_run_starting_eur: Decimal
    target_multiple: Decimal


def _dec(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    return Decimal(value)


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer, got {value!r}")
    return value


def load_config(path: Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    base = path.resolve().parent
    with open(path, "rb") as f:
        raw = tomllib.load(f, parse_float=Decimal)

    try:
        dry_run = raw["mode"]["DRY_RUN"]
        if not isinstance(dry_run, bool):
            raise ConfigError(f"mode.DRY_RUN must be true or false, got {dry_run!r}")

        a, p, lo, s, e = raw["api"], raw["paths"], raw["loop"], raw["strategy"], raw["exit"]
        cfg = Config(
            dry_run=dry_run,
            api=ApiConfig(
                base_url=str(a["base_url"]).rstrip("/"),
                private_key_path=base / a["private_key_path"],
                timeout_seconds=_int(a["timeout_seconds"], "api.timeout_seconds"),
                max_consecutive_errors=_int(a["max_consecutive_errors"], "api.max_consecutive_errors"),
                daily_order_budget=_int(a["daily_order_budget"], "api.daily_order_budget"),
            ),
            paths=PathsConfig(
                ledger=base / p["ledger"],
                backups=base / p["backups"],
                halt_file=base / p["halt_file"],
            ),
            loop=LoopConfig(
                interval_seconds=_int(lo["interval_seconds"], "loop.interval_seconds"),
                unfilled_cancel_minutes=_int(lo["unfilled_cancel_minutes"], "loop.unfilled_cancel_minutes"),
            ),
            strategy=StrategyConfig(
                pairs=tuple(s["pairs"]),
                candle_interval_minutes=_int(s["candle_interval_minutes"], "strategy.candle_interval_minutes"),
                tranche_eur=_dec(s["tranche_eur"], "strategy.tranche_eur"),
                max_open_lots=_int(s["max_open_lots"], "strategy.max_open_lots"),
                max_open_lots_per_pair=_int(s["max_open_lots_per_pair"], "strategy.max_open_lots_per_pair"),
                bollinger_period=_int(s["bollinger_period"], "strategy.bollinger_period"),
                bollinger_stddev=_dec(s["bollinger_stddev"], "strategy.bollinger_stddev"),
                atr_period=_int(s["atr_period"], "strategy.atr_period"),
                spacing_atr_mult=_dec(s["spacing_atr_mult"], "strategy.spacing_atr_mult"),
                vol_window=_int(s["vol_window"], "strategy.vol_window"),
                vol_lookback_days=_int(s["vol_lookback_days"], "strategy.vol_lookback_days"),
                vol_max_percentile=_dec(s["vol_max_percentile"], "strategy.vol_max_percentile"),
                signal=str(s["signal"]),
                band_width_pct=_dec(s["band_width_pct"], "strategy.band_width_pct"),
                band_max_lots=_int(s["band_max_lots"], "strategy.band_max_lots"),
                breakout_atr_mult=_dec(s["breakout_atr_mult"], "strategy.breakout_atr_mult"),
                breakout_cooldown_candles=_int(s["breakout_cooldown_candles"], "strategy.breakout_cooldown_candles"),
            ),
            exit=ExitConfig(
                min_profit=_dec(e["min_profit"], "exit.min_profit"),
                trail_atr_mult=_dec(e["trail_atr_mult"], "exit.trail_atr_mult"),
                partial_pct=_dec(e["partial_pct"], "exit.partial_pct"),
            ),
            reconcile_tolerance_pct=_dec(raw["reconcile"]["tolerance_pct"], "reconcile.tolerance_pct"),
            reconcile_abs_eur=_dec(raw["reconcile"]["abs_tolerance_eur"], "reconcile.abs_tolerance_eur"),
            reconcile_abs_crypto=_dec(raw["reconcile"]["abs_tolerance_crypto"], "reconcile.abs_tolerance_crypto"),
            dry_run_starting_eur=_dec(raw["dry_run"]["starting_eur"], "dry_run.starting_eur"),
            target_multiple=_dec(raw["report"]["target_multiple"], "report.target_multiple"),
        )
    except KeyError as exc:
        raise ConfigError(f"missing config key: {exc}") from exc

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    s, e = cfg.strategy, cfg.exit
    if not s.pairs:
        raise ConfigError("strategy.pairs is empty")
    if s.tranche_eur <= 0:
        raise ConfigError("strategy.tranche_eur must be positive")
    if s.max_open_lots < 1 or s.max_open_lots_per_pair < 1:
        raise ConfigError("lot caps must be at least 1")
    if e.min_profit < 0:
        raise ConfigError("exit.min_profit must not be negative")
    if e.trail_atr_mult <= 0:
        raise ConfigError("exit.trail_atr_mult must be positive")
    if not (Decimal(0) <= e.partial_pct < Decimal(1)):
        raise ConfigError("exit.partial_pct must be in [0, 1)")
    if not (Decimal(0) < s.vol_max_percentile <= Decimal(100)):
        raise ConfigError("strategy.vol_max_percentile must be in (0, 100]")
    if s.band_width_pct <= 0 or s.band_max_lots < 1:
        raise ConfigError("band rule needs band_width_pct > 0 and band_max_lots >= 1")
    if s.breakout_atr_mult <= 0 or s.breakout_cooldown_candles < 0:
        raise ConfigError("breakout rule needs breakout_atr_mult > 0 and breakout_cooldown_candles >= 0")


def resolve_dry_run(cfg: Config, live_flag: bool) -> tuple[bool, str]:
    """The live gate. Live only when config says DRY_RUN=false AND --live was passed.

    Returns (dry_run, reason).
    """
    if not cfg.dry_run and live_flag:
        return False, "live: DRY_RUN=false in config and --live passed"
    if cfg.dry_run and live_flag:
        return True, "dry-run: --live passed but config has DRY_RUN=true"
    if not cfg.dry_run and not live_flag:
        return True, "dry-run: config has DRY_RUN=false but --live was not passed"
    return True, "dry-run: default"
