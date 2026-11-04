import logging
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    bot_token: str = Field(min_length=1)
    admin_ids: Annotated[tuple[int, ...], NoDecode]
    db_path: Path = BASE_DIR / "bot.db"
    logs_dir: Path = BASE_DIR / "logs"
    backups_dir: Path = BASE_DIR / "backups"
    nominatim_contact: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("bot_token")
    @classmethod
    def validate_bot_token(cls, value: str) -> str:
        token = value.strip()
        if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{10,}", token):
            raise ValueError(
                "BOT_TOKEN имеет неверный формат; ожидается значение вида " "123456789:ABCdef..."
            )
        return token

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            values = value.split(",")
        elif isinstance(value, (tuple, list)):
            values = value
        else:
            raise ValueError("ADMIN_IDS укажите через запятую.")

        if not values:
            raise ValueError("ADMIN_IDS должен содержать хотя бы один ID.")

        parsed = []
        for position, item in enumerate(values, start=1):
            raw_id = str(item).strip()
            if not re.fullmatch(r"[0-9]+", raw_id):
                raise ValueError(f"ADMIN_IDS: некорректный ID на позиции {position}.")
            admin_id = int(raw_id)
            if admin_id <= 0:
                raise ValueError(f"ADMIN_IDS: ID на позиции {position} должен быть положительным.")
            parsed.append(admin_id)

        return tuple(dict.fromkeys(parsed))

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @property
    def bot_id(self) -> int:
        return int(self.bot_token.split(":", maxsplit=1)[0])


settings = Settings()

BOT_TOKEN = settings.bot_token
BOT_ID = settings.bot_id
ADMIN_IDS = settings.admin_ids
DB_PATH = settings.db_path
LOGS_DIR = settings.logs_dir
BACKUPS_DIR = settings.backups_dir
LOG_FILE = LOGS_DIR / "bot.log"
LOG_LEVEL = settings.log_level
NOMINATIM_CONTACT = settings.nominatim_contact.strip()
LOG_LEVEL_NUM = getattr(logging, LOG_LEVEL)
LOG_FORMAT = "[%(levelname)s] %(asctime)s - %(name)s - %(message)s"

STATUS_WAITING = "Ожидает реквизиты"
STATUS_PAID = "Оплачено"
STATUS_IN_PROGRESS = "В работе"
STATUS_DONE = "Завершено"
STATUS_CANCELED = "Отменено"

ALL_STATUSES = (
    STATUS_WAITING,
    STATUS_PAID,
    STATUS_IN_PROGRESS,
    STATUS_DONE,
    STATUS_CANCELED,
)

PAYMENT_CARD = "Карта"
PAYMENT_CRYPTO = "Криптовалюта"
PAYMENT_CASH = "Наличные"

ALL_PAYMENT_METHODS = (
    PAYMENT_CARD,
    PAYMENT_CRYPTO,
    PAYMENT_CASH,
)


def ensure_directories() -> None:
    for directory in (DB_PATH.parent, LOGS_DIR, BACKUPS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def print_config_summary() -> None:
    print("=" * 60)
    print("КОНФИГУРАЦИЯ БОТА")
    print("=" * 60)
    print(f"Базовая директория: {BASE_DIR}")
    print(f"Файл .env:          {ENV_FILE}")
    print(f"База данных:        {DB_PATH}")
    print(f"Файл логов:         {LOG_FILE}")
    print(f"Резервные копии:    {BACKUPS_DIR}")
    print(f"Количество админов: {len(ADMIN_IDS)}")
    print(f"ID бота:            {BOT_ID}")
    print("Токен:              загружен")
    print(f"Уровень логирования: {LOG_LEVEL}")
    print("=" * 60)


if __name__ == "__main__":
    print_config_summary()
