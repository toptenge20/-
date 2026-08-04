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

from . import db
from .config import Config
from .export import build_snapshot
from .parsing import format_won

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent / "web"

# mimetypes 가 모르는 확장자
EXTRA_TYPES = {".webmanifest": "application/manifest+json", ".js": "application/javascript"}

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
                if path == "/api/snapshot":
                    return self._api_snapshot()
                if path == "/api/status":
                    return self._api_status()
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
        def _api_snapshot(self):
            """대시보드가 쓰는 데이터 전부.

            거르고 묶는 일은 브라우저(web/data.js)가 한다. 정적 호스팅으로 내보낸
            snapshot.json 과 똑같은 모양이라 화면 코드가 두 모드에서 동일하게 돈다.
            """
            snapshot = build_snapshot(app.conn, cafes=[c.name for c in app.cfg.cafes])
            snapshot["collecting"] = app.collecting
            self._send_json(snapshot)

        def _api_status(self):
            self._send_json({"collecting": app.collecting, "last_report": app.last_report})

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

    return Handler


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
