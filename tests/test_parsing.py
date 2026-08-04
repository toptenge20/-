"""제목 파서 테스트: python -m unittest discover tests"""

import unittest

from pokewatch.parsing import parse_price, parse_title


class PriceTest(unittest.TestCase):
    def check(self, text, expected):
        self.assertEqual(parse_price(text).price, expected, f"입력: {text!r}")

    def test_man_units(self):
        self.check("리자몽 SAR 12만", 120_000)
        self.check("리자몽 SAR 12만원", 120_000)
        self.check("리자몽 SAR 12.5만", 125_000)
        self.check("리자몽 SAR 12만5천", 125_000)
        self.check("리자몽 SAR 12만5", 125_000)
        self.check("리자몽 SAR 12만5000원", 125_000)

    def test_plain_won(self):
        self.check("가디안 ex 85,000원", 85_000)
        self.check("가디안 ex 85000원", 85_000)
        self.check("가디안 ex 85000", 85_000)
        self.check("피카츄 프로모 8천원", 8_000)
        self.check("피카츄 프로모 55k", 55_000)

    def test_ignores_card_numbers(self):
        info = parse_price("리자몽 ex SAR 165/165 12만")
        self.assertEqual(info.price, 120_000)

    def test_ignores_grade_score(self):
        info = parse_price("피카츄 프로모 PSA 10 42만원")
        self.assertEqual(info.price, 420_000)

    def test_ignores_quantity_and_year(self):
        self.assertIsNone(parse_price("슬리브 100장 팝니다").price)
        self.assertIsNone(parse_price("2024년 발매 카드 문의").price)

    def test_no_price_hints(self):
        self.assertIsNone(parse_price("리자몽 SAR 가격제시 받습니다").price)
        self.assertIsNone(parse_price("블래키 SA 쪽지주세요").price)

    def test_range(self):
        info = parse_price("리자몽 SAR 12~15만")
        self.assertEqual((info.price, info.price_max), (120_000, 150_000))

    def test_modifiers(self):
        info = parse_price("카드 일괄 30만 택포 네고가능")
        self.assertTrue(info.is_bundle)
        self.assertTrue(info.shipping_included)
        self.assertTrue(info.negotiable)

    def test_too_small_is_not_a_price(self):
        self.assertIsNone(parse_price("리자몽 3장 있어요").price)


class TitleTest(unittest.TestCase):
    def test_full_sell_post(self):
        info = parse_title("[판매] 리자몽 ex SAR (한글) SV5a 165/165 상태 S급 18만원 택포")
        self.assertEqual(info.trade_type, "sell")
        self.assertEqual(info.card_name, "리자몽")
        self.assertEqual(info.card_name_en, "Charizard")
        self.assertEqual(info.dex, 6)
        self.assertEqual(info.rarity, "SAR")
        self.assertEqual(info.language, "KO")
        self.assertEqual(info.condition, "MINT")
        self.assertEqual(info.set_code, "SV5A")
        self.assertEqual(info.card_no, "165/165")
        self.assertEqual(info.price, 180_000)
        self.assertTrue(info.shipping_included)

    def test_buy_post(self):
        info = parse_title("[삽니다] 블래키 VMAX SA 일판 구합니다 50만까지")
        self.assertEqual(info.trade_type, "buy")
        self.assertEqual(info.card_name, "블래키")
        self.assertEqual(info.language, "JP")
        self.assertEqual(info.price, 500_000)

    def test_graded_card(self):
        info = parse_title("[판매] 피카츄 프로모 PSA 10 42만원")
        self.assertEqual(info.grade_company, "PSA")
        self.assertEqual(info.grade_score, "10")
        self.assertEqual(info.rarity, "PROMO")
        self.assertEqual(info.price, 420_000)

    def test_longest_name_wins(self):
        # '리자드' 가 '리자몽' 을 가로채면 안 된다
        self.assertEqual(parse_title("[판매] 리자몽 VMAX 20만").card_name, "리자몽")
        self.assertEqual(parse_title("[판매] 리자드 R 5천원").card_name, "리자드")

    def test_english_name(self):
        info = parse_title("[판매] Charizard ex SAR 18만원")
        self.assertEqual(info.card_name, "리자몽")

    def test_rarity_priority(self):
        self.assertEqual(parse_title("망나뇽 VSTAR 9만").rarity, "VSTAR")
        self.assertEqual(parse_title("망나뇽 VMAX 9만").rarity, "VMAX")

    def test_trainer_card(self):
        info = parse_title("[판매] 릴리에 SR 일판 78만원")
        self.assertEqual(info.card_name, "릴리에")
        self.assertEqual(info.kind, "trainer")

    def test_unknown_card_still_parses(self):
        info = parse_title("[판매] 이름없는카드 3만원")
        self.assertIsNone(info.card_name)
        self.assertEqual(info.price, 30_000)
        self.assertTrue(info.card_key)

    def test_card_key_groups_same_card(self):
        a = parse_title("[판매] 리자몽 ex SAR 한글 18만원")
        b = parse_title("[팝니다] 리자몽 SAR (한글) 19만")
        self.assertEqual(a.card_key, b.card_key)

    def test_card_key_separates_language(self):
        a = parse_title("[판매] 리자몽 SAR 한글 18만원")
        b = parse_title("[판매] 리자몽 SAR 일판 24만원")
        self.assertNotEqual(a.card_key, b.card_key)

    def test_confidence(self):
        strong = parse_title("[판매] 리자몽 ex SAR 165/165 한글 18만원")
        weak = parse_title("[공지] 장터 이용 규칙 안내")
        self.assertGreater(strong.confidence, 0.8)
        self.assertLess(weak.confidence, 0.3)


class PipelineSmokeTest(unittest.TestCase):
    def test_demo_flow(self):
        import tempfile
        from pathlib import Path

        from pokewatch import db, stats
        from pokewatch.demo import generate

        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            generate(conn, count=120, seed=3)

            rows = db.fetch_listings(conn, named_only=True, priced_only=True)
            self.assertGreater(len(rows), 40)

            cards = stats.aggregate_cards(rows)
            self.assertGreater(len(cards), 5)
            for card in cards:
                self.assertIsNotNone(card["median_price"])
                self.assertLessEqual(card["min_price"], card["median_price"])
                self.assertGreaterEqual(card["max_price"], card["median_price"])

            self.assertGreater(stats.overview(rows, cards)["listings"], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
