"""대시보드 웹 서버. 표준 라이브러리만 사용한다."""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db, stats
from .config import Config
from .parsing import CONDITION_LABELS, LANGUAGE_LABELS, RARITY_LABELS, TRADE_LABELS, format_won

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent / "web"

# mimetypes 가 모르는 확장자
EXTRA_TYPES = {".webmanifest": "application/manifest+json", ".js": "application/javascript"}

# 레어도별 카드 이미지 배경색 (썸네일이 없을 때 그려주는 대체 이미지에 쓰인다)
RARITY_COLORS = {
    "SAR": ("#7c3aed", "#c4b5fd"),
    "SR": ("#b45309", "#fcd34d"),
    "AR": ("#0e7490", "#67e8f9"),
    "UR": ("#a16207", "#fde68a"),
    "HR": ("#be123c", "#fda4af"),
    "CHR": ("#0f766e", "#5eead4"),
    "CSR": ("#1d4ed8", "#93c5fd"),
    "RR": ("#4338ca", "#a5b4fc"),
    "SA": ("#9d174d", "#f9a8d4"),
    "PROMO": ("#374151", "#d1d5db"),
    "EX": ("#c2410c", "#fdba74"),
    "GX": ("#1e40af", "#bfdbfe"),
    "V": ("#155e75", "#a5f3fc"),
    "VMAX": ("#86198f", "#f0abfc"),
    "VSTAR": ("#3730a3", "#c7d2fe"),
    "BOX": ("#166534", "#86efac"),
}
DEFAULT_COLORS = ("#334155", "#cbd5e1")


class Dashboard:
    """HTTP 핸들러가 참조하는 상태 묶음."""

    def __init__(self, conn, cfg: Config):
        self.conn = conn
        self.cfg = cfg
        self.collecting = False
        self.last_report: dict | None = None
        self.lock = threading.Lock()


def make_handler(app: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PokeWatch"

        def log_message(self, fmt, *args):  # 기본 stderr 로그를 죽인다
            log.debug("%s - %s", self.address_string(), fmt % args)

        # ── 라우팅 ──────────────────────────────────────────────────────────
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            try:
                if path == "/" or path == "/index.html":
                    # 첫 화면은 캐시하지 않는다. 오프라인 대비는 서비스 워커가 맡는다.
                    return self._send_file(WEB_DIR / "index.html", cache=False)
                if path.startswith("/static/"):
                    return self._send_static(path[len("/static/"):])
                if path.startswith("/icons/"):
                    return self._send_static("icons/" + path[len("/icons/"):])
                if path == "/manifest.webmanifest":
                    return self._send_file(WEB_DIR / "manifest.webmanifest")
                if path == "/sw.js":
                    # 서비스 워커는 제어할 범위의 최상위 경로에서 내려줘야 한다.
                    return self._send_file(WEB_DIR / "sw.js", cache=False)
                if path == "/api/overview":
                    return self._api_overview(query)
                if path == "/api/cards":
                    return self._api_cards(query)
                if path == "/api/card":
                    return self._api_card(query)
                if path == "/api/listings":
                    return self._api_listings(query)
                if path == "/api/facets":
                    return self._api_facets()
                if path.startswith("/img/"):
                    return self._placeholder(urllib.parse.unquote(path[len("/img/"):]))
                self._send_json({"error": "not found"}, status=404)
            except Exception as e:  # 대시보드가 통째로 죽는 것보다 낫다
                log.exception("요청 처리 중 오류: %s", self.path)
                self._send_json({"error": str(e)}, status=500)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/collect":
                return self._api_collect()
            self._send_json({"error": "not found"}, status=404)

        # ── API ─────────────────────────────────────────────────────────────
        def _filters(self, query: dict) -> dict:
            def one(key, cast=str, default=None):
                vals = query.get(key)
                if not vals or vals[0] == "":
                    return default
                try:
                    return cast(vals[0])
                except (TypeError, ValueError):
                    return default

            return {
                "q": one("q"),
                "trade_type": one("trade"),
                "language": one("lang"),
                "rarity": one("rarity"),
                "condition": one("condition"),
                "grade": one("grade"),
                "graded_only": one("graded") == "1",
                "min_price": one("min", int),
                "max_price": one("max", int),
                "since": _since_ts(one("days", int)),
                "priced_only": one("priced", str, "0") == "1",
                "named_only": one("named", str, "0") == "1",
                "exclude_bundle": one("nobundle", str, "0") == "1",
                "min_confidence": one("conf", float, app.cfg.min_confidence),
                "card_key": one("card_key"),
            }

        def _api_overview(self, query):
            rows = db.fetch_listings(app.conn, **self._filters(query))
            cards = stats.aggregate_cards(rows)
            totals = db.totals(app.conn)
            payload = stats.overview(rows, cards)
            # 인식률은 필터('카드 인식된 것만')에 따라 100%로 고정되지 않도록 전체 기준으로 낸다.
            payload["identified_rate"] = totals["identified_rate"]
            payload.update(
                {
                    "totals": totals,
                    "last_collect_at": int(db.get_meta(app.conn, "last_collect_at", "0") or 0),
                    "is_demo": db.get_meta(app.conn, "demo") == "1",
                    "collecting": app.collecting,
                    "last_report": app.last_report,
                    "cafes": [c.name for c in app.cfg.cafes],
                }
            )
            self._send_json(payload)

        def _api_cards(self, query):
            filters = self._filters(query)
            rows = db.fetch_listings(app.conn, **filters)
            cards = stats.aggregate_cards(rows)

            sort = (query.get("sort") or ["listings"])[0]
            cards.sort(key=_sort_key(sort), reverse=sort != "name")

            limit = int((query.get("limit") or ["300"])[0])
            self._send_json({"count": len(cards), "cards": cards[:limit]})

        def _api_card(self, query):
            key = (query.get("key") or [""])[0]
            if not key:
                return self._send_json({"error": "key 파라미터가 필요합니다"}, status=400)

            # 언어 표기가 없는 글도 같은 그룹에 접히므로, 언어를 뗀 앞부분으로 넓게 가져온 뒤
            # 목록 화면과 똑같은 규칙으로 다시 묶어서 요청된 그룹을 고른다.
            base = key.rsplit("|", 1)[0]
            rows = db.fetch_listings(app.conn, card_key_prefix=base + "|")
            if not rows:
                return self._send_json({"error": "해당 카드를 찾지 못했습니다"}, status=404)

            card, listings = stats.select_card(rows, key)
            self._send_json({"card": card, "listings": listings})

        def _api_listings(self, query):
            filters = self._filters(query)
            filters["limit"] = int((query.get("limit") or ["200"])[0])
            self._send_json({"listings": db.fetch_listings(app.conn, **filters)})

        def _api_facets(self):
            self._send_json(
                {
                    "rarity": _labelled(db.facet_counts(app.conn, "rarity"), RARITY_LABELS),
                    "language": _labelled(db.facet_counts(app.conn, "language"), LANGUAGE_LABELS),
                    "trade_type": _labelled(db.facet_counts(app.conn, "trade_type"), TRADE_LABELS),
                    "condition": _labelled(db.facet_counts(app.conn, "condition"), CONDITION_LABELS),
                }
            )

        def _api_collect(self):
            from .pipeline import collect

            with app.lock:
                if app.collecting:
                    return self._send_json({"status": "already_running"}, status=409)
                app.collecting = True

            def run():
                try:
                    app.last_report = collect(app.conn, app.cfg)
                except Exception as e:
                    log.exception("수집 실패")
                    app.last_report = {"errors": [str(e)]}
                finally:
                    app.collecting = False

            threading.Thread(target=run, daemon=True).start()
            self._send_json({"status": "started"})

        # ── 응답 도우미 ─────────────────────────────────────────────────────
        def _send_json(self, payload, status: int = 200):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, cache: bool = True):
            if not path.exists() or not path.is_file():
                return self._send_json({"error": "not found"}, status=404)
            body = path.read_bytes()
            ctype = EXTRA_TYPES.get(path.suffix) or mimetypes.guess_type(path.name)[0] \
                or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # 서비스 워커 파일이 캐시되면 앱 갱신이 막힌다.
            self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, rel: str):
            # 경로 탈출 방지
            target = (WEB_DIR / rel).resolve()
            if not str(target).startswith(str(WEB_DIR.resolve())):
                return self._send_json({"error": "forbidden"}, status=403)
            self._send_file(target)

        def _placeholder(self, name: str):
            """썸네일이 없는 카드를 위한 대체 이미지 (오프라인에서도 보이도록 직접 그린다)."""
            rarity = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("r") or [""])[0]
            svg = _card_svg(name.removesuffix(".svg"), rarity)
            body = svg.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _labelled(rows: list[dict], labels: dict) -> list[dict]:
    return [{**r, "label": labels.get(r["value"], r["value"])} for r in rows]


def _since_ts(days: int | None) -> int | None:
    return int(time.time()) - days * 86_400 if days else None


def _sort_key(sort: str):
    if sort == "price":
        return lambda c: c["median_price"] or 0
    if sort == "recent":
        return lambda c: c["last_seen"] or 0
    if sort == "trend":
        return lambda c: (c["trend"]["percent"] or 0)
    if sort == "name":
        return lambda c: c["display_name"]
    return lambda c: c["listing_count"]


def _card_svg(title: str, rarity: str) -> str:
    dark, light = RARITY_COLORS.get(rarity.upper(), DEFAULT_COLORS)
    safe = (title or "?")[:14]
    safe = safe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    badge = (rarity or "CARD").upper()[:6]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 336" width="240" height="336">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{dark}"/><stop offset="100%" stop-color="{light}"/>
    </linearGradient>
  </defs>
  <rect width="240" height="336" rx="14" fill="url(#g)"/>
  <rect x="12" y="12" width="216" height="312" rx="9" fill="none" stroke="rgba(255,255,255,.45)" stroke-width="2"/>
  <circle cx="120" cy="132" r="52" fill="rgba(255,255,255,.22)"/>
  <circle cx="120" cy="132" r="52" fill="none" stroke="rgba(255,255,255,.6)" stroke-width="3"/>
  <path d="M68 132h104" stroke="rgba(255,255,255,.6)" stroke-width="3"/>
  <circle cx="120" cy="132" r="16" fill="#fff"/>
  <text x="120" y="232" text-anchor="middle" font-family="system-ui,-apple-system,sans-serif"
        font-size="20" font-weight="700" fill="#fff">{safe}</text>
  <text x="120" y="262" text-anchor="middle" font-family="system-ui,-apple-system,sans-serif"
        font-size="13" fill="rgba(255,255,255,.85)">{badge}</text>
  <text x="120" y="300" text-anchor="middle" font-family="system-ui,-apple-system,sans-serif"
        font-size="11" fill="rgba(255,255,255,.6)">이미지 없음</text>
</svg>"""


def lan_ip() -> str | None:
    """휴대폰에서 접속할 때 쓸 이 컴퓨터의 LAN 주소를 찾는다."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 실제로 패킷을 보내지는 않는다. 어떤 인터페이스로 나가는지만 확인한다.
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return ip if not ip.startswith("127.") else None
    except OSError:
        return None
    finally:
        s.close()


def _auto_collect_loop(app: Dashboard, minutes: int) -> None:
    """앱을 켜 두면 주기적으로 알아서 새 글을 받아 온다."""
    from .pipeline import collect

    while True:
        time.sleep(minutes * 60)
        if app.collecting or not app.cfg.cafes:
            continue
        with app.lock:
            if app.collecting:
                continue
            app.collecting = True
        try:
            log.info("자동 수집을 시작합니다")
            app.last_report = collect(app.conn, app.cfg)
        except Exception:
            log.exception("자동 수집 실패")
        finally:
            app.collecting = False


def serve(conn, cfg: Config, open_browser: bool = False) -> None:
    app = Dashboard(conn, cfg)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(app))

    local = f"http://127.0.0.1:{cfg.port}"
    print(f"\n  포켓몬 카드 시세 보드가 열렸습니다")
    print(f"    이 컴퓨터   {local}")

    if cfg.host in ("0.0.0.0", "::"):
        ip = lan_ip()
        if ip:
            print(f"    휴대폰      http://{ip}:{cfg.port}   (같은 와이파이에 연결한 뒤 접속)")
        else:
            print("    휴대폰      LAN 주소를 찾지 못했습니다")

    if cfg.auto_collect_minutes > 0 and cfg.cafes:
        threading.Thread(
            target=_auto_collect_loop, args=(app, cfg.auto_collect_minutes), daemon=True
        ).start()
        print(f"    자동 수집   {cfg.auto_collect_minutes}분마다")

    print(f"\n  수집된 매물 {db.totals(conn)['articles']:,}건 · 끄려면 Ctrl+C\n")

    if open_browser:
        import webbrowser

        threading.Timer(0.7, lambda: webbrowser.open(local)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  종료합니다.")
    finally:
        httpd.server_close()


__all__ = ["serve", "format_won", "lan_ip"]
