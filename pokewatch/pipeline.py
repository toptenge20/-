"""수집 파이프라인: 카페에서 글을 가져와 파싱하고 DB에 넣는다."""

from __future__ import annotations

import logging
import sqlite3
import time

from . import db
from .config import CafeTarget, Config
from .naver import NaverCafeClient, NaverCafeError
from .parsing import parse_title

log = logging.getLogger(__name__)


def collect(conn: sqlite3.Connection, cfg: Config, progress=None) -> dict:
    """설정된 모든 카페/게시판을 수집한다. 결과 요약 dict 반환."""
    client = NaverCafeClient(cookie=cfg.cookie, delay=cfg.delay, timeout=cfg.timeout)
    report = {"new": 0, "updated": 0, "errors": [], "cafes": []}

    if not cfg.cafes:
        report["errors"].append("설정에 카페가 없습니다. config.json 의 cafes 를 채우세요.")
        return report

    for target in cfg.cafes:
        try:
            result = _collect_cafe(conn, client, target, progress)
            report["cafes"].append(result)
            report["new"] += result["new"]
            report["updated"] += result["updated"]
        except NaverCafeError as e:
            log.error("%s 수집 실패: %s", target.name, e)
            report["errors"].append(f"{target.name}: {e}")

    db.set_meta(conn, "last_collect_at", int(time.time()))
    conn.commit()
    return report


def _collect_cafe(conn, client: NaverCafeClient, target: CafeTarget, progress=None) -> dict:
    club_id = target.club_id
    if not club_id:
        if not target.cafe_url:
            raise NaverCafeError(f"{target.name}: club_id 또는 cafe_url 이 필요합니다")
        club_id = client.resolve_club_id(target.cafe_url)
        log.info("%s → clubId %s", target.cafe_url, club_id)

    menu_ids = list(target.menu_ids)
    if not menu_ids and target.menu_name_filter:
        menus = client.list_menus(club_id)
        menu_ids = [
            m["menu_id"] for m in menus
            if any(word in m["name"] for word in target.menu_name_filter)
        ]
        log.info("%s: 이름으로 고른 게시판 %s개", target.name, len(menu_ids))

    result = {"name": target.name, "club_id": club_id, "new": 0, "updated": 0, "seen": 0}

    for menu_id in menu_ids or [None]:
        for article in client.iter_articles(club_id, menu_id, target.pages, target.per_page):
            existed = conn.execute(
                "SELECT 1 FROM articles WHERE club_id=? AND article_id=?",
                (club_id, article.article_id),
            ).fetchone()

            db.upsert_article(conn, article, cafe_name=target.name)
            info = parse_title(article.subject)
            db.upsert_listing(conn, club_id, article.article_id, info, article.written_at)

            result["seen"] += 1
            if existed:
                result["updated"] += 1
            else:
                result["new"] += 1
            if progress:
                progress(article, info)

        conn.commit()

    return result


def reparse(conn: sqlite3.Connection) -> int:
    """파서를 고친 뒤, 이미 저장된 글 제목을 다시 해석한다 (네트워크 불필요)."""
    rows = conn.execute("SELECT club_id, article_id, subject, written_at FROM articles").fetchall()
    for row in rows:
        info = parse_title(row["subject"])
        db.upsert_listing(conn, row["club_id"], row["article_id"], info, row["written_at"] or 0)
    conn.commit()
    return len(rows)
