import aiosqlite
import logging
import asyncio
import os
import uuid
import secrets
import sqlite3
import re
import urllib.parse
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime
from collections import Counter

from config import DB_PATH, BACKUPS_DIR


logger = logging.getLogger(__name__)
CURRENT_SCHEMA_VERSION = 12


EXPECTED_SCHEMA = {
    "users": {
        "id": ("INTEGER", 0, None, 1),
        "username": ("TEXT", 0, None, 0),
        "first_name": ("TEXT", 0, None, 0),
        "city": ("TEXT", 0, None, 0),
        "notif_on": ("INTEGER", 0, "1", 0),
        "registered_at": ("TEXT", 0, "CURRENT_TIMESTAMP", 0),
        "is_blocked": ("INTEGER", 0, "0", 0),
    },
    "anketas": {
        "id": ("INTEGER", 0, None, 1),
        "code": ("TEXT", 1, None, 0),
        "name": ("TEXT", 0, None, 0),
        "age": ("INTEGER", 0, None, 0),
        "city": ("TEXT", 0, None, 0),
        "bio": ("TEXT", 0, None, 0),
        "price_1h": ("INTEGER", 1, "8000", 0),
        "price_3h": ("INTEGER", 1, "17000", 0),
        "price_night": ("INTEGER", 1, "50000", 0),
        "outcall": ("INTEGER", 0, "1", 0),
        "photo_id": ("TEXT", 0, None, 0),
    },
    "favorites": {
        "user_id": ("INTEGER", 0, None, 1),
        "anketa_id": ("INTEGER", 0, None, 2),
    },
    "requests": {
        "id": ("INTEGER", 0, None, 1),
        "unique_id": ("TEXT", 1, None, 0),
        "session_token": ("TEXT", 0, None, 0),
        "user_id": ("INTEGER", 1, None, 0),
        "anketas_id": ("INTEGER", 0, None, 0),
        "service": ("TEXT", 0, None, 0),
        "amount": ("INTEGER", 0, None, 0),
        "payment_method": ("TEXT", 0, "Не выбран", 0),
        "status": ("TEXT", 0, "Ожидает реквизиты", 0),
        "created_at": ("TEXT", 0, "CURRENT_TIMESTAMP", 0),
    },
    "support_msgs": {
        "id": ("INTEGER", 0, None, 1),
        "user_id": ("INTEGER", 0, None, 0),
        "text": ("TEXT", 0, None, 0),
        "is_admin": ("INTEGER", 0, "0", 0),
        "created_at": ("TEXT", 0, "CURRENT_TIMESTAMP", 0),
    },
    "anketa_photos": {
        "id": ("INTEGER", 0, None, 1),
        "anketa_id": ("INTEGER", 0, None, 0),
        "photo_id": ("TEXT", 0, None, 0),
    },
    "anketa_reviews": {
        "id": ("INTEGER", 0, None, 1),
        "anketa_id": ("INTEGER", 0, None, 0),
        "text": ("TEXT", 0, None, 0),
        "created_at": ("TEXT", 0, "CURRENT_TIMESTAMP", 0),
    },
    "services": {
        "id": ("INTEGER", 0, None, 1),
        "name": ("TEXT", 1, None, 0),
        "default_price": ("INTEGER", 1, "0", 0),
    },
    "anketa_services": {
        "anketa_id": ("INTEGER", 0, None, 1),
        "service_id": ("INTEGER", 0, None, 2),
        "custom_price": ("INTEGER", 1, "0", 0),
    },
    "request_services": {
        "request_id": ("INTEGER", 0, None, 1),
        "service_id": ("INTEGER", 0, None, 2),
        "price": ("INTEGER", 1, "0", 0),
    },
    "bot_settings": {
        "key": ("TEXT", 0, None, 1),
        "value": ("TEXT", 0, None, 0),
    },
    "promocodes": {
        "id": ("INTEGER", 0, None, 1),
        "code": ("TEXT", 1, None, 0),
        "discount_percent": ("INTEGER", 1, "10", 0),
        "is_active": ("INTEGER", 0, "1", 0),
    },
}


REQUIRED_INDEXES = {
    "idx_anketas_city": {"table": "anketas", "cols": ["city"], "unique": False, "partial": 0},
    "idx_requests_user": {"table": "requests", "cols": ["user_id"], "unique": False, "partial": 0},
    "idx_anketa_services": {"table": "anketa_services", "cols": ["anketa_id"], "unique": False, "partial": 0},
    "idx_request_services": {"table": "request_services", "cols": ["request_id"], "unique": False, "partial": 0},
}


REQUIRED_UNIQUE_CONSTRAINTS = {
    "requests": ["unique_id", "session_token"],
    "services": ["name"],
    "anketas": ["code"],
    "promocodes": ["code"],
}


REQUIRED_FOREIGN_KEYS = {
    "favorites": [("anketa_id", "anketas", "id", "NO ACTION", "CASCADE", "NONE")],
    "anketa_photos": [("anketa_id", "anketas", "id", "NO ACTION", "CASCADE", "NONE")],
    "anketa_reviews": [("anketa_id", "anketas", "id", "NO ACTION", "CASCADE", "NONE")],
    "anketa_services": [
        ("anketa_id", "anketas", "id", "NO ACTION", "CASCADE", "NONE"),
        ("service_id", "services", "id", "NO ACTION", "CASCADE", "NONE"),
    ],
    "request_services": [
        ("request_id", "requests", "id", "NO ACTION", "CASCADE", "NONE"),
        ("service_id", "services", "id", "NO ACTION", "CASCADE", "NONE"),
    ],
}


AUTOINCREMENT_TABLES = (
    "anketas", "requests", "support_msgs",
    "anketa_photos", "anketa_reviews", "services",
    "promocodes",
)

ID_TABLES = (
    "users", "anketas", "requests", "support_msgs",
    "anketa_photos", "anketa_reviews", "services",
    "promocodes",
)


TABLE_DDL = {
    "users": """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT, first_name TEXT, city TEXT,
            notif_on INTEGER DEFAULT 1,
            registered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_blocked INTEGER DEFAULT 0
        )
    """,
    "anketas": """
        CREATE TABLE IF NOT EXISTS anketas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE CHECK (length(code) = 4 AND code GLOB '[0-9][0-9][0-9][0-9]'),
            name TEXT,
            age INTEGER CHECK (age >= 18),
            city TEXT, bio TEXT,
            price_1h INTEGER NOT NULL DEFAULT 8000 CHECK (price_1h >= 0),
            price_3h INTEGER NOT NULL DEFAULT 17000 CHECK (price_3h >= 0),
            price_night INTEGER NOT NULL DEFAULT 50000 CHECK (price_night >= 0),
            outcall INTEGER DEFAULT 1,
            photo_id TEXT
        )
    """,
    "favorites": """
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER, anketa_id INTEGER,
            PRIMARY KEY (user_id, anketa_id),
            FOREIGN KEY (anketa_id) REFERENCES anketas(id) ON DELETE CASCADE
        )
    """,
    "requests": """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unique_id TEXT NOT NULL UNIQUE,
            session_token TEXT UNIQUE,
            user_id INTEGER NOT NULL,
            anketas_id INTEGER,
            service TEXT, amount INTEGER,
            payment_method TEXT DEFAULT 'Не выбран',
            status TEXT DEFAULT 'Ожидает реквизиты',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "support_msgs": """
        CREATE TABLE IF NOT EXISTS support_msgs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, text TEXT,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "anketa_photos": """
        CREATE TABLE IF NOT EXISTS anketa_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anketa_id INTEGER, photo_id TEXT,
            FOREIGN KEY (anketa_id) REFERENCES anketas(id) ON DELETE CASCADE
        )
    """,
    "anketa_reviews": """
        CREATE TABLE IF NOT EXISTS anketa_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anketa_id INTEGER, text TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (anketa_id) REFERENCES anketas(id) ON DELETE CASCADE
        )
    """,
    "services": """
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            default_price INTEGER NOT NULL DEFAULT 0 CHECK (default_price >= 0)
        )
    """,
    "anketa_services": """
        CREATE TABLE IF NOT EXISTS anketa_services (
            anketa_id INTEGER, service_id INTEGER,
            custom_price INTEGER NOT NULL DEFAULT 0 CHECK (custom_price >= 0),
            PRIMARY KEY (anketa_id, service_id),
            FOREIGN KEY (anketa_id) REFERENCES anketas(id) ON DELETE CASCADE,
            FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
        )
    """,
    "request_services": """
        CREATE TABLE IF NOT EXISTS request_services (
            request_id INTEGER, service_id INTEGER,
            price INTEGER NOT NULL DEFAULT 0 CHECK (price >= 0),
            PRIMARY KEY (request_id, service_id),
            FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE,
            FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
        )
    """,
    "bot_settings": """
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """,
    "promocodes": """
        CREATE TABLE IF NOT EXISTS promocodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            discount_percent INTEGER NOT NULL DEFAULT 10
                CHECK (discount_percent > 0 AND discount_percent <= 100),
            is_active INTEGER DEFAULT 1
        )
    """,
}


DEFAULT_SERVICES = [
    ("Кунилингус", 3000), ("МБР", 2000), ("Анал", 5000),
    ("Окончание в рот", 2000), ("Окончание на лицо", 1500),
    ("Игрушки", 2000), ("Эротический массаж", 3000), ("ГФЕ", 4000),
    ("Анилингус клиенту", 2500), ("Фистинг клиенту", 3000),
    ("Порка", 2000), ("МЖМ", 5000), ("Госпожа", 4000),
    ("Двойное проникновение", 4000), ("Бандаж", 2000),
    ("Рабыня", 3000), ("Лесби-шоу", 6000),
    ("Урологический массаж", 3000), ("Золотой дождь клиенту", 3000),
    ("Семейной паре", 5000),
]

DEFAULT_BOT_SETTINGS = {
    "welcome_text": "👋 <b>Добро пожаловать!</b>\n\nВыберите ваш город:",
    "main_menu_text": "🏠 <b>Главное меню</b>",
    "info_text": "ℹ️ <b>Информация</b>\n\nЗдесь будет текст о гарантиях и правилах.",
    "rules_url": "",
    "terms_url": "",
    "default_price_1h": "8000",
    "default_price_3h": "17000",
    "default_price_night": "50000",
}


class Database:
    def __init__(self, path, backup_dir=None):
        self.path = str(path)
        self.backup_dir = Path(backup_dir) if backup_dir else Path(BACKUPS_DIR)
        self._memory_connection = None
        self._memory_lock = asyncio.Lock()
        real_path = self._get_file_path(self.path)
        if not self._is_memory_database():
            Path(real_path).parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _get_file_path(path_str: str) -> str:
        if path_str.startswith("file:"):
            path_part = path_str[5:].split("?", 1)[0]
            return urllib.parse.unquote(path_part)
        return path_str

    def _is_memory_database(self) -> bool:
        return self.path == ":memory:" or "mode=memory" in self.path

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        return '"' + str(identifier).replace('"', '""') + '"'

    @asynccontextmanager
    async def connect(self):
        is_uri = self.path.startswith("file:")
        if self._is_memory_database():
            async with self._memory_lock:
                if self._memory_connection is None:
                    self._memory_connection = await aiosqlite.connect(
                        self.path,
                        timeout=10.0,
                        check_same_thread=False,
                        uri=is_uri,
                    )
                    self._memory_connection.isolation_level = None
                    self._memory_connection.row_factory = aiosqlite.Row
                    await self._memory_connection.execute("PRAGMA foreign_keys = ON")
                    await self._memory_connection.execute("PRAGMA busy_timeout = 10000")
                yield self._memory_connection
                return

        db = await aiosqlite.connect(
            self.path, timeout=10.0,
            check_same_thread=False, uri=is_uri,
        )
        db.isolation_level = None
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 10000")
            yield db
        finally:
            await db.close()

    async def close(self):
        if self._memory_connection is not None:
            try:
                await self._memory_connection.close()
            finally:
                self._memory_connection = None

    @asynccontextmanager
    async def transaction(self):
        async with self.connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                yield db
                await db.execute("COMMIT")
            except BaseException:
                await self._safe_rollback(db)
                raise

    async def _safe_rollback(self, db):
        if not db.in_transaction:
            return
        try:
            await asyncio.shield(db.execute("ROLLBACK"))
        except BaseException:
            logger.exception("Не удалось выполнить ROLLBACK")

    async def _get_custom_objects(self, db):
        idx, trg, vws = [], [], []
        required_names = list(REQUIRED_INDEXES)
        if required_names:
            placeholders = ",".join("?" for _ in required_names)
            query = f"""
                SELECT name, sql FROM sqlite_master
                WHERE type = 'index' AND sql IS NOT NULL
                  AND name NOT IN ({placeholders})
            """
            async with db.execute(query, required_names) as cur:
                idx = [(row["name"], row["sql"]) for row in await cur.fetchall()]
        else:
            async with db.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND sql IS NOT NULL"
            ) as cur:
                idx = [(row["name"], row["sql"]) for row in await cur.fetchall()]

        async with db.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND sql IS NOT NULL"
        ) as cur:
            trg = [(row["name"], row["sql"]) for row in await cur.fetchall()]

        async with db.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'view' AND sql IS NOT NULL"
        ) as cur:
            vws = [(row["name"], row["sql"]) for row in await cur.fetchall()]
        return idx, trg, vws

    async def _check_unique_constraints(self, db, table_name):
        expected_cols_list = REQUIRED_UNIQUE_CONSTRAINTS.get(table_name, [])
        if not expected_cols_list:
            return True
        quoted_table = self._quote_identifier(table_name)
        async with db.execute(f"PRAGMA index_list({quoted_table})") as cur:
            indexes = await cur.fetchall()
        for expected_col in expected_cols_list:
            found = False
            for index in indexes:
                if not index["unique"] or index["partial"]:
                    continue
                index_name = self._quote_identifier(index["name"])
                async with db.execute(f"PRAGMA index_info({index_name})") as cur:
                    columns = [row["name"] for row in await cur.fetchall()]
                if columns == [expected_col]:
                    found = True
                    break
            if not found:
                return False
        return True

    async def _check_autoincrement(self, db, table_name):
        if table_name not in AUTOINCREMENT_TABLES:
            return True
        async with db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ) as cur:
            row = await cur.fetchone()
        if not row or not row["sql"]:
            return False
        sql = re.sub(r"--.*$", "", row["sql"], flags=re.MULTILINE)
        sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
        sql = sql.upper()
        return bool(
            re.search(r"\bID\b[^,\n]*\bAUTOINCREMENT\b", sql)
            or re.search(r"\bPRIMARY\s+KEY\s*\(\s*ID\s*\)[^,]*\bAUTOINCREMENT\b", sql)
        )

    async def _rebuild_table_if_needed(self, db, table_name):
        quoted_table = self._quote_identifier(table_name)
        async with db.execute(f"PRAGMA table_info({quoted_table})") as cur:
            old_info = await cur.fetchall()
        if not old_info:
            return
        old_cols = {row["name"]: row for row in old_info}
        expected_cols = set(EXPECTED_SCHEMA[table_name])
        needs_rebuild = set(old_cols) != expected_cols

        if not needs_rebuild:
            for col_name, expected in EXPECTED_SCHEMA[table_name].items():
                exp_type, exp_notnull, exp_default, exp_pk = expected
                actual = old_cols[col_name]
                if (actual["pk"] != exp_pk
                    or actual["type"].upper() != exp_type.upper()
                    or actual["notnull"] != exp_notnull):
                    needs_rebuild = True
                    break
                actual_default = actual["dflt_value"]
                if exp_default is None:
                    if actual_default is not None:
                        needs_rebuild = True
                        break
                elif actual_default is None or (
                    str(actual_default).replace("'", "").replace('"', "").upper()
                    != str(exp_default).replace("'", "").replace('"', "").upper()
                ):
                    needs_rebuild = True
                    break

        if not needs_rebuild and not await self._check_autoincrement(db, table_name):
            needs_rebuild = True
        if not needs_rebuild and not await self._check_unique_constraints(db, table_name):
            needs_rebuild = True

        if not needs_rebuild:
            async with db.execute(f"PRAGMA foreign_key_list({quoted_table})") as cur:
                rows = await cur.fetchall()
            actual_fks = sorted(
                (r["from"], r["table"], r["to"], r["on_update"], r["on_delete"], r["match"])
                for r in rows
            )
            expected_fks = sorted(REQUIRED_FOREIGN_KEYS.get(table_name, []))
            if actual_fks != expected_fks:
                needs_rebuild = True

        if needs_rebuild:
            await self._do_rebuild(db, table_name, old_cols)

    async def _backfill_anketa_codes(self, db):
        async with db.execute("PRAGMA table_info(anketas)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "code" not in columns:
            await db.execute("ALTER TABLE anketas ADD COLUMN code TEXT")

        async with db.execute(
            "SELECT id, code FROM anketas ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
        missing = [row["id"] for row in rows if row["code"] is None]
        if not missing:
            return

        used = {
            row["code"]
            for row in rows
            if isinstance(row["code"], str)
            and len(row["code"]) == 4
            and row["code"].isascii()
            and row["code"].isdigit()
        }
        start = secrets.randbelow(10_000)
        available = [
            f"{(start + offset) % 10_000:04d}"
            for offset in range(10_000)
            if f"{(start + offset) % 10_000:04d}" not in used
        ]
        if len(available) < len(missing):
            raise RuntimeError(
                "Невозможно заполнить коды анкет: исчерпан диапазон 0000–9999."
            )

        for anketa_id, code in zip(missing, available, strict=False):
            await db.execute(
                "UPDATE anketas SET code = ? WHERE id = ? AND code IS NULL",
                (code, anketa_id),
            )

    async def _do_rebuild(self, db, table_name, old_cols):
        temp_name = f"{table_name}_temp_{uuid.uuid4().hex[:8]}"
        quoted_temp = self._quote_identifier(temp_name)
        quoted_table = self._quote_identifier(table_name)
        await db.execute(f"DROP TABLE IF EXISTS {quoted_temp}")

        old_sequence = None
        async with db.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'sqlite_sequence'"
        ) as cur:
            if await cur.fetchone():
                async with db.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = ?",
                    (table_name,),
                ) as c:
                    row = await c.fetchone()
                    if row:
                        old_sequence = row["seq"]

        ddl_temp = TABLE_DDL[table_name].replace(
            f"CREATE TABLE IF NOT EXISTS {table_name}",
            f"CREATE TABLE IF NOT EXISTS {quoted_temp}", 1,
        )
        await db.execute(ddl_temp)

        async with db.execute(f"PRAGMA table_info({quoted_temp})") as cur:
            new_cols = [row["name"] for row in await cur.fetchall()]

        if table_name == "anketas":
            price_columns = ("price_1h", "price_3h", "price_night")
            common_cols = [
                c
                for c in new_cols
                if c in old_cols and c not in ("price", *price_columns)
            ]
            insert_cols = list(common_cols)
            select_exprs = [self._quote_identifier(c) for c in common_cols]
            price_defaults = {"price_1h": 8000, "price_3h": 17000, "price_night": 50000}
            for price_column in price_columns:
                insert_cols.append(price_column)
                quoted_price = self._quote_identifier(price_column)
                if price_column in old_cols:
                    fallback = (
                        f", {self._quote_identifier('price')}"
                        if price_column == "price_1h" and "price" in old_cols
                        else ""
                    )
                    select_exprs.append(
                        f"COALESCE({quoted_price}{fallback}, {price_defaults[price_column]})"
                    )
                elif price_column == "price_1h" and "price" in old_cols:
                    select_exprs.append(
                        f"COALESCE({self._quote_identifier('price')}, 8000)"
                    )
                else:
                    select_exprs.append(str(price_defaults[price_column]))
            if insert_cols:
                cols_sql = ", ".join(self._quote_identifier(c) for c in insert_cols)
                expr_sql = ", ".join(select_exprs)
                await db.execute(
                    f"INSERT INTO {quoted_temp} ({cols_sql}) "
                    f"SELECT {expr_sql} FROM {quoted_table}"
                )
        elif table_name == "requests":
            has_unique_id = "unique_id" in old_cols
            source_uid_expr = '"unique_id"' if has_unique_id else "NULL"
            async with db.execute(
                f"SELECT {source_uid_expr} AS unique_id FROM {quoted_table}"
            ) as cur:
                source_uids = []
                while True:
                    chunk = await cur.fetchmany(1000)
                    if not chunk:
                        break
                    source_uids.extend(
                        r["unique_id"] for r in chunk if r["unique_id"]
                    )
            source_counts = Counter(source_uids)
            assigned_uids = set()

            async with db.execute(f"SELECT * FROM {quoted_table}") as cur:
                while True:
                    rows = await cur.fetchmany(500)
                    if not rows:
                        break
                    for row in rows:
                        row_data = dict(row)
                        uid = row_data.get("unique_id") if has_unique_id else None
                        if (not uid or source_counts.get(uid, 0) > 1
                                or uid in assigned_uids):
                            while True:
                                generated_uid = str(uuid.uuid4())
                                if (generated_uid not in source_counts
                                        and generated_uid not in assigned_uids):
                                    uid = generated_uid
                                    break
                        assigned_uids.add(uid)
                        row_data["unique_id"] = uid
                        insert_cols = [c for c in new_cols if c in row_data]
                        if not insert_cols:
                            raise RuntimeError("Не удалось определить колонки для переноса requests.")
                        cols_sql = ", ".join(self._quote_identifier(c) for c in insert_cols)
                        placeholders = ", ".join("?" for _ in insert_cols)
                        values = [row_data[c] for c in insert_cols]
                        await db.execute(
                            f"INSERT INTO {quoted_temp} ({cols_sql}) "
                            f"VALUES ({placeholders})", values,
                        )
        else:
            common_cols = [c for c in new_cols if c in old_cols]
            if common_cols:
                cols_sql = ", ".join(self._quote_identifier(c) for c in common_cols)
                await db.execute(
                    f"INSERT INTO {quoted_temp} ({cols_sql}) "
                    f"SELECT {cols_sql} FROM {quoted_table}"
                )

        await db.execute(f"DROP TABLE {quoted_table}")
        await db.execute(
            f"ALTER TABLE {quoted_temp} "
            f"RENAME TO {self._quote_identifier(table_name)}"
        )

        if old_sequence is not None:
            async with db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?",
                (table_name,),
            ) as cur:
                new_row = await cur.fetchone()
            new_sequence = new_row["seq"] if new_row else 0
            if old_sequence > new_sequence:
                if new_row:
                    await db.execute(
                        "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                        (old_sequence, table_name),
                    )
                else:
                    await db.execute(
                        "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                        (table_name, old_sequence),
                    )

    async def _validate_indexes(self, db):
        for index_name, spec in REQUIRED_INDEXES.items():
            quoted_table = self._quote_identifier(spec["table"])
            async with db.execute(f"PRAGMA index_list({quoted_table})") as cur:
                indexes = await cur.fetchall()
            found = next((i for i in indexes if i["name"] == index_name), None)
            if not found:
                raise RuntimeError(f"Отсутствует обязательный индекс {index_name}")
            if bool(found["unique"]) != spec["unique"]:
                raise RuntimeError(f"Неверная уникальность индекса {index_name}")
            if bool(found["partial"]) != bool(spec["partial"]):
                raise RuntimeError(f"Неверный partial у индекса {index_name}")
            quoted_index = self._quote_identifier(index_name)
            async with db.execute(f"PRAGMA index_info({quoted_index})") as cur:
                columns = [row["name"] for row in await cur.fetchall()]
            if columns != spec["cols"]:
                raise RuntimeError(f"Неверные колонки индекса {index_name}")

        for table_name in REQUIRED_UNIQUE_CONSTRAINTS:
            if not await self._check_unique_constraints(db, table_name):
                raise RuntimeError(f"Нарушены UNIQUE-ограничения {table_name}")

        for table_name in AUTOINCREMENT_TABLES:
            if not await self._check_autoincrement(db, table_name):
                raise RuntimeError(f"В таблице '{table_name}' нет AUTOINCREMENT")

    async def _generate_test_id(self, db):
        while True:
            candidate = secrets.randbelow(2**31) + 1
            exists = False
            for table_name in ID_TABLES:
                quoted_table = self._quote_identifier(table_name)
                async with db.execute(
                    f"SELECT 1 FROM {quoted_table} WHERE id = ? LIMIT 1",
                    (candidate,),
                ) as cur:
                    if await cur.fetchone():
                        exists = True
                        break
            if not exists:
                return candidate

    async def _validate_schema(self, db, check_foreign_keys=True):
        if check_foreign_keys:
            async with db.execute("PRAGMA foreign_keys") as cur:
                row = await cur.fetchone()
            if not row or row[0] != 1:
                raise RuntimeError("Foreign Keys отключены на уровне соединения.")

        for table_name, expected_cols in EXPECTED_SCHEMA.items():
            quoted_table = self._quote_identifier(table_name)
            async with db.execute(f"PRAGMA table_info({quoted_table})") as cur:
                actual_cols = {r["name"]: r for r in await cur.fetchall()}
            if not actual_cols:
                raise RuntimeError(f"Таблица '{table_name}' отсутствует.")
            if set(actual_cols) != set(expected_cols):
                raise RuntimeError(f"Таблица '{table_name}': неверный набор колонок.")
            for col_name, expected in expected_cols.items():
                exp_type, exp_notnull, exp_default, exp_pk = expected
                actual = actual_cols[col_name]
                if actual["pk"] != exp_pk:
                    raise RuntimeError(f"{table_name}.{col_name}: неверный PK.")
                if actual["type"].upper() != exp_type.upper():
                    raise RuntimeError(f"{table_name}.{col_name}: неверный тип.")
                if actual["notnull"] != exp_notnull:
                    raise RuntimeError(f"{table_name}.{col_name}: неверный NOT NULL.")
                actual_default = actual["dflt_value"]
                if exp_default is None:
                    if actual_default is not None:
                        raise RuntimeError(f"{table_name}.{col_name}: лишний DEFAULT.")
                elif actual_default is None or (
                    str(actual_default).replace("'", "").replace('"', "").upper()
                    != str(exp_default).replace("'", "").replace('"', "").upper()
                ):
                    raise RuntimeError(f"{table_name}.{col_name}: неверный DEFAULT.")

        for table_name in EXPECTED_SCHEMA:
            quoted_table = self._quote_identifier(table_name)
            async with db.execute(f"PRAGMA foreign_key_list({quoted_table})") as cur:
                rows = await cur.fetchall()
            actual_fks = sorted(
                (r["from"], r["table"], r["to"], r["on_update"], r["on_delete"], r["match"])
                for r in rows
            )
            expected_fks = sorted(REQUIRED_FOREIGN_KEYS.get(table_name, []))
            if actual_fks != expected_fks:
                raise RuntimeError(f"{table_name}: неверные FK.")

        await self._validate_indexes(db)

        await db.execute("SAVEPOINT check_sp")
        try:
            test_id = await self._generate_test_id(db)
            test_uid = str(uuid.uuid4())
            test_session = str(uuid.uuid4())
            test_service_name = f"__schema_test_{uuid.uuid4().hex[:8]}"
            test_promo = f"TEST{uuid.uuid4().hex[:4]}"
            async with db.execute("SELECT code FROM anketas") as cursor:
                used_anketa_codes = {row["code"] for row in await cursor.fetchall()}
            available_anketa_codes = [
                f"{number:04d}"
                for number in range(10_000)
                if f"{number:04d}" not in used_anketa_codes
            ]
            test_anketa_code = (
                available_anketa_codes[0]
                if available_anketa_codes
                else next(iter(used_anketa_codes), None)
            )
            test_anketa_check_code = (
                available_anketa_codes[1]
                if len(available_anketa_codes) > 1
                else None
            )

            await db.execute("INSERT INTO users (id) VALUES (?)", (test_id,))
            if available_anketa_codes:
                await db.execute(
                    "INSERT INTO anketas (id, code, name, age, city, bio, "
                    "price_1h, price_3h, price_night, outcall, photo_id) "
                    "VALUES (?, ?, 't', 18, 't', 't', 100, 100, 100, 1, 't')",
                    (test_id, test_anketa_code),
                )
                test_anketa_id = test_id
            else:
                async with db.execute("SELECT id FROM anketas LIMIT 1") as cursor:
                    existing_anketa = await cursor.fetchone()
                if existing_anketa is None:
                    raise RuntimeError("Не удалось найти анкету для проверки схемы.")
                test_anketa_id = existing_anketa["id"]
            await db.execute(
                "INSERT INTO services (id, name, default_price) VALUES (?, ?, 100)",
                (test_id, test_service_name),
            )
            await db.execute(
                "INSERT INTO requests (id, unique_id, session_token, user_id) "
                "VALUES (?, ?, ?, ?)",
                (test_id, test_uid, test_session, test_id),
            )
            await db.execute(
                "INSERT INTO promocodes (code, discount_percent) VALUES (?, 10)",
                (test_promo,),
            )

            await db.execute("SAVEPOINT unique_service_test")
            try:
                await db.execute(
                    "INSERT INTO services (name, default_price) VALUES (?, 100)",
                    (test_service_name,),
                )
                raise RuntimeError("UNIQUE services.name не работает.")
            except sqlite3.IntegrityError as exc:
                await db.execute("ROLLBACK TO unique_service_test")
                await db.execute("RELEASE unique_service_test")
                if "unique constraint failed" not in str(exc).lower():
                    raise

            await db.execute("SAVEPOINT unique_request_test")
            try:
                await db.execute(
                    "INSERT INTO requests (unique_id, user_id) VALUES (?, ?)",
                    (test_uid, test_id),
                )
                raise RuntimeError("UNIQUE requests.unique_id не работает.")
            except sqlite3.IntegrityError as exc:
                await db.execute("ROLLBACK TO unique_request_test")
                await db.execute("RELEASE unique_request_test")
                if "unique constraint failed" not in str(exc).lower():
                    raise

            await db.execute("SAVEPOINT unique_session_test")
            try:
                await db.execute(
                    "INSERT INTO requests (unique_id, session_token, user_id) "
                    "VALUES (?, ?, ?)",
                    (str(uuid.uuid4()), test_session, test_id),
                )
                raise RuntimeError("UNIQUE requests.session_token не работает.")
            except sqlite3.IntegrityError as exc:
                await db.execute("ROLLBACK TO unique_session_test")
                await db.execute("RELEASE unique_session_test")
                if "unique constraint failed" not in str(exc).lower():
                    raise

            await db.execute("SAVEPOINT unique_promo_test")
            try:
                await db.execute(
                    "INSERT INTO promocodes (code, discount_percent) VALUES (?, 10)",
                    (test_promo,),
                )
                raise RuntimeError("UNIQUE promocodes.code не работает.")
            except sqlite3.IntegrityError as exc:
                await db.execute("ROLLBACK TO unique_promo_test")
                await db.execute("RELEASE unique_promo_test")
                if "unique constraint failed" not in str(exc).lower():
                    raise

            await db.execute("SAVEPOINT unique_anketa_code_test")
            try:
                await db.execute(
                    "INSERT INTO anketas (code, name, age, city, bio, price_1h, price_3h, price_night, outcall, photo_id) "
                    "VALUES (?, 't3', 18, 't', 't', 100, 100, 100, 1, 't')",
                    (test_anketa_code,),
                )
                raise RuntimeError("UNIQUE anketas.code не работает.")
            except sqlite3.IntegrityError as exc:
                await db.execute("ROLLBACK TO unique_anketa_code_test")
                await db.execute("RELEASE unique_anketa_code_test")
                if "unique constraint failed" not in str(exc).lower():
                    raise

            checks = [
                ("INSERT INTO services (name, default_price) VALUES (?, -1)",
                 "default_price >= 0", (f"{test_service_name}_2",)),
                ("INSERT INTO anketa_services (anketa_id, service_id, custom_price) "
                 "VALUES (?, ?, -1)", "custom_price >= 0", (test_anketa_id, test_id)),
                ("INSERT INTO request_services (request_id, service_id, price) "
                 "VALUES (?, ?, -1)", "price >= 0", (test_id, test_id)),
                ("INSERT INTO promocodes (code, discount_percent) VALUES (?, 0)",
                 "discount_percent > 0", (f"{test_promo}_2",)),
                ("INSERT INTO promocodes (code, discount_percent) VALUES (?, 101)",
                 "discount_percent <= 100", (f"{test_promo}_3",)),
                ("INSERT INTO anketas (code, name, age, city, bio, price_1h, price_3h, price_night, outcall, photo_id) "
                 "VALUES ('ABC', 't4', 18, 't', 't', 100, 100, 100, 1, 't')",
                 "length(code) = 4 AND code GLOB", ()),
            ]
            if test_anketa_check_code is not None:
                checks.extend(
                    [
                        ("INSERT INTO anketas (code, name, age, city, bio, price_1h, "
                         "price_3h, price_night, outcall, photo_id) "
                         "VALUES (?, 't', 17, 't', 't', 100, 100, 100, 1, 't')",
                         "age >= 18", (test_anketa_check_code,)),
                        ("INSERT INTO anketas (code, name, age, city, bio, price_1h, "
                         "price_3h, price_night, outcall, photo_id) "
                         "VALUES (?, 't', 18, 't', 't', -1, 100, 100, 1, 't')",
                         "price_1h >= 0", (test_anketa_check_code,)),
                        ("INSERT INTO anketas (code, name, age, city, bio, price_1h, "
                         "price_3h, price_night, outcall, photo_id) "
                         "VALUES (?, 't', 18, 't', 't', 100, -1, 100, 1, 't')",
                         "price_3h >= 0", (test_anketa_check_code,)),
                        ("INSERT INTO anketas (code, name, age, city, bio, price_1h, "
                         "price_3h, price_night, outcall, photo_id) "
                         "VALUES (?, 't', 18, 't', 't', 100, 100, -1, 1, 't')",
                         "price_night >= 0", (test_anketa_check_code,)),
                    ]
                )
            for sql, check_name, params in checks:
                await db.execute("SAVEPOINT check_item")
                try:
                    await db.execute(sql, params)
                    await db.execute("ROLLBACK TO check_item")
                    await db.execute("RELEASE check_item")
                    raise RuntimeError(f"CHECK {check_name} не работает.")
                except sqlite3.IntegrityError as exc:
                    await db.execute("ROLLBACK TO check_item")
                    await db.execute("RELEASE check_item")
                    if "check constraint failed" not in str(exc).lower():
                        raise RuntimeError(
                            f"Ожидалась ошибка CHECK {check_name}, получено: {exc}"
                        ) from exc
        finally:
            await db.execute("ROLLBACK TO check_sp")
            await db.execute("RELEASE check_sp")

    async def backup(self):
        if self._is_memory_database():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_id = uuid.uuid4().hex
        backup_path = self.backup_dir / (
            f"backup_v{CURRENT_SCHEMA_VERSION}_{timestamp}_{backup_id}.db"
        )
        try:
            async with self.connect() as source:
                async with aiosqlite.connect(str(backup_path)) as target:
                    await source.backup(target)
            async with aiosqlite.connect(str(backup_path)) as check_db:
                async with check_db.execute("PRAGMA integrity_check") as cur:
                    row = await cur.fetchone()
                if not row or row[0] != "ok":
                    raise ValueError("Созданная резервная копия повреждена.")
                async with check_db.execute("PRAGMA foreign_key_check") as cur:
                    if await cur.fetchall():
                        raise ValueError("В резервной копии нарушения FK.")
            return str(backup_path)
        except BaseException:
            try:
                if backup_path.exists():
                    backup_path.unlink()
            except OSError:
                logger.exception("Не удалось удалить незавершённый бэкап: %s", backup_path)
            raise

    async def init(self):
        real_path = self._get_file_path(self.path)
        db_exists = (
            not self._is_memory_database()
            and os.path.exists(real_path)
            and os.path.getsize(real_path) > 0
        )

        async with self.connect() as db:
            await db.execute("PRAGMA journal_mode = WAL")
            async with db.execute("PRAGMA user_version") as cur:
                version = (await cur.fetchone())[0]

            if version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError("Версия БД выше поддерживаемой.")

            if version < CURRENT_SCHEMA_VERSION:
                await db.execute("PRAGMA foreign_keys = OFF")
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    async with db.execute("PRAGMA user_version") as cur:
                        locked_version = (await cur.fetchone())[0]
                    if locked_version > CURRENT_SCHEMA_VERSION:
                        raise RuntimeError("Версия БД стала выше во время ожидания.")

                    if locked_version < CURRENT_SCHEMA_VERSION:
                        if db_exists:
                            await self.backup()

                        custom_indexes, custom_triggers, custom_views = (
                            await self._get_custom_objects(db)
                        )

                        for name, _ in custom_views:
                            await db.execute(
                                f"DROP VIEW IF EXISTS {self._quote_identifier(name)}"
                            )
                        for name, _ in custom_indexes:
                            await db.execute(
                                f"DROP INDEX IF EXISTS {self._quote_identifier(name)}"
                            )
                        for name, _ in custom_triggers:
                            await db.execute(
                                f"DROP TRIGGER IF EXISTS {self._quote_identifier(name)}"
                            )

                        for ddl in TABLE_DDL.values():
                            await db.execute(ddl)

                        await self._backfill_anketa_codes(db)
                        for table_name in EXPECTED_SCHEMA:
                            await self._rebuild_table_if_needed(db, table_name)

                        for index_name, spec in REQUIRED_INDEXES.items():
                            quoted_index = self._quote_identifier(index_name)
                            quoted_table = self._quote_identifier(spec["table"])
                            columns = ", ".join(
                                self._quote_identifier(c) for c in spec["cols"]
                            )
                            unique_sql = "UNIQUE " if spec["unique"] else ""
                            await db.execute(f"DROP INDEX IF EXISTS {quoted_index}")
                            await db.execute(
                                f"CREATE {unique_sql}INDEX {quoted_index} "
                                f"ON {quoted_table} ({columns})"
                            )

                        for service_name, price in DEFAULT_SERVICES:
                            await db.execute(
                                "INSERT OR IGNORE INTO services (name, default_price) "
                                "VALUES (?, ?)", (service_name, price),
                            )

                        for key, value in DEFAULT_BOT_SETTINGS.items():
                            await db.execute(
                                "INSERT OR IGNORE INTO bot_settings (key, value) "
                                "VALUES (?, ?)", (key, value),
                            )

                        for object_name, object_sql in custom_indexes:
                            try:
                                await db.execute(object_sql)
                            except sqlite3.OperationalError as exc:
                                if "no such column" in str(exc).lower():
                                    logger.warning(
                                        "Индекс '%s' пропущен: %s", object_name, exc,
                                    )
                                else:
                                    logger.exception("Ошибка индекса '%s'", object_name)
                                    raise

                        for object_name, object_sql in custom_triggers:
                            try:
                                await db.execute(object_sql)
                            except sqlite3.Error:
                                logger.exception("Ошибка триггера '%s'", object_name)
                                raise

                        remaining_views = list(custom_views)
                        max_attempts = len(remaining_views) * 2 + 1
                        for _ in range(max_attempts):
                            if not remaining_views:
                                break
                            next_remaining = []
                            for view_name, view_sql in remaining_views:
                                quoted_view = self._quote_identifier(view_name)
                                try:
                                    await db.execute(view_sql)
                                    async with db.execute(
                                        f"SELECT * FROM {quoted_view} LIMIT 0"
                                    ):
                                        pass
                                except sqlite3.OperationalError as exc:
                                    error_text = str(exc).lower()
                                    if ("no such table" in error_text
                                            or "no such view" in error_text):
                                        await db.execute(
                                            f"DROP VIEW IF EXISTS {quoted_view}"
                                        )
                                        next_remaining.append((view_name, view_sql))
                                    else:
                                        logger.exception("Ошибка VIEW '%s'", view_name)
                                        raise
                            if len(next_remaining) == len(remaining_views):
                                raise RuntimeError(
                                    f"Неразрешимые VIEW: "
                                    f"{[n for n, _ in next_remaining]}"
                                )
                            remaining_views = next_remaining

                        async with db.execute("PRAGMA foreign_key_check") as cur:
                            if await cur.fetchall():
                                raise RuntimeError("Нарушения FK после миграции.")

                        await self._validate_schema(db, check_foreign_keys=False)
                        await db.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                        await db.execute("COMMIT")
                    else:
                        await db.execute("ROLLBACK")
                except BaseException:
                    await self._safe_rollback(db)
                    raise
                finally:
                    await db.execute("PRAGMA foreign_keys = ON")

            async with db.execute("PRAGMA foreign_key_check") as cur:
                if await cur.fetchall():
                    raise RuntimeError("Нарушения FK при запуске.")

            async with db.execute("PRAGMA integrity_check") as cur:
                row = await cur.fetchone()
            if not row or row[0] != "ok":
                raise RuntimeError(
                    f"Ошибка целостности БД: {row[0] if row else 'unknown'}"
                )

            await self._validate_schema(db)


db_manager = Database(DB_PATH)
