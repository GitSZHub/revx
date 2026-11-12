from dataclasses import replace
from decimal import Decimal

import pytest

from revx.config import DEFAULT_CONFIG_PATH, ConfigError, load_config, resolve_dry_run


def test_repo_config_loads_and_defaults_to_dry_run():
    cfg = load_config()
    assert cfg.dry_run is True
    assert cfg.strategy.pairs == ("BTC-EUR", "ETH-EUR")
    assert cfg.strategy.tranche_eur == Decimal("50")
    assert cfg.exit.min_profit == Decimal("0.03")
    assert isinstance(cfg.exit.min_profit, Decimal)
    assert isinstance(cfg.reconcile_tolerance_pct, Decimal)


@pytest.mark.parametrize(
    "config_dry_run, live_flag, expected",
    [(True, False, True), (True, True, True), (False, False, True), (False, True, False)],
)
def test_live_needs_both_config_and_flag(config_dry_run, live_flag, expected):
    cfg = replace(load_config(), dry_run=config_dry_run)
    dry_run, reason = resolve_dry_run(cfg, live_flag)
    assert dry_run is expected
    assert reason


def test_non_boolean_dry_run_is_rejected(tmp_path):
    bad = tmp_path / "config.toml"
    bad.write_text(DEFAULT_CONFIG_PATH.read_text().replace("DRY_RUN = true", 'DRY_RUN = "false"'))
    with pytest.raises(ConfigError):
        load_config(bad)
