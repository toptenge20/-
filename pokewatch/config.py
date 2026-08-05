"""설정 로딩. JSON 파일 + 환경변수."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.json")

# 시세가 없는 게시판 (후기·인증·질문 등). 포함 단어에 걸려도 여기 해당하면 뺀다.
DEFAULT_MENU_EXCLUDE = (
    "후기", "인증", "공지", "질문", "잡담", "자유", "가입", "출석",
    "등업", "신고", "정보", "이벤트", "회원",
)


def clean_cookie(raw: str | None) -> str | None:
    """붙여넣기 과정에서 섞인 줄바꿈·여분 공백을 걷어낸다.

    실제로 'NID_SES= AAAB...' 처럼 = 뒤에 공백이 들어가고 끝에 줄바꿈이 붙어
    있었다. 그 상태로 HTTP 헤더에 넣으면 파이썬이 값을 통째로 담은 예외를
    던지고, 그 예외가 로그에 남아 쿠키가 노출된다. 그래서 여기서 정리한다.
    """
    if not raw:
        return None

    pairs = []
    for chunk in re.split(r"[;\r\n]+", raw):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name, value = name.strip(), value.strip()
        # 헤더에 들어가면 안 되는 문자가 남아 있으면 그 조각은 버린다
        if not name or not value or re.search(r"[\r\n\x00-\x1f]", name + value):
            continue
        pairs.append(f"{name}={value}")

    return "; ".join(pairs) or None


@dataclass
class CafeTarget:
    name: str
    cafe_url: str | None = None      # 예: "pokemontcgkr" (cafe.naver.com/뒤 부분)
    club_id: int | None = None       # 숫자 ID를 이미 알면 바로 지정
    menu_ids: list[int] = field(default_factory=list)   # 비우면 카페 전체 최신글
    menu_name_filter: list[str] = field(default_factory=list)  # 예: ["장터", "거래"]
    # 이름에 이 단어가 들어간 게시판은 뺀다. '거래 후기 게시판'처럼 포함 단어에
    # 걸리지만 시세가 없는 곳이 많아서, 빼는 쪽이 더 중요하다.
    menu_name_exclude: list[str] = field(default_factory=lambda: list(DEFAULT_MENU_EXCLUDE))
    pages: int = 3
    per_page: int = 50
    # 이 카페들은 제목에 가격을 안 쓰고 본문에 적는다. 그래서 제목에서 가격을
    # 못 찾은 글은 본문까지 읽는다. 요청이 글 하나당 하나씩 늘어나므로 상한을 둔다.
    fetch_bodies: bool = True
    body_limit: int = 120

    @classmethod
    def from_dict(cls, d: dict) -> "CafeTarget":
        return cls(
            name=d.get("name") or d.get("cafe_url") or str(d.get("club_id") or "cafe"),
            cafe_url=d.get("cafe_url"),
            club_id=d.get("club_id"),
            menu_ids=[int(m) for m in d.get("menu_ids", [])],
            menu_name_filter=list(d.get("menu_name_filter", [])),
            menu_name_exclude=list(d.get("menu_name_exclude", DEFAULT_MENU_EXCLUDE)),
            pages=int(d.get("pages", 3)),
            per_page=int(d.get("per_page", 50)),
            fetch_bodies=bool(d.get("fetch_bodies", True)),
            body_limit=int(d.get("body_limit", 120)),
        )


@dataclass
class Config:
    db_path: str = "data/pokewatch.db"
    cookie: str | None = None
    delay: float = 0.8
    timeout: float = 15.0
    cafes: list[CafeTarget] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8765
    keep_days: int = 365
    min_confidence: float = 0.0
    auto_collect_minutes: int = 0  # 0이면 자동 수집 안 함

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path or os.environ.get("POKEWATCH_CONFIG") or DEFAULT_CONFIG_PATH)
        raw: dict = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)

        cfg = cls(
            db_path=raw.get("db_path", cls.db_path),
            cookie=raw.get("cookie"),
            delay=float(raw.get("delay", cls.delay)),
            timeout=float(raw.get("timeout", cls.timeout)),
            cafes=[CafeTarget.from_dict(c) for c in raw.get("cafes", [])],
            host=raw.get("host", cls.host),
            port=int(raw.get("port", cls.port)),
            keep_days=int(raw.get("keep_days", cls.keep_days)),
            min_confidence=float(raw.get("min_confidence", cls.min_confidence)),
            auto_collect_minutes=int(raw.get("auto_collect_minutes", cls.auto_collect_minutes)),
        )

        # 쿠키는 설정 파일보다 환경변수를 우선한다 (실수로 커밋되는 일을 줄이기 위해).
        env_cookie = os.environ.get("POKEWATCH_COOKIE")
        if env_cookie:
            cfg.cookie = env_cookie
        cfg.cookie = clean_cookie(cfg.cookie)
        if os.environ.get("POKEWATCH_DB"):
            cfg.db_path = os.environ["POKEWATCH_DB"]
        if os.environ.get("POKEWATCH_PORT"):
            cfg.port = int(os.environ["POKEWATCH_PORT"])

        return cfg
