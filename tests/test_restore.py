import sqlite3
from pathlib import Path

import pytest

import restore
from database import Database


async def _initialized_database(path, backup_dir):
    database = Database(path, backup_dir=backup_dir)
    await database.init()
    return database


@pytest.mark.asyncio
async def test_safe_restore_replaces_database_from_validated_backup(tmp_path):
    source = await _initialized_database(
        tmp_path / "source.db",
        tmp_path / "source-backups",
    )
    destination_path = tmp_path / "destination.db"
    destination = await _initialized_database(
        destination_path,
        tmp_path / "destination-backups",
    )

    async with source.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            (1001, "from_backup"),
        )
    async with destination.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            (1002, "old_database"),
        )

    backup_path = await source.backup()
    assert backup_path is not None

    await restore.safe_restore(
        backup_path,
        destination_path,
        backup_dir=tmp_path / "restore-backups",
    )

    async with destination.connect() as db:
        async with db.execute(
            "SELECT username FROM users WHERE id = ?",
            (1001,),
        ) as cursor:
            assert (await cursor.fetchone())["username"] == "from_backup"
        async with db.execute(
            "SELECT 1 FROM users WHERE id = ?",
            (1002,),
        ) as cursor:
            assert await cursor.fetchone() is None

    assert not (tmp_path / "destination.db-wal").exists()
    assert not (tmp_path / "destination.db-shm").exists()


@pytest.mark.asyncio
async def test_invalid_backup_does_not_change_destination(tmp_path):
    destination_path = tmp_path / "destination.db"
    destination = await _initialized_database(
        destination_path,
        tmp_path / "destination-backups",
    )
    async with destination.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            (1002, "keep_me"),
        )

    invalid_backup = tmp_path / "invalid.db"
    invalid_backup.write_bytes(b"not a SQLite database")

    with pytest.raises(sqlite3.DatabaseError):
        await restore.safe_restore(
            invalid_backup,
            destination_path,
            backup_dir=tmp_path / "restore-backups",
        )

    async with destination.connect() as db:
        async with db.execute(
            "SELECT username FROM users WHERE id = ?",
            (1002,),
        ) as cursor:
            assert (await cursor.fetchone())["username"] == "keep_me"


@pytest.mark.asyncio
async def test_failed_atomic_replace_keeps_destination_database(tmp_path, monkeypatch):
    source = await _initialized_database(
        tmp_path / "source.db",
        tmp_path / "source-backups",
    )
    destination_path = tmp_path / "destination.db"
    destination = await _initialized_database(
        destination_path,
        tmp_path / "destination-backups",
    )
    async with destination.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            (1002, "keep_me"),
        )

    backup_path = await source.backup()
    assert backup_path is not None

    def fail_replace(_source, _destination):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(restore.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        await restore.safe_restore(
            backup_path,
            destination_path,
            backup_dir=tmp_path / "restore-backups",
        )

    async with destination.connect() as db:
        async with db.execute(
            "SELECT username FROM users WHERE id = ?",
            (1002,),
        ) as cursor:
            assert (await cursor.fetchone())["username"] == "keep_me"


@pytest.mark.asyncio
async def test_failed_post_restore_validation_rolls_back_previous_database(
    tmp_path,
    monkeypatch,
):
    source = await _initialized_database(
        tmp_path / "source.db",
        tmp_path / "source-backups",
    )
    destination_path = tmp_path / "destination.db"
    destination = await _initialized_database(
        destination_path,
        tmp_path / "destination-backups",
    )
    async with destination.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            (1002, "keep_me"),
        )

    backup_path = await source.backup()
    assert backup_path is not None
    original_validate = restore._validate_database
    validation_calls = 0

    async def fail_after_install(path):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 2:
            raise RuntimeError("simulated post-install validation failure")
        await original_validate(path)

    monkeypatch.setattr(restore, "_validate_database", fail_after_install)
    with pytest.raises(RuntimeError, match="simulated post-install validation failure"):
        await restore.safe_restore(
            backup_path,
            destination_path,
            backup_dir=tmp_path / "restore-backups",
        )

    async with destination.connect() as db:
        async with db.execute(
            "SELECT username FROM users WHERE id = ?",
            (1002,),
        ) as cursor:
            assert (await cursor.fetchone())["username"] == "keep_me"


@pytest.mark.asyncio
async def test_failed_checkpoint_preserves_destination_and_sidecars(
    tmp_path,
    monkeypatch,
):
    source = await _initialized_database(
        tmp_path / "checkpoint-source.db",
        tmp_path / "source-backups",
    )
    destination_path = tmp_path / "checkpoint-destination.db"
    destination = await _initialized_database(
        destination_path,
        tmp_path / "destination-backups",
    )
    async with destination.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, username) VALUES (1003, 'keep_after_checkpoint_error')"
        )
    backup_path = await source.backup()
    before = destination_path.read_bytes()
    wal_path = tmp_path / "checkpoint-destination.db-wal"
    shm_path = tmp_path / "checkpoint-destination.db-shm"

    async def fail_checkpoint(path):
        Path(f"{path}-wal").write_bytes(b"preserve wal")
        Path(f"{path}-shm").write_bytes(b"preserve shm")
        raise RuntimeError("simulated busy checkpoint")

    monkeypatch.setattr(restore, "_checkpoint_and_close", fail_checkpoint)
    with pytest.raises(RuntimeError, match="simulated busy checkpoint"):
        await restore.safe_restore(
            backup_path,
            destination_path,
            backup_dir=tmp_path / "restore-backups",
        )

    assert destination_path.read_bytes() == before
    assert wal_path.read_bytes() == b"preserve wal"
    assert shm_path.read_bytes() == b"preserve shm"


@pytest.mark.asyncio
async def test_rollback_checkpoint_failure_keeps_backup_and_does_not_delete_sidecars(
    tmp_path,
    monkeypatch,
):
    source = await _initialized_database(
        tmp_path / "rollback-source.db",
        tmp_path / "source-backups",
    )
    destination_path = tmp_path / "rollback-destination.db"
    destination = await _initialized_database(
        destination_path,
        tmp_path / "destination-backups",
    )
    async with destination.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, username) VALUES (1004, 'old_database')"
        )
    backup_path = await source.backup()
    original_validate = restore._validate_database
    original_checkpoint = restore._checkpoint_and_close
    validations = 0
    checkpoints = 0

    async def fail_installed_validation(path):
        nonlocal validations
        validations += 1
        if validations == 2:
            raise RuntimeError("original post-restore validation failure")
        await original_validate(path)

    async def fail_rollback_checkpoint(path):
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 1:
            return await original_checkpoint(path)
        Path(f"{path}-wal").write_bytes(b"preserve failed rollback wal")
        raise RuntimeError("rollback checkpoint failure")

    monkeypatch.setattr(restore, "_validate_database", fail_installed_validation)
    monkeypatch.setattr(restore, "_checkpoint_and_close", fail_rollback_checkpoint)
    with pytest.raises(RuntimeError, match="original post-restore validation failure") as error:
        await restore.safe_restore(
            backup_path,
            destination_path,
            backup_dir=tmp_path / "restore-backups",
        )

    assert any("ручное вмешательство" in note.lower() for note in getattr(error.value, "__notes__", []))
    assert Path(f"{destination_path}-wal").read_bytes() == b"preserve failed rollback wal"
    assert Path(backup_path).is_file()
