"""수집 파이프라인: 카페에서 글을 가져와 파싱하고 DB에 넣는다."""

from __future__ import annotations

import logging
import re
import sqlite3
import time

from . import db
from .config import CafeTarget, Config
from .naver import NaverAccessDenied, NaverCafeClient, NaverCafeError
from .parsing import parse_title

log = logging.getLogger(__name__)

# 본문이 실제로 어떻게 오는지 몇 건만 로그로 남기기 위한 카운터
_body_samples_logged = 0


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
        log.info("%s: 게시판 %s개 — %s", target.name, len(menus),
                 ", ".join(m["name"] for m in menus if m["name"]) or "(이름 없음)")

        chosen = []
        for m in menus:
            name = m["name"]
            if not any(word in name for word in target.menu_name_filter):
                continue
            skip = next((w for w in target.menu_name_exclude if w in name), None)
            if skip:
                log.info("  제외: %s ('%s' 때문)", name, skip)
                continue
            chosen.append(m)

        menu_ids = [m["menu_id"] for m in chosen]
        log.info("%s: 수집할 게시판 %s개 — %s", target.name, len(menu_ids),
                 ", ".join(m["name"] for m in chosen) or "(없음)")

    result = {"name": target.name, "club_id": club_id, "new": 0, "updated": 0, "seen": 0,
              "bodies": 0, "body_prices": 0, "denied": False}
    body_budget = target.body_limit if target.fetch_bodies else 0

    for menu_id in menu_ids or [None]:
        for article in client.iter_articles(club_id, menu_id, target.pages, target.per_page):
            existed = conn.execute(
                "SELECT price_source FROM listings WHERE club_id=? AND article_id=?",
                (club_id, article.article_id),
            ).fetchone()

            db.upsert_article(conn, article, cafe_name=target.name)
            info = parse_title(article.subject)
            info.price_source = "title" if info.price is not None else None

            # 이 카페들은 제목에 가격을 안 쓰고 본문에 적는다. 제목에서 못 찾았고
            # 아직 본문을 본 적 없는 글이면 본문을 읽어 본다.
            if (info.price is None and body_budget > 0
                    and (existed is None or existed["price_source"] != "body")
                    and _worth_reading(info)):
                body_budget -= 1
                result["bodies"] += 1
                outcome = _price_from_body(client, club_id, article.article_id, info)
                if outcome == "found":
                    result["body_prices"] += 1
                elif outcome == "denied":
                    # 회원 전용이라 막힌 것이다. 이 카페의 다른 글도 마찬가지이므로
                    # 남은 예산을 여기에 더 쓰지 않는다. '확인 완료' 로도 표시하지
                    # 않는다 — 나중에 로그인 쿠키를 넣으면 다시 읽어야 한다.
                    if not result["denied"]:
                        result["denied"] = True
                        log.info("%s: 본문이 회원 전용입니다. 이번 실행에서는 본문 읽기를 멈춥니다 "
                                 "(로그인 쿠키가 있어야 가격을 볼 수 있습니다)", target.name)
                    body_budget = 0
                    result["bodies"] -= 1
                else:
                    # 다음 실행 때 같은 글을 또 읽지 않도록 표시해 둔다
                    info.price_source = "body-none"

            db.upsert_listing(conn, club_id, article.article_id, info, article.written_at)

            result["seen"] += 1
            if existed:
                result["updated"] += 1
            else:
                result["new"] += 1
            if progress:
                progress(article, info)

        conn.commit()

    if result["bodies"]:
        log.info("%s: 본문 %s건을 읽어 가격 %s건을 찾았습니다",
                 target.name, result["bodies"], result["body_prices"])
    return result


def _worth_reading(info) -> bool:
    """본문까지 읽어 볼 만한 글인지. 요청을 아끼려고 카드 거래글만 읽는다.

    거래 게시판 글이라도 제목에 '판매' 같은 말이 없는 경우가 흔해서
    (예: '일판 뮤,이상해씨'), 거래 유형은 조건으로 쓰지 않는다.
    """
    if info.trade_type in ("free", "info"):
        return False
    return bool(info.card_name or info.rarity)


def _price_from_body(client: NaverCafeClient, club_id: int, article_id: int, info) -> str:
    """본문에서 가격을 찾아 info 에 채운다.

    'found'(찾음) / 'none'(읽었지만 가격 없음) / 'denied'(회원 전용이라 못 읽음)
    를 돌려준다. 'denied' 를 'none' 과 섞으면 안 된다 — 막힌 글을 확인 완료로
    기록해 버리면 나중에 쿠키를 넣어도 그 글을 다시 안 읽는다.
    """
    from .parsing import parse_price

    try:
        body = client.get_article_body(club_id, article_id)
    except NaverAccessDenied as e:
        log.debug("본문이 회원 전용입니다 (%s): %s", article_id, e)
        return "denied"
    except NaverCafeError as e:
        log.debug("본문을 읽지 못했습니다 (%s): %s", article_id, e)
        return "none"

    text = (body.get("text") or "").strip()

    # 본문을 읽었는데 가격이 하나도 안 나오면, 실제로 무엇이 왔는지 봐야 한다.
    # (회원 전용이라 빈 본문이 오는 경우와 구분이 안 되기 때문)
    if not text:
        return "none"

    price = parse_price(text[:2000])   # 앞부분에 가격을 적는 글이 대부분이다

    # 본문은 열리는데 가격이 안 나오는 경우를 봐야 한다. 값을 못 찾은 것 중
    # 앞의 몇 건만 실제 내용을 남긴다. (빈 본문은 볼 것이 없으므로 제외)
    global _body_samples_logged
    if price.price is None and _body_samples_logged < 3:
        _body_samples_logged += 1
        log.info("가격 못 찾은 본문 %s (글 %s, %d자): %s",
                 _body_samples_logged, article_id, len(text),
                 re.sub(r"\s+", " ", text[:400]))

    if price.price is None:
        return "none"

    info.price = price.price
    info.price_max = price.price_max
    info.price_text = price.price_text
    info.is_bundle = info.is_bundle or price.is_bundle
    info.shipping_included = info.shipping_included or price.shipping_included
    info.negotiable = info.negotiable or price.negotiable
    info.price_source = "body"
    # 본문에서 온 가격은 제목보다 덜 확실하다 (여러 카드가 섞여 있을 수 있다)
    info.confidence = round(min(1.0, info.confidence + 0.2), 2)
    return "found"


def reparse(conn: sqlite3.Connection) -> int:
    """파서를 고친 뒤, 이미 저장된 글 제목을 다시 해석한다 (네트워크 불필요)."""
    rows = conn.execute("SELECT club_id, article_id, subject, written_at FROM articles").fetchall()
    for row in rows:
        info = parse_title(row["subject"])
        db.upsert_listing(conn, row["club_id"], row["article_id"], info, row["written_at"] or 0)
    conn.commit()
    return len(rows)
