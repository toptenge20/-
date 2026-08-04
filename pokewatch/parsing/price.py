"""한국어 중고 거래 글 제목에서 가격을 뽑아낸다.

'12만', '12.5만', '12만5천', '85,000원', '120000', '55k' 같은 표현을 모두 원(KRW)
단위 정수로 바꾼다. 카드 번호(165/165), 감정 점수(PSA 10), 수량(10장), 연도(2025년)
처럼 가격처럼 생겼지만 가격이 아닌 숫자는 걸러낸다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MIN_PRICE = 500
MAX_PRICE = 100_000_000

# 가격이 아님을 알리는 신호 (있으면 가격 미기재로 처리)
NO_PRICE_HINTS = (
    "가격제시",
    "가격 제시",
    "제시받",
    "제시 받",
    "쪽지주세요",
    "쪽지 주세요",
    "쪽지문의",
    "가격문의",
    "가격 문의",
    "비공개",
    "댓글참고",
)

# 숫자 바로 뒤에 오면 가격이 아닌 단위
UNIT_BLOCKERS = "장개팩박스년월일시분명번점기회판인칸종권살호도"

# 감정 점수 앞에 붙는 말 (PSA 10 → 10을 가격으로 읽지 않기 위해)
GRADE_PREFIX = re.compile(
    r"(psa|bgs|cgc|ars|sgc|beckett|등급|점수|서브|sub)\s*$", re.IGNORECASE
)

_NUM = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"

# 순서가 중요하다. 위쪽 패턴이 먼저 소비한다.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("man", re.compile(rf"(?P<base>{_NUM})\s*만\s*(?:(?P<tail>\d{{1,4}})\s*(?P<chun>천)?)?\s*원?")),
    ("chun", re.compile(rf"(?P<base>{_NUM})\s*천\s*원?")),
    ("won", re.compile(rf"(?P<base>{_NUM})\s*원")),
    ("k", re.compile(r"(?P<base>\d{1,5})\s*[kK](?![a-zA-Z])")),
    ("bare", re.compile(r"(?<![\d,./])(?P<base>\d{1,3}(?:,\d{3})+|\d{4,8})(?![\d,./])")),
]


@dataclass
class PriceInfo:
    price: int | None = None
    price_max: int | None = None
    price_text: str = ""
    is_bundle: bool = False          # 일괄 판매
    is_per_unit: bool = False        # 장당 / 개당
    shipping_included: bool = False  # 택포
    negotiable: bool = False         # 네고 가능
    candidates: list[int] = field(default_factory=list)

    @property
    def has_price(self) -> bool:
        return self.price is not None


# '12~15만' 처럼 단위를 뒤에서 한 번만 쓴 범위 표기
_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*([~\-–])\s*(\d+(?:\.\d+)?)\s*(만원|만|천원|천|원|k|K)"
)


def _expand_ranges(text: str) -> str:
    """'12~15만' → '12만~15만'. 앞쪽 숫자에도 단위를 붙여 둘 다 인식되게 한다."""
    return _RANGE_RE.sub(lambda m: f"{m.group(1)}{m.group(4)}{m.group(2)}{m.group(3)}{m.group(4)}", text)


def parse_price(raw: str) -> PriceInfo:
    info = PriceInfo()
    if not raw:
        return info

    text = _expand_ranges(raw)
    low = text.lower()
    info.is_bundle = any(w in text for w in ("일괄", "통판", "한번에"))
    info.is_per_unit = any(w in text for w in ("장당", "개당", "각각", "각 "))
    info.shipping_included = any(w in text for w in ("택포", "배송비포함", "택배포함", "무료배송"))
    info.negotiable = any(w in text for w in ("네고", "제안", "조정가능")) or "nego" in low

    if any(h in text.replace(" ", "") for h in (h.replace(" ", "") for h in NO_PRICE_HINTS)):
        return info

    found: list[tuple[int, int, int, str]] = []  # (start, end, value, raw)
    consumed: list[tuple[int, int]] = []

    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            if _overlaps(m.start(), m.end(), consumed):
                continue
            value = _to_won(kind, m)
            if value is None:
                continue
            if not _plausible(text, m, kind, value):
                continue
            consumed.append((m.start(), m.end()))
            found.append((m.start(), m.end(), value, m.group(0).strip()))

    if not found:
        return info

    found.sort(key=lambda t: t[0])
    values = [v for _, _, v, _ in found]
    info.candidates = values
    info.price_text = found[0][3]

    # '12~15만' 처럼 범위로 적은 경우: 가장 앞 두 값이 물결로 이어져 있으면 범위로 본다.
    if len(found) >= 2 and re.fullmatch(r"\s*[~\-–]\s*", text[found[0][1]:found[1][0]]):
        lo, hi = sorted((found[0][2], found[1][2]))
        info.price, info.price_max = lo, hi
        info.price_text = text[found[0][0]:found[1][1]].strip()
    else:
        info.price = values[0]
        if len(values) > 1:
            info.price_max = max(values)

    return info


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < e and s < end for s, e in spans)


def _to_won(kind: str, m: re.Match) -> int | None:
    raw = m.group("base").replace(",", "")
    try:
        base = float(raw)
    except ValueError:
        return None

    if kind == "man":
        value = base * 10_000
        tail = m.groupdict().get("tail")
        if tail:
            if m.groupdict().get("chun"):
                value += int(tail) * 1_000
            elif len(tail) == 1:
                # '12만5' → 125,000
                value += int(tail) * 1_000
            elif len(tail) == 4:
                # '12만5000' → 125,000
                value += int(tail)
            else:
                return None  # 해석이 모호하면 버린다
    elif kind == "chun":
        value = base * 1_000
    elif kind == "k":
        value = base * 1_000
    else:  # won, bare
        value = base

    value = int(round(value))
    if not (MIN_PRICE <= value <= MAX_PRICE):
        return None
    return value


def _plausible(text: str, m: re.Match, kind: str, value: int) -> bool:
    before = text[max(0, m.start() - 8):m.start()]
    after = text[m.end():m.end() + 2]

    if GRADE_PREFIX.search(before):
        return False

    # 165/165 같은 카드 번호
    if before.endswith("/") or after.startswith("/"):
        return False

    # sv5a, s12a 같은 세트 코드의 일부
    if re.search(r"[A-Za-z]$", before) and kind in ("bare", "k"):
        return False

    if after[:1] and after[0] in UNIT_BLOCKERS:
        return False
    if after.startswith("%"):
        return False

    if kind == "bare":
        # 단위 없는 숫자는 보수적으로: 3천원 이상 + 연도로 보이지 않을 것
        if value < 3_000:
            return False
        if 1990 <= value <= 2100 and "," not in m.group("base"):
            return False
        if after.strip()[:1] in ("~", "-") and "," not in m.group("base"):
            # 165-165 처럼 이어지는 숫자
            pass

    return True


def format_won(value: int | None) -> str:
    """12500000 → '1,250만원' 처럼 읽기 쉬운 한국어 표기."""
    if value is None:
        return "가격 미기재"
    if value >= 10_000:
        man = value / 10_000
        if abs(man - round(man)) < 0.01:
            return f"{int(round(man)):,}만원"
        return f"{man:,.1f}만원"
    return f"{value:,}원"
