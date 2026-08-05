"""네이버 카페 게시글 수집기.

네이버 카페가 웹/모바일 화면에서 쓰는 JSON 엔드포인트를 그대로 사용한다.
공개 카페의 공개 게시판은 로그인 없이 읽히고, 회원 전용 게시판은 로그인 쿠키
(NID_AUT / NID_SES)를 config 나 환경변수로 넘겨야 한다.

주의: 개인적으로 시세를 확인하는 용도로만 쓰고, 요청 간격(delay)을 줄이지 말 것.
카페 운영 정책과 robots.txt 를 확인하는 것은 사용자 책임이다.
"""

from __future__ import annotations

import base64
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
# 본문 주소는 네이버가 바꿔 왔다. v2 는 지금 HTTP 500 을 낸다(로그로 확인).
# 어느 것이 살아 있는지 알 수 없어 순서대로 시도하고, 되는 것을 기억해 둔다.
ARTICLE_URL_CANDIDATES = (
    f"{API_BASE}/cafe-articleapi/v3/cafes/{{club_id}}/articles/{{article_id}}",
    f"{API_BASE}/cafe-articleapi/v2.1/cafes/{{club_id}}/articles/{{article_id}}",
    f"{API_BASE}/cafe-articleapi/v2/cafes/{{club_id}}/articles/{{article_id}}",
    "https://apis.naver.com/cafe-web/cafe-mobile/CafeArticleRead.json"
    "?cafeId={club_id}&articleId={article_id}",
    "https://cafe.naver.com/ArticleRead.nhn?clubid={club_id}&articleid={article_id}",
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class NaverCafeError(RuntimeError):
    pass


class NaverAccessDenied(NaverCafeError):
    """회원 전용이라 막힌 경우 (HTTP 401/403).

    '읽었는데 가격이 없었다' 와 구분해야 한다. 막힌 글을 '확인 완료' 로
    표시해 버리면, 나중에 로그인 쿠키를 넣어도 그 글을 다시 읽지 않는다.
    """


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
    # 장터 글이면 네이버가 목록에 가격을 실어 보낸다. 본문은 회원 전용이라
    # 못 읽지만 이 값은 로그인 없이 온다. 시세의 실제 출처는 여기다.
    cost: int | None = None
    is_market: bool = False
    on_sale: bool = True        # False 면 거래 완료

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
    _article_url_template: str | None = field(default=None, repr=False)
    _article_errors_logged: bool = field(default=False, repr=False)
    _list_shape_logged: bool = field(default=False, repr=False)

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

        for url in self._candidate_urls(cafe):
            club_id = self._try_resolve(url)
            if club_id:
                log.info("카페 ID를 찾았습니다: %s → %s", cafe, club_id)
                return club_id

        raise NaverCafeError(
            f"카페 ID를 찾지 못했습니다: {cafe}\n"
            "  카페 글 하나를 열어 '공유 → 링크 복사' 한 주소를 넣어 보세요.\n"
            "  글 주소에는 숫자 카페 ID가 들어 있어 확실합니다.\n"
            "  회원 전용 카페라면 POKEWATCH_COOKIE 로 로그인 쿠키가 필요합니다."
        )

    def _candidate_urls(self, cafe: str) -> list[str]:
        """카페 주소를 알아낼 수 있는 후보들. 성공률이 높은 순서로 늘어놓는다."""
        candidates: list[str] = []
        slug: str | None

        article_no = None
        if cafe.startswith(("http://", "https://")):
            candidates.append(cafe)
            slug = cafe_slug_from_url(cafe)
            # 'm.cafe.naver.com/cardmvk/744833' 처럼 글 번호가 붙어 있으면 따로 챙긴다.
            m = re.search(r"cafe\.naver\.com/[A-Za-z0-9_.-]+/(\d{3,})", cafe)
            if m:
                article_no = m.group(1)
        else:
            slug = cafe.rstrip("/").split("/")[-1]

        if slug:
            q = urllib.parse.quote(slug)
            # 모바일 페이지를 먼저 본다. 앱에서 복사한 주소가 이 형태라 잘 맞는다.
            # '○○○.cafe' 처럼 점이 붙어 오는 경우가 있는데 실제 주소는 앞부분이다.
            # (앱에서 복사한 주소가 그렇다. 로그로 확인한 실제 사례: cardmvk.cafe → cardmvk)
            stems = [slug]
            if "." in slug:
                stems.insert(0, slug.split(".")[0])

            for stem in stems:
                s = urllib.parse.quote(stem)
                # 글 번호가 있으면 데스크톱 글 주소를 먼저 본다. 예전 주소(ArticleRead.nhn)로
                # 넘어가면서 주소에 clubid 가 붙는 경우가 있다.
                if article_no:
                    candidates.append(f"https://cafe.naver.com/{s}/{article_no}")
                candidates += [
                    f"{API_BASE}/cafe-search-api/v1.0/cafes?query={s}",
                    f"{API_BASE}/cafe2/CafeProfileView.json?cluburl={s}",
                    f"{API_BASE}/cafe-mobile/CafeIntroView.json?cluburl={s}",
                    f"https://m.cafe.naver.com/{s}",
                    f"{API_BASE}/cafe2/CafeGate.json?cluburl={s}",
                    f"https://cafe.naver.com/{s}",
                ]

        seen, ordered = set(), []
        for url in candidates:
            if url not in seen:
                seen.add(url)
                ordered.append(url)
        return ordered

    def _try_resolve(self, url: str) -> int | None:
        """후보 주소 하나로 카페 ID를 찾아본다. 실패해도 예외를 올리지 않는다."""
        club_id = _club_id_from_url(url)
        if club_id:
            return club_id

        try:
            final_url, body = self._follow(url)
        except NaverCafeError as e:
            log.info("  %s → 실패 (%s)", url, e)
            return None

        # 주소를 못 알아들으면 네이버가 카페 홈 피드로 돌려보낸다
        if "section.cafe.naver.com" in final_url or "/ca-fe/home" in final_url:
            log.info("  %s → 카페 홈으로 튕김 (주소를 못 알아봤거나 로그인이 필요함)", url)
            return None

        club_id = _club_id_from_url(final_url)
        if club_id:
            return club_id

        for pattern in ID_PATTERNS:
            m = pattern.search(body)
            if m:
                return int(m.group(1))

        log.info("  %s → 카페 ID 없음 (최종 주소: %s, 본문 %d자)", url, final_url, len(body))
        # 왜 못 찾았는지 다음 실행 때 알 수 있도록 본문 단서를 남긴다.
        hints = _id_hints(body)
        if hints:
            for hint in hints:
                log.info("      단서: %s", hint)
        elif len(body) <= 500:
            log.info("      본문 전체: %s", re.sub(r"\s+", " ", body).strip())
        else:
            log.info("      본문 앞부분: %s", re.sub(r"\s+", " ", body[:300]).strip())
        return None

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

            # 본문은 회원 전용이라 못 읽는다(로그로 확인). 그렇다면 목록에 이미
            # 가격이 들어 있는지 봐야 한다 — 네이버 장터 게시판은 값을 따로
            # 갖고 있는 경우가 있고, 그러면 본문도 쿠키도 필요 없어진다.
            if not self._list_shape_logged:
                self._list_shape_logged = True
                log.info("글 목록 항목이 가진 값: %s", ", ".join(sorted(rows[0])))
                fields = _price_like_fields(rows[0])
                log.info("  가격 비슷한 값: %s", fields or "(없음)")

            for row in rows:
                article = _to_article(club_id, menu_id, row)
                if article:
                    yield article

    def get_article_body(self, club_id: int, article_id: int) -> dict:
        """본문 HTML과 이미지 목록. 제목에 가격이 없을 때 여기서 찾는다."""
        urls = ARTICLE_URL_CANDIDATES
        if self._article_url_template:            # 한 번 성공한 주소를 계속 쓴다
            urls = (self._article_url_template,)

        errors = []
        denied = False
        for template in urls:
            url = template.format(club_id=club_id, article_id=article_id)
            sep = "&" if "?" in url else "?"
            try:
                # 안 되는 주소에 세 번씩 매달리면 수집이 너무 느려진다
                raw = self._get_bytes(f"{url}{sep}query=&useCafeId=true&requestFrom=A",
                                      retries=1)
            except NaverAccessDenied as e:
                denied = True
                errors.append(f"{template.split('?')[0]}: {e}")
                continue
            except NaverCafeError as e:
                errors.append(f"{template.split('?')[0]}: {e}")
                continue

            body = self._parse_article(raw.decode("utf-8", errors="replace"))
            if body["text"] or body["content_html"]:
                if self._article_url_template != template:
                    self._article_url_template = template
                    log.info("본문 주소를 찾았습니다: %s", template.split("?")[0])
                return body
            errors.append(f"{template.split('?')[0]}: 본문이 비어 있음")

        if not self._article_errors_logged:
            self._article_errors_logged = True
            for e in errors:
                log.info("  본문 주소 실패 — %s", e)

        # 한 군데라도 '회원 전용'이라고 답했으면, 이건 '본문이 없는 글'이 아니라
        # '못 보는 글'이다. 그대로 빈 본문을 돌려주면 부르는 쪽이 둘을 구분하지
        # 못해, 막힌 글 전체를 '가격 없음'으로 기록하고 계속 요청하게 된다.
        if denied:
            raise NaverAccessDenied(
                f"본문이 회원 전용입니다 (글 {article_id}). 로그인 쿠키(NID_AUT, NID_SES)가 "
                "있어야 읽을 수 있습니다."
            )
        return {"subject": "", "content_html": "", "text": "", "images": [],
                "shape": None, "price_fields": {}}

    @staticmethod
    def _parse_article(text: str) -> dict:
        """JSON 응답이면 JSON 에서, HTML 페이지면 본문 영역에서 내용을 꺼낸다."""
        html = ""
        subject = ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None

        if isinstance(data, dict):
            article = (_dig(data, "result", "article") or _dig(data, "article")
                       or _dig(data, "message", "result", "article") or {})
            html = article.get("contentHtml") or article.get("content") or ""
            subject = article.get("subject") or ""
        else:
            m = re.search(r'<div[^>]+class="[^"]*(?:ContentRenderer|article_viewer|NHN_Writeform_Main)[^"]*"[^>]*>(.*?)</div>\s*(?:<div|</body)',
                          text, re.DOTALL | re.IGNORECASE)
            html = m.group(1) if m else ""
            sm = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
            subject = _unescape(sm.group(1).strip()) if sm else ""

        return {
            "subject": subject,
            "content_html": html,
            "text": _html_to_text(html),
            "images": _extract_images(html),
            # 왜 가격을 못 찾는지 판별하기 위한 단서. 응답이 어떤 모양인지,
            # 값 이름에 price/cost 같은 게 있는지 본다. (장터 글이면 네이버가
            # 가격을 구조화된 값으로 갖고 있을 수 있다)
            "shape": _shape_hint(data),
            "price_fields": _price_like_fields(data),
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

    def _get_bytes(self, url: str, params: dict | None = None,
                   retries: int | None = None) -> bytes:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        last_error: Exception | None = None
        for attempt in range(retries or self.max_retries):
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
                    raise NaverAccessDenied(
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
                    raise NaverAccessDenied(
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
# 페이지 본문에서 숫자 카페 ID 를 찾는 패턴들
ID_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r'"cafeId"\s*:\s*"?(\d+)',
        r'"clubId"\s*:\s*"?(\d+)',
        r"clubid[\"']?\s*[:=]\s*[\"']?(\d+)",
        r"g_sClubId\s*=\s*[\"'](\d+)",
        r'"cafeIdIntoUrl"\s*:\s*"?(\d+)',
        r'cafe[_-]?id["\']?\s*[:=]\s*["\']?(\d{5,12})',
        r'/cafes/(\d{5,12})',
    )
]


def _id_hints(body: str, limit: int = 4) -> list[str]:
    """카페 ID 를 못 찾았을 때, 본문에서 ID 처럼 보이는 부분을 로그에 남긴다.

    네이버가 페이지 구조를 바꾸면 어떤 형태로 들어 있는지 알 길이 없다.
    다음 실행 로그만 보고 패턴을 고칠 수 있도록 단서를 뽑아 둔다.
    """
    hints, seen = [], set()
    for m in re.finditer(r"(?:cafe|club)[_-]?(?:id|Id|ID)", body):
        start, end = max(0, m.start() - 30), min(len(body), m.end() + 50)
        snippet = re.sub(r"\s+", " ", body[start:end]).strip()
        if snippet in seen:
            continue
        seen.add(snippet)
        hints.append(snippet)
        if len(hints) >= limit:
            break
    return hints


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
    return _club_id_from_share_token(url)


def _club_id_from_share_token(url: str) -> int | None:
    """공유 링크에 붙는 art 토큰에서 카페 번호를 꺼낸다.

    카페 앱에서 '공유 → 링크 복사' 하면 이런 주소가 나온다.

        https://m.cafe.naver.com/pokemontcg/559991?art=<베이스64>.<베이스64>.<서명>

    가운데 조각을 풀면 {"cafeId":19480246, "articleId":559991, ...} 이 들어 있다.
    모바일 페이지 본문에는 카페 번호가 없어서, 이게 가장 확실한 통로다.
    """
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except ValueError:
        return None

    for token in query.get("art", []):
        for part in token.split("."):
            data = _decode_b64_json(part)
            if isinstance(data, dict) and str(data.get("cafeId", "")).isdigit():
                return int(data["cafeId"])
    return None


def _decode_b64_json(part: str):
    try:
        padded = part + "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
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
        cost=_market_cost(row),
        is_market=bool(row.get("marketArticle") or row.get("productSale")
                       or row.get("nfleaMarketSale")),
        # 값이 없으면 '판매 중'으로 본다. 장터 글이 아니면 애초에 의미가 없다.
        on_sale=bool(row.get("onSale", True)),
    )


# 장터 가격의 상식 범위. 위쪽을 열어 두면 '가격 대신 아무 숫자'가 시세를
# 통째로 망친다 (실제로 1억원짜리 부스터, 1,111.1만원짜리 카드가 들어왔다).
MARKET_COST_MIN = 500
MARKET_COST_MAX = 30_000_000


def _market_cost(row: dict) -> int | None:
    """장터 글의 가격. 공지처럼 값이 없거나, 가격이 아닌 값이면 None.

    'cost' 는 원 단위 정수로 온다. 'formattedCost' 는 '45,000' 같은 표기라
    숫자만 남겨 예비로 쓴다.
    """
    for key in ("cost", "formattedCost"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        digits = re.sub(r"[^\d]", "", str(raw))
        if not digits:
            continue
        value = int(digits)
        if _plausible_cost(value):
            return value
    return None


def _plausible_cost(value: int) -> bool:
    """가격 칸에 들어온 숫자가 실제 가격인지.

    '가격은 쪽지로' 라는 뜻으로 11111111 이나 99999999 를 적는 사람이 있다.
    그런 값이 하나만 섞여도 평균·최고가가 통째로 망가진다.
    """
    if not (MARKET_COST_MIN <= value <= MARKET_COST_MAX):
        return False

    # 뒤의 0을 떼고 같은 숫자만 남으면 자리를 채운 값이다.
    # (11,111,000 → '11111', 99,999 → '99999'). 11,000 → '11' 은 짧아서 통과.
    stem = str(value).rstrip("0") or "0"
    if len(stem) >= 4 and len(set(stem)) == 1:
        return False
    return True


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


_PRICE_KEY_RE = re.compile(r"price|cost|sale|money|amount|won|가격", re.IGNORECASE)


def _shape_hint(data, depth: int = 3) -> str:
    """응답 JSON의 뼈대만 짧게 적는다 (본문 내용은 넣지 않는다)."""
    if isinstance(data, dict):
        if depth <= 0:
            return "{…}"
        keys = list(data)[:12]
        inner = ", ".join(f"{k}:{_shape_hint(data[k], depth - 1)}" for k in keys)
        if len(data) > 12:
            inner += ", …"
        return "{" + inner + "}"
    if isinstance(data, list):
        return f"[{len(data)}]" + (_shape_hint(data[0], depth - 1) if data and depth > 0 else "")
    if isinstance(data, str):
        return f"str({len(data)})"
    return type(data).__name__


def _price_like_fields(data, path: str = "", out: dict | None = None) -> dict:
    """이름에 price/cost 같은 게 들어간 값을 모은다. 장터 글이면 여기에 가격이 있다."""
    if out is None:
        out = {}
    if len(out) >= 12:
        return out
    if isinstance(data, dict):
        for k, v in data.items():
            here = f"{path}.{k}" if path else k
            if isinstance(v, (str, int, float)) and _PRICE_KEY_RE.search(k):
                text = str(v)
                if text and text.lower() not in ("none", "null", "false"):
                    out[here] = text[:80]
            elif isinstance(v, (dict, list)):
                _price_like_fields(v, here, out)
    elif isinstance(data, list):
        for i, v in enumerate(data[:5]):
            _price_like_fields(v, f"{path}[{i}]", out)
    return out


def _unescape(s: str) -> str:
    import html as _html

    return _html.unescape(s or "")
