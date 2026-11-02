"""Fork-only: suppressed strategies (settings.SUPPRESSED_STRATEGIES).

Upstream's priority-consume-first engine code stays; no user surface may
select it, and a settings.json carrying it loads as the default.
"""
from __future__ import annotations

import json
import logging
import sys
from unittest.mock import patch

import pytest

from claude_swap import cli, engine_control
from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    AUTOSWITCH_STRATEGIES,
    SELECTABLE_STRATEGIES,
    SETTING_SPECS,
    SUPPRESSED_STRATEGIES,
    load_settings,
    set_setting,
)

SUPPRESSED = "priority-consume-first"


def test_suppressed_strategy_is_known_but_not_selectable():
    assert SUPPRESSED in AUTOSWITCH_STRATEGIES
    assert SUPPRESSED in SUPPRESSED_STRATEGIES
    assert SUPPRESSED not in SELECTABLE_STRATEGIES
    assert SETTING_SPECS["autoswitch.strategy"].choices == SELECTABLE_STRATEGIES


def test_config_set_refuses_it(tmp_path):
    with pytest.raises(ConfigError, match="must be one of"):
        set_setting(tmp_path, "autoswitch.strategy", SUPPRESSED)
    assert not (tmp_path / "settings.json").exists()


def test_auto_strategy_flag_refuses_it(capsys):
    with patch.object(sys, "argv", ["cswap", "auto", "--once", "--strategy", SUPPRESSED]):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_auto_help_does_not_mention_it(capsys):
    with patch.object(sys, "argv", ["cswap", "auto", "--help"]):
        with pytest.raises(SystemExit):
            cli.main()
    assert SUPPRESSED not in capsys.readouterr().out


def test_engine_strategy_refuses_it(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        engine_control.run(["strategy", SUPPRESSED, "--json"], backup_root=tmp_path)
    assert exc.value.code == 2
    assert not (tmp_path / "settings.json").exists()


def test_engine_strategy_op_refuses_it_server_side(tmp_path):
    # A client that bypasses the CLI parser still gets bad-request.
    server = engine_control.ControlServer(lambda: None, host="auto", backup_root=tmp_path)
    reply = server._strategy(SUPPRESSED)
    assert (reply["result"], reply["error"]) == ("error", "bad-request")
    assert not (tmp_path / "settings.json").exists()


def test_loader_treats_it_as_an_invalid_value(tmp_path, caplog):
    (tmp_path / "settings.json").write_text(json.dumps({
        "schemaVersion": 1,
        "autoswitch": {"strategy": SUPPRESSED, "threshold": 85},
    }))
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        settings = load_settings(tmp_path)
    assert settings.strategy == "best"
    assert settings.threshold == 85  # the rest of the section still loads
    assert "unsupported autoswitch.strategy" in caplog.text
