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


if __name__ == "__main__":
    unittest.main()
