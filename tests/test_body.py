"""본문 읽기에서 '못 봤다'와 '봤는데 가격이 없었다'를 구분하는지 확인한다.

이 둘을 섞으면 조용히 망가진다. 막힌 글을 '확인 완료'로 저장해 버리면,
나중에 로그인 쿠키를 넣어도 그 글을 다시 읽지 않기 때문이다. 실제로
그 버그가 있었고, 고친 줄 알았는데 예외가 중간에서 잡아먹혀 그대로였다.
"""

from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from pokewatch.naver import NaverAccessDenied, NaverCafeClient, NaverCafeError
from pokewatch.parsing import parse_title
from pokewatch.pipeline import _price_from_body


class FakeClient(NaverCafeClient):
    """실제 요청 대신 정해진 응답을 돌려준다."""

    def __init__(self, responses):
        super().__init__(delay=0)
        self._responses = responses
        self.calls = 0

    def _get_bytes(self, url, params=None, retries=None):
        self.calls += 1
        item = self._responses.pop(0) if self._responses else self._responses_default()
        if isinstance(item, int):
            # 진짜 _get_bytes 가 401/403 을 이 예외로 바꾼다 (아래 테스트에서 확인)
            raise NaverAccessDenied(f"접근이 거부되었습니다 (HTTP {item})")
        return item.encode("utf-8")

    @staticmethod
    def _responses_default():
        return "{}"


class TestHttpErrorMapping(unittest.TestCase):
    """진짜 _get_bytes 가 HTTP 401/403 을 NaverAccessDenied 로 바꾸는지."""

    def _raise(self, code):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "nope", {}, None)
        return fake_urlopen

    def test_401_and_403_become_access_denied(self):
        client = NaverCafeClient(delay=0)
        for code in (401, 403):
            with mock.patch("urllib.request.urlopen", self._raise(code)):
                with self.assertRaises(NaverAccessDenied):
                    client._get_bytes("https://example.test/x", retries=1)

    def test_500_is_a_plain_error(self):
        """500 은 '막힌 것'이 아니다. 다른 주소를 더 시도해 봐야 한다."""
        client = NaverCafeClient(delay=0)
        with mock.patch("urllib.request.urlopen", self._raise(500)):
            with self.assertRaises(NaverCafeError) as ctx:
                client._get_bytes("https://example.test/x", retries=1)
        self.assertNotIsInstance(ctx.exception, NaverAccessDenied)


class TestAccessDenied(unittest.TestCase):
    def test_401_raises_instead_of_empty_body(self):
        """모든 후보 주소가 401 이면 빈 본문이 아니라 예외가 나와야 한다."""
        client = FakeClient([401] * 10)
        with self.assertRaises(NaverAccessDenied):
            client.get_article_body(19480246, 560051)

    def test_pipeline_reports_denied(self):
        client = FakeClient([401] * 10)
        info = parse_title("리자몽 SAR 판매합니다")
        self.assertEqual(
            _price_from_body(client, 19480246, 560051, info, [0], "테스트"),
            "denied",
        )
        # 막힌 글에는 '확인 완료' 표시를 남기면 안 된다
        self.assertNotEqual(info.price_source, "body-none")

    def test_empty_body_is_none_not_denied(self):
        """열렸지만 내용이 없는 글은 'none' 이다 (막힌 것과 다르다)."""
        client = FakeClient(['{"result": {"article": {"contentHtml": ""}}}'] * 10)
        info = parse_title("리자몽 SAR 판매합니다")
        self.assertEqual(
            _price_from_body(client, 30418914, 1, info, [0], "테스트"),
            "none",
        )

    def test_price_in_body_is_found(self):
        client = FakeClient(
            ['{"result": {"article": {"contentHtml": "<p>리자몽 SAR 12만원에 팝니다</p>"}}}']
        )
        info = parse_title("리자몽 SAR")
        self.assertEqual(
            _price_from_body(client, 30418914, 2, info, [0], "테스트"),
            "found",
        )
        self.assertEqual(info.price, 120_000)
        self.assertEqual(info.price_source, "body")


class TestSampleBudgetIsPerCafe(unittest.TestCase):
    def test_samples_counter_is_passed_in(self):
        """표본 카운터가 카페마다 따로여야 한다 (전역이면 한 카페가 다 써 버린다)."""
        client = FakeClient(['{"result": {"article": {"contentHtml": "<p>사진 참고</p>"}}}'] * 20)
        samples = [0]
        for i in range(5):
            _price_from_body(client, 30418914, i, parse_title("리자몽 SAR"), samples, "테스트")
        # 상한(3)에서 멈춰야 한다
        self.assertEqual(samples[0], 3)

        other = [0]
        _price_from_body(client, 30418914, 99, parse_title("리자몽 SAR"), other, "다른 카페")
        self.assertEqual(other[0], 1)



class TestDenyStreak(unittest.TestCase):
    """막힌 글이 섞여 있어도, 열리는 글은 계속 읽어야 한다.

    첫 401 에서 카페 전체를 포기하게 만들었다가 실제 수집이 통째로 멈췄다.
    같은 카페 안에서도 공지는 막히고 거래글은 열리는 경우가 있다.
    """

    def _run(self, outcomes):
        """outcomes 순서대로 본문 결과를 내주는 가짜 수집을 돌린다."""
        from pokewatch import db, pipeline
        from pokewatch.config import CafeTarget
        from pokewatch.naver import Article

        conn = db.connect(":memory:")
        articles = [
            Article(club_id=1, article_id=i, menu_id=1, menu_name="거래",
                    subject="리자몽 SAR 판매", writer="누군가", written_at=1_700_000_000)
            for i in range(len(outcomes))
        ]

        class Client:
            def list_menus(self, club_id):
                return [{"menu_id": 1, "name": "거래 게시판"}]

            def iter_articles(self, club_id, menu_id, pages, per_page):
                return iter(articles)

        calls = []

        def fake_body(client, club_id, article_id, info, samples, cafe_name=""):
            calls.append(article_id)
            return outcomes[article_id]

        target = CafeTarget(name="테스트", club_id=1, menu_name_filter=["거래"],
                            menu_name_exclude=[], body_limit=len(outcomes))

        original = pipeline._price_from_body
        pipeline._price_from_body = fake_body
        try:
            result = pipeline._collect_cafe(conn, Client(), target)
        finally:
            pipeline._price_from_body = original
        return result, calls

    def test_scattered_denials_do_not_stop_collection(self):
        """막힌 글 사이에 열리는 글이 있으면 끝까지 읽는다."""
        outcomes = ["denied", "none", "denied", "found", "denied", "none"]
        result, calls = self._run(outcomes)
        self.assertEqual(len(calls), 6, "중간에 멈추면 안 된다")
        self.assertEqual(result["denied"], 3)
        self.assertEqual(result["body_prices"], 1)
        self.assertEqual(result["bodies"], 3)   # 막힌 3건은 '읽은 것'이 아니다

    def test_all_denied_stops_after_streak(self):
        """전부 막힌 카페는 상한에서 멈춰 요청을 아낀다."""
        outcomes = ["denied"] * 60
        result, calls = self._run(outcomes)
        self.assertEqual(len(calls), pipeline_deny_limit())
        self.assertEqual(result["bodies"], 0)


def pipeline_deny_limit():
    from pokewatch.pipeline import DENY_STREAK_LIMIT
    return DENY_STREAK_LIMIT


class TestMarketCost(unittest.TestCase):
    """장터 글은 목록 응답에 가격이 들어 있다. 본문도 쿠키도 필요 없다."""

    def _row(self, **extra):
        row = {"articleId": 1, "subject": "리자몽 SAR", "writeDateTimestamp": 1_700_000_000_000}
        row.update(extra)
        return row

    def test_cost_is_read_from_list(self):
        from pokewatch.naver import _to_article

        a = _to_article(1, 1, self._row(cost=45000, marketArticle=True))
        self.assertEqual(a.cost, 45000)
        self.assertTrue(a.is_market)

    def test_zero_cost_is_not_a_price(self):
        """공지 등은 cost 가 0으로 온다. 0원짜리 매물로 잡으면 시세가 망가진다."""
        from pokewatch.naver import _to_article

        self.assertIsNone(_to_article(1, 1, self._row(cost=0)).cost)
        self.assertIsNone(_to_article(1, 1, self._row(cost="")).cost)
        self.assertIsNone(_to_article(1, 1, self._row()).cost)

    def test_formatted_cost_is_a_fallback(self):
        from pokewatch.naver import _to_article

        a = _to_article(1, 1, self._row(cost=0, formattedCost="45,000"))
        self.assertEqual(a.cost, 45000)

    def test_market_price_wins_over_title(self):
        """제목의 숫자는 카드 번호일 수 있다. 카페가 매긴 값이 우선이다."""
        from pokewatch import db, pipeline
        from pokewatch.config import CafeTarget
        from pokewatch.naver import Article

        conn = db.connect(":memory:")
        article = Article(club_id=1, article_id=1, menu_id=1, menu_name="거래",
                          subject="리자몽 SAR 165/165 3만원", writer="누구",
                          written_at=1_700_000_000, cost=45000, is_market=True)

        class Client:
            def list_menus(self, club_id):
                return [{"menu_id": 1, "name": "거래 게시판"}]

            def iter_articles(self, club_id, menu_id, pages, per_page):
                return iter([article])

        target = CafeTarget(name="테스트", club_id=1, menu_name_filter=["거래"],
                            menu_name_exclude=[], fetch_bodies=False)
        result = pipeline._collect_cafe(conn, Client(), target)

        self.assertEqual(result["market_prices"], 1)
        row = conn.execute("SELECT price, price_source FROM listings").fetchone()
        self.assertEqual(row["price"], 45000)
        self.assertEqual(row["price_source"], "market")

    def test_market_price_skips_the_body_request(self):
        """가격을 이미 알면 본문을 읽을 이유가 없다 (요청 절약)."""
        from pokewatch import db, pipeline
        from pokewatch.config import CafeTarget
        from pokewatch.naver import Article

        conn = db.connect(":memory:")
        article = Article(club_id=1, article_id=1, menu_id=1, menu_name="거래",
                          subject="리자몽 SAR", writer="누구",
                          written_at=1_700_000_000, cost=45000, is_market=True)

        class Client:
            def list_menus(self, club_id):
                return [{"menu_id": 1, "name": "거래 게시판"}]

            def iter_articles(self, club_id, menu_id, pages, per_page):
                return iter([article])

        target = CafeTarget(name="테스트", club_id=1, menu_name_filter=["거래"],
                            menu_name_exclude=[], fetch_bodies=True, body_limit=10)

        called = []
        original = pipeline._price_from_body
        pipeline._price_from_body = lambda *a, **k: called.append(1) or "none"
        try:
            pipeline._collect_cafe(conn, Client(), target)
        finally:
            pipeline._price_from_body = original
        self.assertEqual(called, [], "가격을 아는데도 본문을 읽었다")

    def test_reparse_keeps_the_market_price(self):
        """파서를 고쳐 다시 해석해도 장터 가격은 남아야 한다."""
        from pokewatch import db, pipeline
        from pokewatch.naver import Article

        conn = db.connect(":memory:")
        article = Article(club_id=1, article_id=1, menu_id=1, menu_name="거래",
                          subject="리자몽 SAR 165/165", writer="누구",
                          written_at=1_700_000_000, cost=45000, is_market=True)
        db.upsert_article(conn, article, cafe_name="테스트")
        info = pipeline.parse_title(article.subject)
        info.price, info.price_source = 45000, "market"
        db.upsert_listing(conn, 1, 1, info, article.written_at)

        pipeline.reparse(conn)

        row = conn.execute("SELECT price, price_source FROM listings").fetchone()
        self.assertEqual(row["price"], 45000)
        self.assertEqual(row["price_source"], "market")

    def test_migration_adds_cost_to_an_old_file(self):
        """예전 DB 파일에도 cost 칸이 붙어야 한다 (기록을 버리지 않기 위해)."""
        import sqlite3
        import tempfile
        from pathlib import Path

        from pokewatch import db

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sqlite3.connect(path)
            old.execute("""CREATE TABLE articles (
                club_id INTEGER NOT NULL, article_id INTEGER NOT NULL, cafe_name TEXT,
                menu_id INTEGER, menu_name TEXT, subject TEXT NOT NULL, writer TEXT,
                written_at INTEGER NOT NULL DEFAULT 0, read_count INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0, thumbnail TEXT, url TEXT,
                collected_at INTEGER NOT NULL, PRIMARY KEY (club_id, article_id))""")
            old.commit()
            old.close()

            conn = db.connect(path)
            have = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
            self.assertIn("cost", have)


class TestMarketBoardSelection(unittest.TestCase):
    """장터 게시판은 이름 조건에 안 걸려도 수집해야 한다 (가격이 거기 있다)."""

    def test_market_board_is_picked_by_name(self):
        from pokewatch.pipeline import _is_market_board

        for name in ("싱글 트레이드(안심거래)", "진행중인 카드 경매", "중고장터"):
            self.assertTrue(_is_market_board({"name": name, "type": "", "board_type": ""}), name)

    def test_plain_board_is_not_a_market_board(self):
        from pokewatch.pipeline import _is_market_board

        for name in ("자유 게시판", "덱 트레이드", "질문 게시판"):
            self.assertFalse(_is_market_board({"name": name, "type": "L", "board_type": "L"}), name)

    def test_market_board_bypasses_the_name_filter(self):
        from pokewatch import db, pipeline
        from pokewatch.config import CafeTarget

        conn = db.connect(":memory:")
        seen = []

        class Client:
            def list_menus(self, club_id):
                return [
                    {"menu_id": 1, "name": "자유 게시판", "type": "L", "board_type": "L"},
                    {"menu_id": 2, "name": "싱글 트레이드(안심거래)", "type": "L",
                     "board_type": "L"},
                ]

            def iter_articles(self, club_id, menu_id, pages, per_page):
                seen.append(menu_id)
                return iter([])

        target = CafeTarget(name="테스트", club_id=1, menu_name_filter=["없는단어"],
                            menu_name_exclude=[])
        pipeline._collect_cafe(conn, Client(), target)
        self.assertEqual(seen, [2])

    def test_exclude_words_still_win(self):
        """'거래 후기 게시판'처럼 시세가 없는 곳은 여전히 빼야 한다."""
        from pokewatch import db, pipeline
        from pokewatch.config import CafeTarget

        conn = db.connect(":memory:")
        seen = []

        class Client:
            def list_menus(self, club_id):
                return [{"menu_id": 9, "name": "장터 거래 후기 게시판", "type": "L",
                         "board_type": "L"}]

            def iter_articles(self, club_id, menu_id, pages, per_page):
                seen.append(menu_id)
                return iter([])

        target = CafeTarget(name="테스트", club_id=1, menu_name_filter=["장터"],
                            menu_name_exclude=["후기"])
        pipeline._collect_cafe(conn, Client(), target)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
