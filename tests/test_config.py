import pytest
from pydantic import ValidationError

from config import Settings


def test_settings_parse_and_deduplicate_admin_ids():
    settings = Settings(
        _env_file=None,
        bot_token="123456789:TEST_TOKEN_FOR_LOCAL_TESTS",
        admin_ids="123, 456,123",
    )

    assert settings.admin_ids == (123, 456)
    assert settings.bot_id == 123456789


def test_settings_load_values_from_env_file(tmp_path, monkeypatch):
    for variable in ("BOT_TOKEN", "ADMIN_IDS", "LOG_LEVEL", "NOMINATIM_CONTACT"):
        monkeypatch.delenv(variable, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOT_TOKEN=123456789:TEST_TOKEN_FOR_LOCAL_TESTS\n"
        "ADMIN_IDS=123,456\n"
        "LOG_LEVEL=debug\n"
        "NOMINATIM_CONTACT=ops@example.test\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.admin_ids == (123, 456)
    assert settings.log_level == "DEBUG"
    assert settings.nominatim_contact == "ops@example.test"


def test_settings_reject_invalid_bot_token_without_echoing_it():
    secret_like_value = "not-a-valid-secret"

    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            bot_token=secret_like_value,
            admin_ids="123",
        )

    assert secret_like_value not in str(error.value)


def test_settings_require_admin_ids(monkeypatch):
    monkeypatch.delenv("ADMIN_IDS")
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            bot_token="123456789:TEST_TOKEN_FOR_LOCAL_TESTS",
        )
