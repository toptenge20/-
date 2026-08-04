"""네트워크 없이 화면을 확인하기 위한 예제 데이터 생성기.

실제 카페 글이 아니라, 한국 포켓몬 카드 장터에서 흔한 제목 형식을 흉내 낸
가짜 데이터다. 파서와 대시보드를 바로 시험해 볼 수 있다.
"""

from __future__ import annotations

import random
import time

from . import db
from .naver import Article
from .parsing import parse_title

DEMO_CLUB_ID = 99999999

# (카드명, 레어도 표기, 언어 표기, 기준가, 90일간 가격 흐름)
CARD_TEMPLATES = [
    ("리자몽", "ex SAR", "한글", 185_000, +0.18),
    ("리자몽", "VMAX HR", "일판", 240_000, -0.09),
    ("피카츄", "프로모", "한글", 42_000, +0.04),
    ("뮤츠", "ex SR", "한글", 88_000, -0.05),
    ("블래키", "VMAX SA", "일판", 520_000, +0.22),
    ("님피아", "ex SAR", "한글", 133_000, +0.11),
    ("가디안", "ex SAR", "한글", 96_000, -0.13),
    ("루카리오", "VSTAR SA", "일판", 71_000, +0.02),
    ("이브이", "AR", "한글", 12_000, +0.03),
    ("잠만보", "VMAX", "한글", 23_000, -0.02),
    ("레쿠쟈", "VMAX SA", "일판", 310_000, +0.07),
    ("개굴닌자", "ex SAR", "한글", 118_000, +0.15),
    ("갸라도스", "ex SR", "한글", 34_000, -0.06),
    ("미라이돈", "ex SAR", "한글", 64_000, -0.11),
    ("코라이돈", "ex SAR", "한글", 59_000, -0.08),
    ("마스카나", "ex SAR", "한글", 45_000, +0.05),
    ("릴리에", "SR", "일판", 780_000, +0.26),
    ("마리", "SR", "일판", 210_000, -0.04),
    ("아세로라", "SR", "일판", 165_000, +0.09),
    ("망나뇽", "ex SAR", "한글", 88_000, +0.06),
    ("루기아", "V SA", "일판", 143_000, -0.03),
    ("파이리", "AR", "한글", 9_000, +0.01),
    ("테라파고스", "ex SAR", "한글", 77_000, +0.12),
    ("우라오스", "VMAX HR", "일판", 92_000, -0.07),
]

SET_CODES = ["SV5a", "SV7", "SV8a", "s12a", "s11", "SV4a", "SV6"]
CONDITIONS = ["미개봉", "상태 S급", "민트", "상태 A급", "니어민트", ""]
SELL_PREFIX = ["[판매]", "[팝니다]", "[분양]", "[판매합니다]"]
BUY_PREFIX = ["[삽니다]", "[구합니다]", "[구매]"]
NICKS = ["포켓몬덕후", "카드왕", "리자몽사랑", "장터지기", "홀로그램", "레어헌터",
         "탑로더", "슬리브", "덱빌더", "카드수집가", "민트헌터", "PSA러버"]
MENUS = ["카드 장터", "싱글 거래", "미개봉/박스", "감정 카드 거래"]

# 파서가 걸러내야 하는 잡음 글
NOISE_TITLES = [
    "[질문] 이 카드 진품인가요?",
    "[시세] 요즘 리자몽 SAR 얼마정도 하나요?",
    "[나눔] 안쓰는 슬리브 나눔합니다",
    "[교환] 님피아 SAR 하고 블래키 SA 교환 원해요",
    "[판매] 카드 일괄 30장 25만원 일괄만",
    "[판매] 슬리브 100장 8,000원",
    "[구합니다] 가격제시 해주세요",
    "[공지] 장터 이용 규칙 안내",
    "[판매] 미개봉 부스터박스 2박스 각 13만원",
    "[판매] 탑로더 50개 1만원 택포",
]


def generate(conn, count: int = 420, days: int = 90, seed: int = 7) -> int:
    """예제 글을 만들어 DB에 넣는다. 반환값은 생성된 글 수."""
    rng = random.Random(seed)
    now = int(time.time())
    article_id = 1_000_000
    made = 0

    for _ in range(count):
        article_id += 1
        age_days = rng.random() ** 1.7 * days       # 최근일수록 글이 많게
        written_at = now - int(age_days * 86_400)

        if rng.random() < 0.12:
            subject = rng.choice(NOISE_TITLES)
        else:
            subject = _make_title(rng, days, age_days)

        article = Article(
            club_id=DEMO_CLUB_ID,
            article_id=article_id,
            menu_id=rng.randrange(10, 14),
            menu_name=rng.choice(MENUS),
            subject=subject,
            writer=rng.choice(NICKS) + str(rng.randrange(1, 99)),
            written_at=written_at,
            read_count=rng.randrange(20, 3000),
            comment_count=rng.randrange(0, 25),
            thumbnail=None,
        )
        db.upsert_article(conn, article, cafe_name="데모 카페")
        db.upsert_listing(conn, DEMO_CLUB_ID, article.article_id,
                          parse_title(subject), written_at)
        made += 1

    db.set_meta(conn, "demo", "1")
    db.set_meta(conn, "last_collect_at", now)
    conn.commit()
    return made


def _make_title(rng: random.Random, days: int, age_days: float) -> str:
    name, rarity, lang, base, drift = rng.choice(CARD_TEMPLATES)

    # 오래된 글일수록 과거 가격에 가깝도록 추세를 적용하고, 개별 노이즈를 얹는다.
    progress = 1 - (age_days / days)
    price = base * (1 + drift * progress) * rng.gauss(1.0, 0.09)
    price = max(3_000, int(round(price / 1_000) * 1_000))

    is_buy = rng.random() < 0.18
    prefix = rng.choice(BUY_PREFIX if is_buy else SELL_PREFIX)
    if is_buy:
        price = int(price * rng.uniform(0.75, 0.95) / 1000) * 1000

    parts = [prefix, name, rarity]

    if rng.random() < 0.55:
        parts.append(f"({lang})")
    if rng.random() < 0.35:
        parts.append(rng.choice(SET_CODES))
    if rng.random() < 0.3:
        total = rng.randrange(120, 200)
        parts.append(f"{rng.randrange(100, total + 1)}/{total}")
    if rng.random() < 0.22:
        parts.append(f"PSA {rng.choice(['10', '10', '9', '9.5'])}")
    cond = rng.choice(CONDITIONS)
    if cond:
        parts.append(cond)

    parts.append(_price_text(rng, price))

    if rng.random() < 0.25:
        parts.append(rng.choice(["택포", "네고가능", "선입금", "직거래우선"]))

    return " ".join(p for p in parts if p)


def _price_text(rng: random.Random, price: int) -> str:
    """같은 금액을 카페 글에서 쓰이는 여러 표기 중 하나로 쓴다."""
    style = rng.random()
    man = price / 10_000

    if style < 0.4 and price % 10_000 == 0:
        return f"{price // 10_000}만원"
    if style < 0.6:
        return f"{man:.1f}만"
    if style < 0.75 and price % 10_000 != 0:
        return f"{price // 10_000}만{(price % 10_000) // 1_000}천"
    if style < 0.9:
        return f"{price:,}원"
    return f"{price}"
