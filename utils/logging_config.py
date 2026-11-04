import logging
from logging.handlers import RotatingFileHandler

from config import LOG_FORMAT, LOG_LEVEL_NUM, LOGS_DIR


def setup_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "bot.log"

    handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler()

    formatter = logging.Formatter(LOG_FORMAT)
    handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logging.basicConfig(
        level=LOG_LEVEL_NUM,
        handlers=[handler, console_handler],
        force=True,
    )
