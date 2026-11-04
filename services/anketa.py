from __future__ import annotations

import sqlite3
import secrets

from database import db_manager


def _is_int(value: object) -> bool:
    """True только для обычного int, но не для bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_code(code: str | int | None) -> str | None:
    """Проверяет четырёхзначный ASCII-код в том же формате, что и SQLite CHECK."""
    if code is None:
        return None

    value = str(code).strip()
    if len(value) != 4 or not value.isascii() or not value.isdigit():
        raise ValueError("Код анкеты должен состоять ровно из 4 арабских цифр (0–9).")
    return value


async def create_anketa(
    name: str,
    age: int,
    city: str,
    bio: str = "",
    price_1h: int = 8000,
    price_3h: int = 17000,
    price_night: int = 50000,
    outcall: int = 1,
    photo_id: str | None = None,
    code: str | int | None = None,
) -> int:
    """Создаёт анкету по актуальной схеме database.py и возвращает её ID."""

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Имя анкеты не может быть пустым.")
    if not isinstance(city, str) or not city.strip():
        raise ValueError("Город анкеты не может быть пустым.")
    if not _is_int(age) or age < 18:
        raise ValueError("Возраст должен быть целым числом не меньше 18.")

    prices = (price_1h, price_3h, price_night)
    if any(not _is_int(price) or price < 0 for price in prices):
        raise ValueError("Цены должны быть целыми неотрицательными числами.")

    # bool является подклассом int: отдельно запрещаем True/False.
    if not _is_int(outcall) or outcall not in (0, 1):
        raise ValueError("outcall должен быть целым числом 0 или 1.")

    if bio is None:
        bio = ""
    if not isinstance(bio, str):
        raise ValueError("Описание анкеты должно быть строкой.")
    if photo_id is not None and not isinstance(photo_id, str):
        raise ValueError("photo_id должен быть строкой или None.")

    normalized_code = _normalize_code(code)

    try:
        async with db_manager.transaction() as db:
            if normalized_code is None:
                async with db.execute(
                    "SELECT code FROM anketas WHERE code IS NOT NULL"
                ) as cursor:
                    used_codes = {row["code"] for row in await cursor.fetchall()}
                start = secrets.randbelow(10_000)
                candidates = (
                    f"{(start + offset) % 10_000:04d}"
                    for offset in range(10_000)
                )
            else:
                used_codes = set()
                candidates = iter((normalized_code,))

            for candidate in candidates:
                if candidate in used_codes:
                    if normalized_code is not None:
                        raise ValueError("Этот код анкеты уже используется.")
                    continue
                try:
                    cursor = await db.execute(
                        """
                        INSERT INTO anketas (
                            code, name, age, city, bio,
                            price_1h, price_3h, price_night, outcall, photo_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate,
                            name.strip(),
                            age,
                            city.strip(),
                            bio.strip(),
                            price_1h,
                            price_3h,
                            price_night,
                            outcall,
                            photo_id,
                        ),
                    )
                    return int(cursor.lastrowid)
                except sqlite3.IntegrityError as exc:
                    if "unique constraint failed: anketas.code" not in str(exc).lower():
                        raise
                    if normalized_code is not None:
                        raise ValueError("Этот код анкеты уже используется.") from exc
                    used_codes.add(candidate)

            raise ValueError("Исчерпаны все доступные четырёхзначные коды анкет.")
    except sqlite3.IntegrityError as exc:
        message = str(exc).lower()
        if "unique constraint failed: anketas.code" in message:
            raise ValueError("Этот код анкеты уже используется.") from exc
        raise


async def get_anketa_by_id(anketa_id: int):
    """Возвращает одну анкету или None."""
    if not _is_int(anketa_id) or anketa_id < 1:
        return None

    async with db_manager.connect() as db:
        async with db.execute(
            "SELECT * FROM anketas WHERE id = ?",
            (anketa_id,),
        ) as cur:
            return await cur.fetchone()


async def get_anketa_by_code(code: str | int):
    """Возвращает анкету по четырёхзначному ASCII-коду или None."""
    try:
        normalized_code = _normalize_code(code)
    except (TypeError, ValueError):
        return None

    if normalized_code is None:
        return None

    async with db_manager.connect() as db:
        async with db.execute(
            "SELECT * FROM anketas WHERE code = ?",
            (normalized_code,),
        ) as cur:
            return await cur.fetchone()


async def get_anketas_for_user(user_city: str):
    """Возвращает анкеты своего города и анкеты с включённым выездом."""
    normalized_city = user_city.strip() if isinstance(user_city, str) else ""

    async with db_manager.connect() as db:
        async with db.execute(
            """
            SELECT *
            FROM anketas
            WHERE city = ? OR outcall = 1
            ORDER BY id DESC
            """,
            (normalized_city,),
        ) as cur:
            return await cur.fetchall()
