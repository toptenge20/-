"""수집 결과를 정적 스냅샷으로 내보낸다.

핸드폰만으로 쓰려면 파이썬 서버가 없는 곳(깃허브 페이지 등)에도 올릴 수 있어야
한다. 그래서 대시보드가 필요한 데이터를 JSON 한 덩어리로 만들고, 화면 파일과 함께
`site/` 폴더에 담는다. 이 폴더를 그대로 정적 호스팅에 올리면 앱이 된다.

서버 모드의 /api/snapshot 과 정적 모드의 data/snapshot.json 은 **같은 함수**로
만들어진 같은 모양의 데이터다. 그래서 화면 코드가 두 모드에서 똑같이 동작한다.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import db
from .parsing import CONDITION_LABELS, LANGUAGE_LABELS, RARITY_LABELS, TRADE_LABELS

WEB_DIR = Path(__file__).resolve().parent / "web"

# 화면에 필요한 항목만 골라 담는다 (스냅샷 크기를 줄이기 위해).
LISTING_FIELDS = [
    "article_id", "card_key", "display_name", "card_name", "card_name_en", "dex", "kind",
    "rarity", "language", "condition", "grade_company", "grade_score", "set_code", "card_no",
    "trade_type", "price", "price_max", "quantity", "is_bundle", "is_per_unit",
    "shipping_included", "negotiable", "confidence", "written_at",
    "subject", "writer", "url", "thumbnail", "menu_name", "cafe_name",
]


def build_snapshot(conn, cafes: list[str] | None = None, days: int | None = None,
                   limit: int | None = None) -> dict:
    """대시보드가 쓰는 데이터 전부를 하나의 dict 로 만든다."""
    filters: dict = {}
    if days:
        filters["since"] = int(time.time()) - days * 86_400
    if limit:
        filters["limit"] = limit

    rows = db.fetch_listings(conn, **filters)
    listings = [{k: r.get(k) for k in LISTING_FIELDS} for r in rows]

    return {
        "version": 1,
        "generated_at": int(time.time()),
        "last_collect_at": int(db.get_meta(conn, "last_collect_at", "0") or 0),
        "is_demo": db.get_meta(conn, "demo") == "1",
        "cafes": cafes or [],
        "totals": db.totals(conn),
        # 한글 표기는 파이썬 쪽 정의를 그대로 내려보내 화면과 어긋나지 않게 한다.
        "labels": {
            "rarity": RARITY_LABELS,
            "language": LANGUAGE_LABELS,
            "trade": TRADE_LABELS,
            "condition": CONDITION_LABELS,
        },
        "listings": listings,
    }


def export_site(conn, out_dir: Path | str, cafes: list[str] | None = None,
                days: int | None = None, limit: int | None = None) -> dict:
    """정적 호스팅에 그대로 올릴 수 있는 폴더를 만든다."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    snapshot = build_snapshot(conn, cafes=cafes, days=days, limit=limit)
    (out / "data").mkdir(exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    (out / "data" / "snapshot.json").write_text(payload, encoding="utf-8")

    # 화면 파일 복사
    for name in ("styles.css", "app.js", "data.js"):
        shutil.copy2(WEB_DIR / name, out / name)
    shutil.copytree(WEB_DIR / "icons", out / "icons", dirs_exist_ok=True)

    _write_index(out)
    _write_manifest(out)
    _write_sw(out, snapshot["generated_at"])

    # 깃허브 페이지가 _ 로 시작하는 파일을 지우지 않도록
    (out / ".nojekyll").write_text("", encoding="utf-8")

    return {
        "dir": str(out),
        "listings": len(snapshot["listings"]),
        "bytes": len(payload.encode("utf-8")),
    }


def _write_index(out: Path) -> None:
    """정적 모드용 index.html.

    하위 경로(예: https://아이디.github.io/저장소이름/)에 올려도 동작하도록
    절대 경로(/static/...)를 상대 경로로 바꾼다.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = (
        html.replace('href="/static/styles.css"', 'href="styles.css"')
        .replace('src="/static/data.js"', 'src="data.js"')
        .replace('src="/static/app.js"', 'src="app.js"')
        .replace('href="/manifest.webmanifest"', 'href="manifest.webmanifest"')
        .replace('href="/icons/', 'href="icons/')
        .replace("<!--MODE-->", '<script>window.POKEWATCH_MODE="static";</script>')
    )
    (out / "index.html").write_text(html, encoding="utf-8")


def _write_manifest(out: Path) -> None:
    manifest = json.loads((WEB_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    manifest["start_url"] = "."
    manifest["scope"] = "."
    for icon in manifest.get("icons", []):
        icon["src"] = icon["src"].lstrip("/")
    for sc in manifest.get("shortcuts", []):
        sc["url"] = "." + sc["url"].lstrip("/") if sc["url"].startswith("/?") else sc["url"]
        for icon in sc.get("icons", []):
            icon["src"] = icon["src"].lstrip("/")
    (out / "manifest.webmanifest").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_sw(out: Path, version: int) -> None:
    """정적 모드 서비스 워커.

    수집 시각을 캐시 이름에 넣어, 새 스냅샷이 올라오면 옛 데이터가 자동으로 밀린다.
    """
    sw = (WEB_DIR / "sw.js").read_text(encoding="utf-8")
    sw = sw.replace("const VERSION = 'pokewatch-v1';", f"const VERSION = 'pokewatch-{version}';")
    # 정적 모드에는 /api/ 가 없다. 대신 스냅샷을 network-first 로 받아 최신을 유지한다.
    sw = sw.replace("url.pathname.startsWith('/api/')", "url.pathname.endsWith('/data/snapshot.json')")
    sw = sw.replace(
        "const SHELL_FILES = [\n  '/',\n  '/static/styles.css',\n  '/static/app.js',\n"
        "  '/manifest.webmanifest',\n  '/icons/icon-192.png',\n  '/icons/icon-512.png',\n];",
        "const SHELL_FILES = [\n  './',\n  './styles.css',\n  './data.js',\n  './app.js',\n"
        "  './manifest.webmanifest',\n  './icons/icon-192.png',\n  './icons/icon-512.png',\n];",
    )
    sw = sw.replace("caches.match('/')", "caches.match('./')")
    (out / "sw.js").write_text(sw, encoding="utf-8")
