"""명령줄 인터페이스."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import db, stats
from .config import Config
from .parsing import CONDITION_LABELS, LANGUAGE_LABELS, format_won, parse_title

CONFIG_TEMPLATE = {
    "db_path": "data/pokewatch.db",
    "delay": 0.8,
    "host": "127.0.0.1",
    "port": 8765,
    "min_confidence": 0.0,
    "auto_collect_minutes": 0,
    "_cookie_note": "회원 전용 게시판은 POKEWATCH_COOKIE 환경변수로 쿠키를 넘기세요.",
    "cafes": [
        {
            "name": "예시 카페",
            "cafe_url": "카페주소",
            "menu_name_filter": ["장터", "거래", "판매"],
            "pages": 3,
            "per_page": 50,
        }
    ],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pokewatch",
        description="네이버 카페 포켓몬 카드 시세를 모아서 한눈에 보여줍니다.",
    )
    parser.add_argument("-c", "--config", help="설정 파일 경로 (기본: config.json)")
    parser.add_argument("-v", "--verbose", action="store_true", help="자세한 로그")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="config.json 템플릿을 만듭니다")

    p_menus = sub.add_parser("menus", help="카페의 게시판(메뉴) 목록과 ID를 봅니다")
    p_menus.add_argument("cafe", help="카페 주소 또는 clubId")

    p_collect = sub.add_parser("collect", help="설정된 카페에서 글을 수집합니다")
    p_collect.add_argument("--pages", type=int, help="카페별 수집 페이지 수 덮어쓰기")

    p_serve = sub.add_parser("serve", help="대시보드를 띄웁니다")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--host")
    p_serve.add_argument("--lan", action="store_true",
                         help="같은 와이파이의 휴대폰에서도 접속할 수 있게 엽니다")
    p_serve.add_argument("--open", action="store_true", help="브라우저를 자동으로 엽니다")
    p_serve.add_argument("--auto-collect", type=int, metavar="분",
                         help="N분마다 자동으로 새 글을 수집합니다")

    p_demo = sub.add_parser("demo", help="예제 데이터를 넣고 대시보드를 띄웁니다")
    p_demo.add_argument("--count", type=int, default=420)
    p_demo.add_argument("--no-serve", action="store_true")
    p_demo.add_argument("--lan", action="store_true")
    p_demo.add_argument("--open", action="store_true")

    sub.add_parser("reparse", help="저장된 제목을 파서로 다시 해석합니다")

    p_parse = sub.add_parser("parse", help="제목 하나를 파싱해 결과를 봅니다")
    p_parse.add_argument("title", nargs="+")

    p_top = sub.add_parser("top", help="터미널에서 시세 상위 카드를 봅니다")
    p_top.add_argument("-n", type=int, default=20)
    p_top.add_argument("--sort", default="listings", choices=["listings", "price", "recent", "trend"])
    p_top.add_argument("--days", type=int, help="최근 N일만")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.command == "parse":
        return cmd_parse(" ".join(args.title))
    if args.command == "init":
        return cmd_init(args.config)

    cfg = Config.load(args.config)
    if args.command == "menus":
        return cmd_menus(cfg, args.cafe)

    conn = db.connect(cfg.db_path)
    try:
        if args.command == "collect":
            return cmd_collect(conn, cfg, args.pages)
        if args.command == "serve":
            if args.port:
                cfg.port = args.port
            if args.host:
                cfg.host = args.host
            if args.lan:
                cfg.host = "0.0.0.0"
            if args.auto_collect:
                cfg.auto_collect_minutes = args.auto_collect
            return cmd_serve(conn, cfg, args.open)
        if args.command == "demo":
            if args.lan:
                cfg.host = "0.0.0.0"
            return cmd_demo(conn, cfg, args.count, args.no_serve, args.open)
        if args.command == "reparse":
            n = _reparse(conn)
            print(f"{n:,}건 다시 해석했습니다.")
            return 0
        if args.command == "top":
            return cmd_top(conn, args.n, args.sort, args.days)
    finally:
        conn.close()
    return 0


# ── 각 명령 ─────────────────────────────────────────────────────────────────
def cmd_init(path: str | None) -> int:
    target = Path(path or "config.json")
    if target.exists():
        print(f"{target} 이(가) 이미 있습니다. 덮어쓰지 않았습니다.")
        return 1
    target.write_text(json.dumps(CONFIG_TEMPLATE, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{target} 을(를) 만들었습니다.")
    print("cafe_url 과 게시판을 채운 뒤 `python -m pokewatch collect` 을 실행하세요.")
    print("게시판 ID가 궁금하면 `python -m pokewatch menus <카페주소>` 를 써보세요.")
    return 0


def cmd_menus(cfg: Config, cafe: str) -> int:
    from .naver import NaverCafeClient, NaverCafeError

    client = NaverCafeClient(cookie=cfg.cookie, delay=cfg.delay, timeout=cfg.timeout)
    try:
        club_id = client.resolve_club_id(cafe)
        menus = client.list_menus(club_id)
    except NaverCafeError as e:
        print(f"실패: {e}", file=sys.stderr)
        return 1

    print(f"\n  clubId: {club_id}\n")
    print(f"  {'menu_id':>8}  게시판 이름")
    print("  " + "-" * 46)
    for m in menus:
        print(f"  {m['menu_id']:>8}  {m['name']}")
    print()
    return 0


def cmd_collect(conn, cfg: Config, pages: int | None) -> int:
    from .pipeline import collect

    if pages:
        for c in cfg.cafes:
            c.pages = pages

    seen = {"n": 0}

    def progress(article, info):
        seen["n"] += 1
        if seen["n"] % 25 == 0:
            print(f"  … {seen['n']}건 처리", end="\r", flush=True)

    report = collect(conn, cfg, progress=progress)
    print(" " * 40, end="\r")

    for cafe in report["cafes"]:
        print(f"  {cafe['name']}: 새 글 {cafe['new']:,} / 갱신 {cafe['updated']:,}")
    for err in report["errors"]:
        print(f"  ! {err}", file=sys.stderr)

    t = db.totals(conn)
    print(f"\n  누적: 매물 {t['articles']:,}건, 카드 {t['cards']:,}종, 가격 있는 글 {t['priced']:,}건")
    return 1 if report["errors"] and not report["cafes"] else 0


def cmd_serve(conn, cfg: Config, open_browser: bool = False) -> int:
    from .server import serve

    if db.totals(conn)["articles"] == 0:
        print("  (아직 수집된 글이 없습니다. `collect` 또는 `demo` 를 먼저 실행하세요.)")
    serve(conn, cfg, open_browser=open_browser)
    return 0


def cmd_demo(conn, cfg: Config, count: int, no_serve: bool, open_browser: bool = False) -> int:
    from .demo import generate

    n = generate(conn, count=count)
    print(f"  예제 글 {n:,}건을 만들었습니다.")
    if no_serve:
        return 0
    from .server import serve

    serve(conn, cfg, open_browser=open_browser)
    return 0


def cmd_parse(title: str) -> int:
    info = parse_title(title)
    print(f"\n  제목: {info.title}\n")
    fields = [
        ("거래 유형", info.trade_label),
        ("카드 이름", info.card_name or "(못 찾음)"),
        ("영문명", info.card_name_en or "-"),
        ("도감 번호", info.dex or "-"),
        ("레어도", info.rarity_label or info.rarity or "-"),
        ("언어", LANGUAGE_LABELS.get(info.language, info.language)),
        ("상태", CONDITION_LABELS.get(info.condition, info.condition)),
        ("감정", f"{info.grade_company} {info.grade_score}" if info.grade_company else "-"),
        ("세트", info.set_code or "-"),
        ("카드 번호", info.card_no or "-"),
        ("가격", format_won(info.price) + (f"  (원문: {info.price_text})" if info.price_text else "")),
        ("최고가", format_won(info.price_max) if info.price_max else "-"),
        ("수량", info.quantity),
        ("일괄", "예" if info.is_bundle else "아니오"),
        ("택포", "예" if info.shipping_included else "아니오"),
        ("card_key", info.card_key),
        ("신뢰도", info.confidence),
    ]
    for label, value in fields:
        print(f"  {label:<10} {value}")
    print()
    return 0


def cmd_top(conn, n: int, sort: str, days: int | None) -> int:
    filters = {"priced_only": True, "named_only": True}
    if days:
        filters["since"] = int(time.time()) - days * 86_400

    rows = db.fetch_listings(conn, **filters)
    if not rows:
        print("  데이터가 없습니다. `collect` 또는 `demo` 를 먼저 실행하세요.")
        return 1

    cards = stats.aggregate_cards(rows)
    from .server import _sort_key

    cards.sort(key=_sort_key(sort), reverse=True)

    print(f"\n  카드{' ' * 28}{'중앙값':>10}{'최저':>10}{'최고':>10}{'매물':>6}  추이")
    print("  " + "─" * 82)
    for c in cards[:n]:
        trend = c["trend"]
        arrow = {"up": "▲", "down": "▼", "flat": "─"}[trend["direction"]]
        pct = f"{trend['percent']:+.1f}%" if trend["percent"] is not None else "  -"
        lang = c["language_label"] if c["language"] != "UNK" else ""
        name = f"{c['display_name']}{f' ({lang})' if lang else ''}"
        name = _fit(name, 29)
        pad = 30 - _width(name)
        print(
            f"  {name}{' ' * max(1, pad)}"
            f"{format_won(c['median_price']):>12}"
            f"{format_won(c['min_price']):>12}"
            f"{format_won(c['max_price']):>12}"
            f"{c['listing_count']:>6}  {arrow} {pct}"
        )
    print()
    return 0


def _width(s: str) -> int:
    """한글·한자는 터미널에서 두 칸을 차지한다."""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _fit(s: str, width: int) -> str:
    if _width(s) <= width:
        return s
    out = ""
    for ch in s:
        if _width(out + ch) > width - 1:
            return out + "…"
        out += ch
    return out


def _reparse(conn) -> int:
    from .pipeline import reparse

    return reparse(conn)
