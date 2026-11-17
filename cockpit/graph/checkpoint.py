"""Durable graph state: a SQLite checkpointer next to cockpit.db, one thread per invoice run (thread_id = run id).

Checkpoints are deserialized with an explicit allowlist of the cockpit's own dataclasses, so a tampered
checkpoint file cannot make the process construct arbitrary Python objects."""
from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver


def _state_types() -> list[type]:
    from ..core import domain, duplicates, engine, matching, resolve, rules
    from .. import extraction
    out = []
    for mod in (domain, duplicates, engine, matching, resolve, rules, extraction):
        out += [v for v in vars(mod).values()
                if isinstance(v, type) and dataclasses.is_dataclass(v) and v.__module__ == mod.__name__]
    return out


def serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=_state_types())


def saver(data_dir: str | Path) -> SqliteSaver:
    path = Path(data_dir) / "graph.db"
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    cp = SqliteSaver(conn, serde=serializer())
    cp.setup()
    return cp


def clear(cp: SqliteSaver) -> None:
    """Demo reset: drop every thread."""
    with cp.lock:
        for table in ("checkpoints", "writes"):
            cp.conn.execute(f"DELETE FROM {table}")
