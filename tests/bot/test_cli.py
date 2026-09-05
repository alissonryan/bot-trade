import logging
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.cli import AlreadyRunning, InstanceLock, warn_token_age


def test_instance_lock_refuses_second_holder(tmp_path):
    path = tmp_path / "bot.lock"
    with InstanceLock(path):
        assert path.read_text().strip().isdigit()
        with pytest.raises(AlreadyRunning):
            with InstanceLock(path):
                pass
    # released: can be taken again
    with InstanceLock(path):
        pass


def test_warn_token_age_levels(caplog):
    caplog.set_level(logging.WARNING, logger="bot")
    assert warn_token_age(None) is None
    assert "KCEX_TOKEN_AT" in caplog.text
    caplog.clear()
    age = warn_token_age("2000-01-01T00:00:00+00:00")
    assert age is not None and age > 6
    assert "dies at" in caplog.text
