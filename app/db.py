"""SQLite 持久化层。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    total_pieces  INTEGER NOT NULL CHECK (total_pieces >= 0),
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS count_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id      INTEGER NOT NULL REFERENCES count_reports(id) ON DELETE CASCADE,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    lo             INTEGER NOT NULL CHECK (lo >= 0),
    hi             INTEGER NOT NULL CHECK (hi > lo),
    count          INTEGER NOT NULL CHECK (count >= 0)
);

-- 复卷纸追溯：原卷（kind='raw'）与成卷（kind='finished'）统一登记。
-- 原卷登记长度并关联已有质检登记号；成卷长度为各接段长度之和。
CREATE TABLE IF NOT EXISTS rolls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL CHECK (kind IN ('raw', 'finished')),
    length_mm      INTEGER NOT NULL CHECK (length_mm > 0),
    inspection_id  INTEGER REFERENCES inspections(id),
    note           TEXT,
    created_at     TEXT NOT NULL,
    CHECK (kind = 'finished' OR inspection_id IS NOT NULL)
);

-- 成卷的接段：按 seq 记录接入顺序，来源可以是原卷或另一成卷。
-- 区间含起点不含终点：source_lo <= x < source_hi。
CREATE TABLE IF NOT EXISTS splices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    roll_id         INTEGER NOT NULL REFERENCES rolls(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL CHECK (seq >= 1),
    source_roll_id  INTEGER NOT NULL REFERENCES rolls(id),
    source_lo       INTEGER NOT NULL CHECK (source_lo >= 0),
    source_hi       INTEGER NOT NULL CHECK (source_hi > source_lo),
    reversed        INTEGER NOT NULL CHECK (reversed IN (0, 1)),
    UNIQUE (roll_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_reports_inspection
    ON count_reports(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_inspection
    ON segments(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_report
    ON segments(report_id);
CREATE INDEX IF NOT EXISTS idx_splices_roll
    ON splices(roll_id);
CREATE INDEX IF NOT EXISTS idx_splices_source
    ON splices(source_roll_id);
CREATE INDEX IF NOT EXISTS idx_rolls_inspection
    ON rolls(inspection_id);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开一个请求级连接。

    ``isolation_level=None`` 让我们显式使用 ``BEGIN IMMEDIATE`` 控制写事务，
    从而把“读取现有计数 + 校验 + 写入”放在同一个串行化事务里。

    ``check_same_thread=False``：每条连接只服务一个请求，但依赖清理与
    端点可能落在不同的线程池线程上；连接不会跨请求共享。
    """
    connection = sqlite3.connect(
        path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
    finally:
        connection.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_inspection(
    connection: sqlite3.Connection, total_pieces: int, note: str | None
) -> int:
    cursor = connection.execute(
        "INSERT INTO inspections (total_pieces, note, created_at) VALUES (?, ?, ?)",
        (total_pieces, note, now()),
    )
    return int(cursor.lastrowid)


def get_inspection(connection: sqlite3.Connection, inspection_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count
        FROM inspections i
        WHERE i.id = ?
        """,
        (inspection_id,),
    ).fetchone()


def list_inspections(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count
        FROM inspections i
        ORDER BY i.id
        """
    ).fetchall()


def update_inspection(
    connection: sqlite3.Connection,
    inspection_id: int,
    total_pieces: int,
    note: str | None,
) -> None:
    connection.execute(
        "UPDATE inspections SET total_pieces = ?, note = ? WHERE id = ?",
        (total_pieces, note, inspection_id),
    )


def load_constraints(
    connection: sqlite3.Connection, inspection_id: int
) -> list[tuple[int, int, int]]:
    rows = connection.execute(
        "SELECT lo, hi, count FROM segments WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()
    return [(int(row["lo"]), int(row["hi"]), int(row["count"])) for row in rows]


def insert_report(
    connection: sqlite3.Connection,
    inspection_id: int,
    segments: Sequence[tuple[int, int, int]],
) -> int:
    cursor = connection.execute(
        "INSERT INTO count_reports (inspection_id, created_at) VALUES (?, ?)",
        (inspection_id, now()),
    )
    report_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO segments (report_id, inspection_id, lo, hi, count)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(report_id, inspection_id, lo, hi, count) for lo, hi, count in segments],
    )
    return report_id


def get_reports(connection: sqlite3.Connection, inspection_id: int) -> list[dict]:
    reports = connection.execute(
        "SELECT id, inspection_id, created_at FROM count_reports "
        "WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()
    segment_rows = connection.execute(
        "SELECT id, report_id, lo, hi, count FROM segments "
        "WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()

    segments_by_report: dict[int, list[dict]] = {int(r["id"]): [] for r in reports}
    for row in segment_rows:
        segments_by_report[int(row["report_id"])].append(
            {
                "id": int(row["id"]),
                "lo": int(row["lo"]),
                "hi": int(row["hi"]),
                "count": int(row["count"]),
            }
        )

    return [
        {
            "id": int(report["id"]),
            "inspection_id": int(report["inspection_id"]),
            "created_at": report["created_at"],
            "segments": segments_by_report[int(report["id"])],
        }
        for report in reports
    ]


# ---------------------------------------------------------------------------
# 复卷纸追溯：卷与接段
# ---------------------------------------------------------------------------


def create_raw_roll(
    connection: sqlite3.Connection,
    length_mm: int,
    inspection_id: int,
    note: str | None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO rolls (kind, length_mm, inspection_id, note, created_at)
        VALUES ('raw', ?, ?, ?, ?)
        """,
        (length_mm, inspection_id, note, now()),
    )
    return int(cursor.lastrowid)


def create_finished_roll(connection: sqlite3.Connection, note: str | None) -> int:
    # 长度由接段决定：先放占位值 1（满足 length_mm > 0 的 CHECK），
    # 接段写入并校验后由调用方更新为实际长度。
    cursor = connection.execute(
        """
        INSERT INTO rolls (kind, length_mm, inspection_id, note, created_at)
        VALUES ('finished', 1, NULL, ?, ?)
        """,
        (note, now()),
    )
    return int(cursor.lastrowid)


def get_roll(connection: sqlite3.Connection, roll_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT r.id, r.kind, r.length_mm, r.inspection_id, r.note, r.created_at,
               (SELECT COUNT(*) FROM splices s WHERE s.roll_id = r.id) AS splice_count
        FROM rolls r
        WHERE r.id = ?
        """,
        (roll_id,),
    ).fetchone()


def list_rolls(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT r.id, r.kind, r.length_mm, r.inspection_id, r.note, r.created_at,
               (SELECT COUNT(*) FROM splices s WHERE s.roll_id = r.id) AS splice_count
        FROM rolls r
        ORDER BY r.id
        """
    ).fetchall()


def update_roll_length(
    connection: sqlite3.Connection, roll_id: int, length_mm: int
) -> None:
    connection.execute(
        "UPDATE rolls SET length_mm = ? WHERE id = ?",
        (length_mm, roll_id),
    )


def update_roll_note(
    connection: sqlite3.Connection, roll_id: int, note: str | None
) -> None:
    connection.execute(
        "UPDATE rolls SET note = ? WHERE id = ?",
        (note, roll_id),
    )


def next_splice_seq(connection: sqlite3.Connection, roll_id: int) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM splices WHERE roll_id = ?",
        (roll_id,),
    ).fetchone()
    return int(row["next_seq"])


def insert_splice(
    connection: sqlite3.Connection,
    roll_id: int,
    seq: int,
    source_roll_id: int,
    source_lo: int,
    source_hi: int,
    reversed_flag: bool,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO splices
            (roll_id, seq, source_roll_id, source_lo, source_hi, reversed)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (roll_id, seq, source_roll_id, source_lo, source_hi, int(reversed_flag)),
    )
    return int(cursor.lastrowid)


def get_splice(
    connection: sqlite3.Connection, splice_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM splices WHERE id = ?",
        (splice_id,),
    ).fetchone()


def update_splice(
    connection: sqlite3.Connection,
    splice_id: int,
    source_roll_id: int,
    source_lo: int,
    source_hi: int,
    reversed_flag: bool,
) -> None:
    connection.execute(
        """
        UPDATE splices
        SET source_roll_id = ?, source_lo = ?, source_hi = ?, reversed = ?
        WHERE id = ?
        """,
        (source_roll_id, source_lo, source_hi, int(reversed_flag), splice_id),
    )


def list_splices(
    connection: sqlite3.Connection, roll_id: int
) -> list[dict]:
    """按接入顺序返回成卷的接段，并算出每段在成卷上的 [start, end)。"""
    rows = connection.execute(
        """
        SELECT id, roll_id, seq, source_roll_id, source_lo, source_hi, reversed
        FROM splices
        WHERE roll_id = ?
        ORDER BY seq
        """,
        (roll_id,),
    ).fetchall()
    result: list[dict] = []
    cursor_pos = 0
    for row in rows:
        length = int(row["source_hi"]) - int(row["source_lo"])
        result.append(
            {
                "id": int(row["id"]),
                "roll_id": int(row["roll_id"]),
                "seq": int(row["seq"]),
                "start": cursor_pos,
                "end": cursor_pos + length,
                "source_roll_id": int(row["source_roll_id"]),
                "source_lo": int(row["source_lo"]),
                "source_hi": int(row["source_hi"]),
                "reversed": bool(row["reversed"]),
            }
        )
        cursor_pos += length
    return result


def load_rolls(connection: sqlite3.Connection) -> dict[int, dict]:
    """一次性载入全部卷，供事务内的领域校验与追溯使用。"""
    rolls: dict[int, dict] = {}
    for row in connection.execute("SELECT * FROM rolls ORDER BY id").fetchall():
        rolls[int(row["id"])] = {
            "id": int(row["id"]),
            "kind": row["kind"],
            "length_mm": int(row["length_mm"]),
            "inspection_id": row["inspection_id"],
            "note": row["note"],
            "created_at": row["created_at"],
        }
    return rolls


def load_splices(connection: sqlite3.Connection) -> dict[int, list[dict]]:
    """载入全部接段，按所属成卷 id 分组并保持接入顺序。"""
    grouped: dict[int, list[dict]] = {}
    rows = connection.execute(
        "SELECT * FROM splices ORDER BY roll_id, seq"
    ).fetchall()
    for row in rows:
        grouped.setdefault(int(row["roll_id"]), []).append(
            {
                "id": int(row["id"]),
                "roll_id": int(row["roll_id"]),
                "seq": int(row["seq"]),
                "source_roll_id": int(row["source_roll_id"]),
                "source_lo": int(row["source_lo"]),
                "source_hi": int(row["source_hi"]),
                "reversed": bool(row["reversed"]),
            }
        )
    return grouped
