import logging
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from database import db_manager

logger = logging.getLogger(__name__)


class RequestConflictError(Exception):
    """Raised when an existing session token is reused for different data."""


def _validate_request_data(
    user_id: int,
    anketa_id: int,
    service: str,
    amount: int,
    payment_method: str,
    session_token: str,
    selected_services: Sequence[Mapping[str, Any]],
) -> list[dict[str, int]]:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id должен быть положительным целым числом.")
    if isinstance(anketa_id, bool) or not isinstance(anketa_id, int) or anketa_id <= 0:
        raise ValueError("anketa_id должен быть положительным целым числом.")
    if not isinstance(service, str) or not service.strip():
        raise ValueError("Название услуги не может быть пустым.")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("Сумма заявки должна быть неотрицательным целым числом.")
    if not isinstance(payment_method, str) or not payment_method.strip():
        raise ValueError("Способ оплаты не может быть пустым.")
    if not isinstance(session_token, str) or not session_token.strip():
        raise ValueError("Токен сессии не может быть пустым.")

    normalized_services = []
    seen_ids = set()
    for selected in selected_services:
        service_id = selected.get("id")
        price = selected.get("price")
        if isinstance(service_id, bool) or not isinstance(service_id, int) or service_id <= 0:
            raise ValueError("id дополнительной услуги должен быть положительным.")
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            raise ValueError("Цена дополнительной услуги должна быть неотрицательной.")
        if service_id in seen_ids:
            raise ValueError("Дополнительная услуга не может быть выбрана дважды.")
        seen_ids.add(service_id)
        normalized_services.append({"id": service_id, "price": price})

    return normalized_services


async def _get_request_services(db, request_id: int) -> list[dict[str, int]]:
    async with db.execute(
        """
        SELECT service_id AS id, price
        FROM request_services
        WHERE request_id = ?
        ORDER BY service_id
        """,
        (request_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"id": row["id"], "price": row["price"]} for row in rows]


async def _get_request_by_session(db, session_token: str):
    async with db.execute(
        """
        SELECT id, unique_id, user_id, anketas_id, service, amount,
               payment_method, status
        FROM requests
        WHERE session_token = ?
        """,
        (session_token,),
    ) as cursor:
        return await cursor.fetchone()


async def _existing_request_result(
    db,
    existing,
    *,
    user_id: int,
    anketa_id: int,
    service: str,
    amount: int,
    payment_method: str,
    expected_services: list[dict[str, int]],
) -> dict[str, Any]:
    existing_services = await _get_request_services(db, existing["id"])
    same_request = (
        existing["user_id"] == user_id
        and existing["anketas_id"] == anketa_id
        and existing["service"] == service
        and existing["amount"] == amount
        and existing["payment_method"] == payment_method
        and existing_services == expected_services
    )
    if not same_request:
        raise RequestConflictError("Токен оформления уже использован для другой заявки.")

    result = {key: existing[key] for key in existing.keys()}
    result["created"] = False
    return result


async def create_request_idempotent(
    *,
    user_id: int,
    anketa_id: int,
    service: str,
    amount: int,
    payment_method: str,
    session_token: str,
    selected_services: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Create one request per checkout session, returning existing identical requests."""
    services = _validate_request_data(
        user_id,
        anketa_id,
        service,
        amount,
        payment_method,
        session_token,
        selected_services,
    )
    expected_services = sorted(services, key=lambda item: item["id"])

    async with db_manager.transaction() as db:
        existing = await _get_request_by_session(db, session_token)

        if existing:
            return await _existing_request_result(
                db,
                existing,
                user_id=user_id,
                anketa_id=anketa_id,
                service=service,
                amount=amount,
                payment_method=payment_method,
                expected_services=expected_services,
            )

        request_id = None
        unique_id = None
        for _ in range(20):
            candidate = str(secrets.randbelow(90_000_000) + 10_000_000)
            try:
                async with db.execute(
                    """
                    INSERT INTO requests (
                        unique_id, session_token, user_id, anketas_id, service,
                        amount, payment_method
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate,
                        session_token,
                        user_id,
                        anketa_id,
                        service,
                        amount,
                        payment_method,
                    ),
                ) as cursor:
                    request_id = cursor.lastrowid
                unique_id = candidate
                break
            except sqlite3.IntegrityError:
                existing = await _get_request_by_session(db, session_token)
                if existing is not None:
                    return await _existing_request_result(
                        db,
                        existing,
                        user_id=user_id,
                        anketa_id=anketa_id,
                        service=service,
                        amount=amount,
                        payment_method=payment_method,
                        expected_services=expected_services,
                    )

                async with db.execute(
                    "SELECT 1 FROM requests WHERE unique_id = ?",
                    (candidate,),
                ) as cursor:
                    unique_id_exists = await cursor.fetchone() is not None
                if not unique_id_exists:
                    raise

        if request_id is None or unique_id is None:
            raise RuntimeError("Не удалось сгенерировать уникальный номер заявки.")

        if services:
            await db.executemany(
                """
                INSERT INTO request_services (request_id, service_id, price)
                VALUES (?, ?, ?)
                """,
                [(request_id, item["id"], item["price"]) for item in services],
            )

        async with db.execute(
            """
            SELECT id, unique_id, user_id, anketas_id, service, amount,
                   payment_method, status
            FROM requests
            WHERE id = ?
            """,
            (request_id,),
        ) as cursor:
            created = await cursor.fetchone()

        if created is None:
            raise RuntimeError("Созданная заявка не найдена в базе данных.")

        result = {key: created[key] for key in created.keys()}
        result["created"] = True

    logger.info(
        "Создана заявка %s (user_id=%s, anketa_id=%s)",
        unique_id,
        user_id,
        anketa_id,
    )
    return result
