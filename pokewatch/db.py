"""SQLite 저장소. 수집한 글과 파싱된 카드 매물을 담는다."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    club_id       INTEGER NOT NULL,
    article_id    INTEGER NOT NULL,
    cafe_name     TEXT,
    menu_id       INTEGER,
    menu_name     TEXT,
    subject       TEXT NOT NULL,
    writer        TEXT,
    written_at    INTEGER NOT NULL DEFAULT 0,
    read_count    INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    thumbnail     TEXT,
    url           TEXT,
    collected_at  INTEGER NOT NULL,
    PRIMARY KEY (club_id, article_id)
);

CREATE TABLE IF NOT EXISTS listings (
    club_id           INTEGER NOT NULL,
    article_id        INTEGER NOT NULL,
    card_key          TEXT NOT NULL,
    display_name      TEXT,
    card_name         TEXT,
    card_name_en      TEXT,
    dex               INTEGER,
    kind              TEXT,
    rarity            TEXT,
    language          TEXT,
    condition         TEXT,
    grade_company     TEXT,
    grade_score       TEXT,
    set_code          TEXT,
    card_no           TEXT,
    trade_type        TEXT,
    price             INTEGER,
    price_max         INTEGER,
    price_text        TEXT,
    quantity          INTEGER DEFAULT 1,
    is_bundle         INTEGER DEFAULT 0,
    is_per_unit       INTEGER DEFAULT 0,
    shipping_included INTEGER DEFAULT 0,
    negotiable        INTEGER DEFAULT 0,
    confidence        REAL DEFAULT 0,
    written_at        INTEGER NOT NULL DEFAULT 0,
    price_source      TEXT,
    PRIMARY KEY (club_id, article_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_card_key ON listings (card_key);
CREATE INDEX IF NOT EXISTS idx_listings_written  ON listings (written_at DESC);
CREATE INDEX IF NOT EXISTS idx_listings_price    ON listings (price);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

LISTING_COLUMNS = [
    "club_id", "article_id", "card_key", "display_name", "card_name", "card_name_en",
    "dex", "kind", "rarity", "language", "condition", "grade_company", "grade_score",
    "set_code", "card_no", "trade_type", "price", "price_max", "price_text", "quantity",
    "is_bundle", "is_per_unit", "shipping_included", "negotiable", "confidence", "written_at",
    "price_source",
]


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """예전 파일에도 새 컬럼을 붙인다 (시세 기록을 버리지 않기 위해)."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
    for column, ddl in (("price_source", "TEXT"),):
        if column not in have:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {column} {ddl}")
    conn.commit()


def upsert_article(conn: sqlite3.Connection, article, cafe_name: str = "") -> None:
    conn.execute(
        """
        INSERT INTO articles (club_id, article_id, cafe_name, menu_id, menu_name, subject,
                              writer, written_at, read_count, comment_count, thumbnail, url,
                              collected_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(club_id, article_id) DO UPDATE SET
            subject=excluded.subject,
            read_count=excluded.read_count,
            comment_count=excluded.comment_count,
            thumbnail=COALESCE(excluded.thumbnail, articles.thumbnail),
            collected_at=excluded.collected_at
        """,
        (
            article.club_id, article.article_id, cafe_name, article.menu_id, article.menu_name,
            article.subject, article.writer, article.written_at, article.read_count,
            article.comment_count, article.thumbnail, article.url, int(time.time()),
        ),
    )


def upsert_listing(conn: sqlite3.Connection, club_id: int, article_id: int, info, written_at: int) -> None:
    values = {
        "club_id": club_id,
        "article_id": article_id,
        "card_key": info.card_key,
        "display_name": info.display_name,
        "card_name": info.card_name,
        "card_name_en": info.card_name_en,
        "dex": info.dex,
        "kind": info.kind,
        "rarity": info.rarity,
        "language": info.language,
        "condition": info.condition,
        "grade_company": info.grade_company,
        "grade_score": info.grade_score,
        "set_code": info.set_code,
        "card_no": info.card_no,
        "trade_type": info.trade_type,
        "price": info.price,
        "price_max": info.price_max,
        "price_text": info.price_text,
        "quantity": info.quantity,
        "is_bundle": int(info.is_bundle),
        "is_per_unit": int(info.is_per_unit),
        "shipping_included": int(info.shipping_included),
        "negotiable": int(info.negotiable),
        "confidence": info.confidence,
        "written_at": written_at,
        "price_source": getattr(info, "price_source", None),
    }
    placeholders = ",".join("?" for _ in LISTING_COLUMNS)
    updates = ",".join(f"{c}=excluded.{c}" for c in LISTING_COLUMNS if c not in ("club_id", "article_id"))
    conn.execute(
        f"INSERT INTO listings ({','.join(LISTING_COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(club_id, article_id) DO UPDATE SET {updates}",
        [values[c] for c in LISTING_COLUMNS],
    )


def fetch_listings(conn: sqlite3.Connection, **filters: Any) -> list[dict]:
    """필터를 적용해 매물 목록을 가져온다 (기사 정보 조인 포함)."""
    where: list[str] = []
    params: list[Any] = []

    def add(clause: str, *values: Any) -> None:
        where.append(clause)
        params.extend(values)

    if filters.get("q"):
        like = f"%{filters['q']}%"
        add("(l.card_name LIKE ? OR l.card_name_en LIKE ? OR a.subject LIKE ? OR l.card_key LIKE ?)",
            like, like, like, like)
    if filters.get("trade_type"):
        add("l.trade_type = ?", filters["trade_type"])
    if filters.get("language"):
        add("l.language = ?", filters["language"])
    if filters.get("rarity"):
        add("l.rarity = ?", filters["rarity"])
    if filters.get("condition"):
        add("l.condition = ?", filters["condition"])
    if filters.get("graded_only"):
        add("l.grade_company IS NOT NULL")
    if filters.get("grade"):
        add("(l.grade_company || l.grade_score) = ?", filters["grade"])
    if filters.get("card_key"):
        add("l.card_key = ?", filters["card_key"])
    if filters.get("card_key_prefix"):
        add("l.card_key LIKE ? ESCAPE '\\'", _like_prefix(filters["card_key_prefix"]))
    if filters.get("min_price") is not None:
        add("l.price >= ?", filters["min_price"])
    if filters.get("max_price") is not None:
        add("l.price <= ?", filters["max_price"])
    if filters.get("priced_only"):
        add("l.price IS NOT NULL")
    if filters.get("since"):
        add("l.written_at >= ?", filters["since"])
    if filters.get("named_only"):
        add("l.card_name IS NOT NULL")
    if filters.get("min_confidence") is not None:
        add("l.confidence >= ?", filters["min_confidence"])
    if filters.get("exclude_bundle"):
        add("l.is_bundle = 0")

    sql = (
        "SELECT l.*, a.subject, a.writer, a.url, a.thumbnail, a.menu_name, a.cafe_name, "
        "a.read_count, a.comment_count "
        "FROM listings l JOIN articles a "
        "ON a.club_id = l.club_id AND a.article_id = l.article_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY l.written_at DESC"
    if filters.get("limit"):
        sql += " LIMIT ?"
        params.append(int(filters["limit"]))

    return [dict(r) for r in conn.execute(sql, params)]


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def facet_counts(conn: sqlite3.Connection, column: str) -> list[dict]:
    if column not in {"rarity", "language", "trade_type", "condition", "set_code"}:
        raise ValueError(f"허용되지 않은 컬럼: {column}")
    rows = conn.execute(
        f"SELECT {column} AS value, COUNT(*) AS n FROM listings "
        f"WHERE {column} IS NOT NULL GROUP BY {column} ORDER BY n DESC"
    )
    return [{"value": r["value"], "count": r["n"]} for r in rows]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def totals(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS articles, "
        "       SUM(CASE WHEN price IS NOT NULL THEN 1 ELSE 0 END) AS priced, "
        "       SUM(CASE WHEN card_name IS NOT NULL THEN 1 ELSE 0 END) AS identified, "
        "       COUNT(DISTINCT card_key) AS cards, "
        "       MAX(written_at) AS newest "
        "FROM listings"
    ).fetchone()
    total = row["articles"] or 0
    return {
        "articles": total,
        "priced": row["priced"] or 0,
        "identified": row["identified"] or 0,
        "cards": row["cards"] or 0,
        "newest": row["newest"] or 0,
        # 파서가 전체 글 중 몇 %에서 카드를 찾아냈는지 (필터와 무관한 전체 기준)
        "identified_rate": round((row["identified"] or 0) / total * 100) if total else 0,
    }


def purge_older_than(conn: sqlite3.Connection, cutoff_ts: int) -> int:
    cur = conn.execute("DELETE FROM listings WHERE written_at < ?", (cutoff_ts,))
    conn.execute("DELETE FROM articles WHERE written_at < ?", (cutoff_ts,))
    conn.commit()
    return cur.rowcount
