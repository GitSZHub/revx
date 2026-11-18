import logging
from logging.handlers import TimedRotatingFileHandler

import main
from revx.config import load_config


def test_file_log_keeps_the_last_24_hours(tmp_path):
    handler = main.setup_file_log(tmp_path / "logs")
    try:
        assert isinstance(handler, TimedRotatingFileHandler)
        assert handler.when == "H" and handler.interval == 3600 and handler.utc
        assert handler.backupCount == 24
        logging.getLogger("revx").warning("hello file")
        handler.flush()
        assert "hello file" in (tmp_path / "logs" / "revx.log").read_text(encoding="utf-8")
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def test_logs_path_defaults_to_logs_folder():
    cfg = load_config()
    assert cfg.paths.logs.name == "logs"
