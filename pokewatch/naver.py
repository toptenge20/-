"""네이버 카페 게시글 수집기.

네이버 카페가 웹/모바일 화면에서 쓰는 JSON 엔드포인트를 그대로 사용한다.
공개 카페의 공개 게시판은 로그인 없이 읽히고, 회원 전용 게시판은 로그인 쿠키
(NID_AUT / NID_SES)를 config 나 환경변수로 넘겨야 한다.

주의: 개인적으로 시세를 확인하는 용도로만 쓰고, 요청 간격(delay)을 줄이지 말 것.
카페 운영 정책과 robots.txt 를 확인하는 것은 사용자 책임이다.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator

log = logging.getLogger(__name__)

API_BASE = "https://apis.naver.com/cafe-web"
ARTICLE_LIST_URL = f"{API_BASE}/cafe2/ArticleListV2dot1.json"
SIDE_MENU_URL = f"{API_BASE}/cafe2/SideMenuList"
ARTICLE_URL = f"{API_BASE}/cafe-articleapi/v2/cafes/{{club_id}}/articles/{{article_id}}"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class NaverCafeError(RuntimeError):
    pass


@dataclass
class Article:
    club_id: int
    article_id: int
    menu_id: int | None
    menu_name: str
    subject: str
    writer: str
    written_at: int  # epoch seconds
    read_count: int = 0
    comment_count: int = 0
    thumbnail: str | None = None

    @property
    def url(self) -> str:
        return f"https://cafe.naver.com/ca-fe/cafes/{self.club_id}/articles/{self.article_id}"


@dataclass
class NaverCafeClient:
    cookie: str | None = None
    user_agent: str = DEFAULT_UA
    timeout: float = 15.0
    delay: float = 0.8          # 요청 사이 최소 대기 (초)
    max_retries: int = 3
    _last_request: float = field(default=0.0, repr=False)

    # ── 공개 API ────────────────────────────────────────────────────────────
    def resolve_club_id(self, cafe: str) -> int:
        """카페를 가리키는 무엇이든 받아서 숫자 clubId 로 바꾼다.

        다음을 모두 받는다:
          - 숫자 ID            12345678
          - 카페 주소          pokecardkorea
          - 전체 주소          https://cafe.naver.com/pokecardkorea
          - 모바일 주소        https://m.cafe.naver.com/ca-fe/web/cafes/12345678/...
          - 공유 짧은 링크     https://naver.me/xxxxxxxx

        휴대폰에서는 '공유 → 링크 복사' 로 얻은 짧은 링크밖에 없는 경우가 많아서,
        그것도 그대로 넣을 수 있게 했다.
        """
        cafe = cafe.strip()
        if cafe.isdigit():
            return int(cafe)

        if cafe.startswith(("http://", "https://")):
            return self._resolve_from_url(cafe)

        # 주소만 준 경우 (예: 'pokecardkorea')
        slug = cafe.rstrip("/").split("/")[-1]
        return self._resolve_from_url(f"https://cafe.naver.com/{urllib.parse.quote(slug)}")

    def _resolve_from_url(self, url: str) -> int:
        final_url, html = self._follow(url)

        club_id = _club_id_from_url(final_url)
        if club_id:
            return club_id

        for pattern in (r'"cafeId"\s*:\s*"?(\d+)', r"clubid=(\d+)",
                        r"g_sClubId\s*=\s*[\"'](\d+)", r'"cafeUrl"\s*:\s*"([^"]+)"'):
            m = re.search(pattern, html, re.IGNORECASE)
            if not m:
                continue
            value = m.group(1)
            if value.isdigit():
                return int(value)
            # 주소만 찾았으면 그 주소로 한 번 더 시도한다
            if value != final_url:
                return self._resolve_from_url(f"https://cafe.naver.com/{urllib.parse.quote(value)}")

        raise NaverCafeError(
            f"카페 ID를 찾지 못했습니다: {url}\n"
            f"  (최종 주소: {final_url})\n"
            "  카페 대문에서 'cafe.naver.com/○○○' 의 ○○○ 부분을 넣어 보세요."
        )

    def _follow(self, url: str) -> tuple[str, str]:
        """리다이렉트를 따라가 최종 주소와 본문을 돌려준다 (짧은 링크 해석용)."""
        final_url, body = self._get_bytes_with_url(url)
        text = body.decode("utf-8", errors="replace")

        # 자바스크립트나 meta 로 한 번 더 넘기는 경우가 있다
        for pattern in (r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=([^"\'>\s]+)',
                        r'location\.(?:replace|href)\s*=\s*["\']([^"\']+)["\']'):
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                nxt = urllib.parse.urljoin(final_url, _unescape(m.group(1)))
                if nxt != final_url and "naver.com" in nxt:
                    final_url, body = self._get_bytes_with_url(nxt)
                    text = body.decode("utf-8", errors="replace")
                break

        return final_url, text

    def list_menus(self, club_id: int) -> list[dict]:
        """게시판(메뉴) 목록. 어떤 menu_id 를 수집할지 고를 때 쓴다."""
        data = self._get_json(SIDE_MENU_URL, {"cafeId": club_id})
        menus = _dig(data, "message", "result", "menus") or _dig(data, "result", "menus") or []
        out = []
        for m in menus:
            menu_id = m.get("menuId") or m.get("menuid")
            if not menu_id:
                continue
            out.append(
                {
                    "menu_id": int(menu_id),
                    "name": m.get("menuName") or m.get("name") or "",
                    "type": m.get("menuType") or "",
                    "board_type": m.get("boardType") or "",
                }
            )
        return out

    def iter_articles(
        self,
        club_id: int,
        menu_id: int | None = None,
        pages: int = 3,
        per_page: int = 50,
    ) -> Iterator[Article]:
        """게시판 글을 최신순으로 훑는다."""
        for page in range(1, pages + 1):
            params = {
                "search.clubid": club_id,
                "search.queryType": "lastArticle",
                "search.page": page,
                "search.perPage": min(per_page, 50),
                "ad": "False",
            }
            if menu_id:
                params["search.menuid"] = menu_id

            data = self._get_json(ARTICLE_LIST_URL, params)
            rows = (
                _dig(data, "message", "result", "articleList")
                or _dig(data, "result", "articleList")
                or []
            )
            if not rows:
                log.info("club=%s menu=%s page=%s: 더 이상 글이 없습니다", club_id, menu_id, page)
                return
            for row in rows:
                article = _to_article(club_id, menu_id, row)
                if article:
                    yield article

    def get_article_body(self, club_id: int, article_id: int) -> dict:
        """본문 HTML과 이미지 목록. 제목만으로 부족할 때 보조로 쓴다."""
        url = ARTICLE_URL.format(club_id=club_id, article_id=article_id)
        data = self._get_json(url, {"query": "", "useCafeId": "true", "requestFrom": "A"})
        article = _dig(data, "result", "article") or {}
        html = article.get("contentHtml") or ""
        return {
            "subject": article.get("subject") or "",
            "content_html": html,
            "text": _html_to_text(html),
            "images": _extract_images(html),
        }

    # ── 내부 ────────────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Referer": "https://cafe.naver.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Accept-Encoding": "gzip",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        wait = self.delay - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.25))
        self._last_request = time.monotonic()

    def _get_bytes(self, url: str, params: dict | None = None) -> bytes:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            req = urllib.request.Request(url, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        payload = gzip.decompress(payload)
                    return payload
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise NaverCafeError(
                        f"접근이 거부되었습니다 (HTTP {e.code}). 회원 전용 게시판이라면 "
                        "로그인 쿠키(NID_AUT, NID_SES)를 설정하세요."
                    ) from e
                last_error = e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = e

            backoff = 2 ** attempt
            log.warning("요청 실패 (%s/%s), %s초 후 재시도: %s", attempt + 1, self.max_retries, backoff, last_error)
            time.sleep(backoff)

        raise NaverCafeError(f"요청에 실패했습니다: {url} ({last_error})")

    def _get_bytes_with_url(self, url: str) -> tuple[str, bytes]:
        """본문과 함께 (리다이렉트를 따라간) 최종 주소도 돌려준다."""
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            req = urllib.request.Request(url, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        payload = gzip.decompress(payload)
                    return resp.geturl(), payload
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise NaverCafeError(
                        f"접근이 거부되었습니다 (HTTP {e.code}). 회원 전용 게시판이라면 "
                        "로그인 쿠키(NID_AUT, NID_SES)를 설정하세요."
                    ) from e
                last_error = e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = e
            time.sleep(2 ** attempt)
        raise NaverCafeError(f"요청에 실패했습니다: {url} ({last_error})")

    def _get_text(self, url: str, params: dict | None = None) -> str:
        return self._get_bytes(url, params).decode("utf-8", errors="replace")

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        text = self._get_text(url, params)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            snippet = text[:200].replace("\n", " ")
            raise NaverCafeError(f"JSON 응답이 아닙니다: {snippet}") from e


# ── 파싱 도우미 ─────────────────────────────────────────────────────────────
# 주소 안에 숫자 카페 ID 가 들어 있는 형태들
_CLUB_ID_PATTERNS = [
    re.compile(r"/cafes/(\d+)"),          # m.cafe.naver.com/ca-fe/web/cafes/12345678/...
    # 'search.clubid=' 처럼 앞에 점이 붙는 형태도 있어서 단어 경계로 잡는다
    re.compile(r"\bclubid=(\d+)", re.I),
    re.compile(r"\bcafeId=(\d+)", re.I),
    re.compile(r"cafe\.naver\.com/ca-fe/cafes/(\d+)"),
]


def _club_id_from_url(url: str) -> int | None:
    for pattern in _CLUB_ID_PATTERNS:
        m = pattern.search(url)
        if m:
            return int(m.group(1))
    return None


# 카페가 아니라 네이버 내부 페이지를 가리키는 경로 조각
_RESERVED_SLUGS = {"ca-fe", "articleread", "articlelist", "mycafeintro",
                   "cafesearch", "gate", "home", "joincafe"}


def cafe_slug_from_url(url: str) -> str | None:
    """'https://cafe.naver.com/pokecardkorea/123' → 'pokecardkorea'.

    카페 주소에는 점이 들어갈 수 있다(예: 'pokemontcg.cafe'). 그래서 점을 허용하되,
    'ArticleRead.nhn' 같은 내부 페이지와 구분한다.
    """
    m = re.search(r"(?:m\.)?cafe\.naver\.com/([A-Za-z0-9_.-]+)", url)
    if not m:
        return None

    slug = m.group(1).rstrip(".")
    if slug.lower().endswith((".nhn", ".naver", ".html")):
        return None
    if slug.lower() in _RESERVED_SLUGS or slug.lower().split(".")[0] in _RESERVED_SLUGS:
        return None
    return slug


def _dig(data, *keys):
    cur = data
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _to_article(club_id: int, menu_id: int | None, row: dict) -> Article | None:
    article_id = row.get("articleId") or row.get("articleid")
    subject = row.get("subject") or row.get("articleTitle") or ""
    if not article_id or not subject:
        return None

    ts = row.get("writeDateTimestamp") or row.get("writeDate") or 0
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        ts = 0
    if ts > 10_000_000_000:  # 밀리초로 오는 경우
        ts //= 1000

    return Article(
        club_id=club_id,
        article_id=int(article_id),
        menu_id=int(row.get("menuId") or menu_id or 0) or None,
        menu_name=row.get("menuName") or "",
        subject=_unescape(subject),
        writer=row.get("writerNickname") or row.get("writerName") or "",
        written_at=ts,
        read_count=int(row.get("readCount") or 0),
        comment_count=int(row.get("commentCount") or 0),
        thumbnail=row.get("representImage") or row.get("thumbnail") or None,
    )


_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def _html_to_text(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    return _unescape(re.sub(r"[ \t]+", " ", text)).strip()


def _extract_images(html: str) -> list[str]:
    seen, out = set(), []
    for src in _IMG_RE.findall(html or ""):
        if src.startswith("//"):
            src = "https:" + src
        if src not in seen:
            seen.add(src)
            out.append(src)
    return out


def _unescape(s: str) -> str:
    import html as _html

    return _html.unescape(s or "")
