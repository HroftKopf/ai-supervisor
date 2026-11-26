from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from ai_supervisor.memory.database import Database


@pytest.mark.asyncio
async def test_concurrent_writes_and_reads_are_serialized(tmp_path: Path) -> None:
    db = Database(tmp_path / "concurrent.db")
    await db.connect()
    try:
        await db.execute(
            "CREATE TABLE bench(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL)"
        )

        async def write(index: int) -> None:
            await db.execute("INSERT INTO bench(value) VALUES (?)", (str(index),))

        await asyncio.gather(*(write(index) for index in range(40)))
        count = await db.scalar("SELECT COUNT(*) FROM bench")
        assert int(count) == 40
        rows = await asyncio.gather(*(db.scalar("SELECT COUNT(*) FROM bench") for _ in range(20)))
        assert all(int(value) == 40 for value in rows)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "rollback.db")
    await db.connect()
    try:
        await db.execute("CREATE TABLE tx(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        with pytest.raises(RuntimeError, match="boom"):
            async with db.transaction() as conn:
                await conn.execute("INSERT INTO tx(value) VALUES ('kept?')")
                raise RuntimeError("boom")
        assert int(await db.scalar("SELECT COUNT(*) FROM tx")) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_newer_schema_is_refused_without_changes(tmp_path: Path) -> None:
    path = tmp_path / "newer.db"
    db = Database(path)
    await db.connect()
    await db.execute("UPDATE schema_meta SET value='999' WHERE key='schema_version'")
    await db.close()

    newer = Database(path)
    with pytest.raises(RuntimeError, match="newer than supported"):
        await newer.connect()


@pytest.mark.asyncio
async def test_corrupt_database_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a sqlite database")
    db = Database(path)
    with pytest.raises(sqlite3.DatabaseError):
        await db.connect()
