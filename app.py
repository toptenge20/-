"""포켓몬 카드 시세 보드 — 데스크톱 실행기.

더블클릭하거나 `python3 app.py` 로 실행하면 서버를 켜고 창을 띄운다.
처음 실행이라 데이터가 하나도 없으면 예제 데이터를 넣어 화면이 비어 보이지 않게 한다.

  python3 app.py              창으로 실행
  python3 app.py --lan        휴대폰에서도 접속할 수 있게 실행
  python3 app.py --no-window  창 없이 서버만 (직접 브라우저로 접속)

pywebview 가 설치돼 있으면 주소창 없는 전용 창으로 열고, 없으면 기본 브라우저를
연다. PyInstaller 로 묶으면 파이썬 없이도 실행되는 앱이 된다 (packaging/ 참고).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

# PyInstaller 로 묶었을 때도 패키지를 찾을 수 있게 한다.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pokewatch import db  # noqa: E402
from pokewatch.config import Config  # noqa: E402
from pokewatch.server import lan_ip, make_handler, Dashboard  # noqa: E402

APP_TITLE = "포켓몬 카드 시세 보드"


def app_data_dir() -> Path:
    """OS 별로 앱 데이터를 두기에 적절한 위치.

    실행 파일 옆에 쓰면 설치 경로가 읽기 전용일 때 실패하므로 사용자 폴더를 쓴다.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / "pokewatch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_config(args) -> Config:
    # 실행 파일 옆의 config.json 을 먼저 보고, 없으면 앱 데이터 폴더를 본다.
    here = Path(__file__).resolve().parent / "config.json"
    cfg = Config.load(here if here.exists() else app_data_dir() / "config.json")

    if cfg.db_path == Config.db_path and not Path(cfg.db_path).exists():
        cfg.db_path = str(app_data_dir() / "pokewatch.db")
    if args.lan:
        cfg.host = "0.0.0.0"
    if args.port:
        cfg.port = args.port
    if args.auto_collect is not None:
        cfg.auto_collect_minutes = args.auto_collect
    return cfg


def find_free_port(preferred: int) -> int:
    """이미 쓰는 포트면 빈 포트를 찾아 준다 (앱을 두 번 켜도 죽지 않게)."""
    import socket

    for port in [preferred, *range(preferred + 1, preferred + 20)]:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0  # OS 가 알아서 고르게 한다


def _unbuffer_output() -> None:
    """터미널이 아닌 곳으로 출력이 흘러가면 파이썬이 버퍼링을 해서 안내 문구가 안 보인다.
    (PyInstaller 로 묶어 실행할 때 특히 그렇다.)"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass  # 창 모드로 묶으면 stdout 이 아예 없을 수 있다


def main() -> int:
    _unbuffer_output()
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--lan", action="store_true", help="같은 와이파이의 휴대폰에서도 접속")
    parser.add_argument("--no-window", action="store_true", help="창 없이 서버만 실행")
    parser.add_argument("--port", type=int)
    parser.add_argument("--auto-collect", type=int, metavar="분")
    args = parser.parse_args()

    cfg = load_config(args)
    wanted, cfg.port = cfg.port, find_free_port(cfg.port)
    if cfg.port != wanted:
        print(f"  {wanted}번 포트가 사용 중이라 {cfg.port}번으로 엽니다.")
    conn = db.connect(cfg.db_path)

    # 처음 켰는데 아무것도 없으면 빈 화면 대신 예제 데이터를 보여 준다.
    if db.totals(conn)["articles"] == 0:
        from pokewatch.demo import generate

        generate(conn)
        print("  처음 실행이라 예제 데이터를 넣었습니다. 실제 카페를 연결하려면 config.json 을 채우세요.")

    url = f"http://127.0.0.1:{cfg.port}"
    if args.no_window:
        from pokewatch.server import serve

        serve(conn, cfg, open_browser=False)
        return 0

    # 서버는 뒤에서 돌리고, 앞에서 창을 띄운다.
    server = _start_server(conn, cfg)
    print(f"\n  {APP_TITLE}")
    print(f"    {url}")
    if cfg.host == "0.0.0.0":
        ip = lan_ip()
        if ip:
            print(f"    휴대폰  http://{ip}:{cfg.port}")
    print("    창을 닫으면 종료됩니다.\n")

    try:
        _open_window(url)
    finally:
        server.shutdown()
    return 0


def _start_server(conn, cfg: Config):
    from http.server import ThreadingHTTPServer

    app = Dashboard(conn, cfg)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(app))

    if cfg.auto_collect_minutes > 0 and cfg.cafes:
        from pokewatch.server import _auto_collect_loop

        threading.Thread(
            target=_auto_collect_loop, args=(app, cfg.auto_collect_minutes), daemon=True
        ).start()

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _open_window(url: str) -> None:
    """pywebview 전용 창 → 없으면 기본 브라우저."""
    try:
        import webview  # pywebview

        webview.create_window(APP_TITLE, url, width=1180, height=860, min_size=(420, 600))
        webview.start()
        return
    except ImportError:
        pass

    import webbrowser

    webbrowser.open(url)
    print("  (pywebview 를 설치하면 전용 앱 창으로 열립니다: pip install pywebview)")
    print("  브라우저 창을 닫아도 서버는 계속 돌아갑니다. 끄려면 Ctrl+C 를 누르세요.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n  종료합니다.")


if __name__ == "__main__":
    sys.exit(main())
