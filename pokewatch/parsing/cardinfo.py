"""글 제목에서 '어떤 카드인지'를 뽑아낸다.

거래 유형, 카드 이름, 레어도, 감정 등급, 언어(한글/일판/영문), 상태, 세트 코드,
카드 번호를 추출하고, 같은 카드를 하나로 묶기 위한 card_key를 만든다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict

from .pokedex import load_pokedex
from .price import PriceInfo, parse_price

# ── 거래 유형 ────────────────────────────────────────────────────────────────
TRADE_TYPES: list[tuple[str, tuple[str, ...]]] = [
    # 실제 카페 제목에서 확인한 표현들을 넣었다 ('급처합니다', '구해봅니다' 등)
    ("buy", ("삽니다", "구합니다", "구매합니다", "구매", "구함", "삽니당", "wtb",
             "구해요", "구해봅", "구입", "사요", "삽")),
    ("sell", ("판매", "팝니다", "분양", "양도", "판매합니다", "팔아요", "wts",
              "급처", "처분", "정리합", "내놓", "팜", "팝니당", "넘겨")),
    ("trade", ("교환", "트레이드", "바꿔요")),
    ("free", ("나눔", "무료나눔")),
    ("info", ("시세", "문의", "질문", "감정")),
]

TRADE_LABELS = {
    "sell": "판매",
    "buy": "구매",
    "trade": "교환",
    "free": "나눔",
    "info": "시세/문의",
    "unknown": "미분류",
}

# ── 레어도 ───────────────────────────────────────────────────────────────────
# 긴 토큰이 먼저 잡히도록 순서를 유지한다 (VSTAR 가 V 보다 먼저).
RARITY_PATTERNS: list[tuple[str, str]] = [
    ("VSTAR", r"v\s*star|브이스타"),
    ("VMAX", r"v\s*max|브이맥스"),
    ("VUNION", r"v\s*union|브이유니온"),
    ("SAR", r"\bsar\b|스페셜아트레어"),
    ("CSR", r"\bcsr\b"),
    ("CHR", r"\bchr\b"),
    ("SSR", r"\bssr\b"),
    ("RRR", r"\brrr\b"),
    ("UR", r"\bur\b|울트라레어"),
    ("HR", r"\bhr\b|하이퍼레어"),
    ("SR", r"\bsr\b|슈퍼레어"),
    ("AR", r"\bar\b|아트레어"),
    ("RR", r"\brr\b"),
    ("SA", r"\bsa\b|스페셜아트|풀아트|full\s*art"),
    ("PROMO", r"프로모|promo|\bprm\b"),
    ("EX", r"\bex\b|\bｅｘ\b"),
    ("GX", r"\bgx\b"),
    ("V", r"(?<![a-z])v(?![a-z])|브이"),
    ("TAG", r"태그팀|tag\s*team"),
    ("BOX", r"\b박스\b|부스터박스|booster\s*box"),
]

RARITY_LABELS = {
    "SAR": "스페셜 아트 레어",
    "SR": "슈퍼 레어",
    "AR": "아트 레어",
    "UR": "울트라 레어",
    "HR": "하이퍼 레어",
    "CHR": "캐릭터 레어",
    "CSR": "캐릭터 슈퍼 레어",
    "SSR": "SSR",
    "RRR": "RRR",
    "RR": "더블 레어",
    "SA": "스페셜 아트",
    "PROMO": "프로모",
    "EX": "ex",
    "GX": "GX",
    "V": "V",
    "VMAX": "VMAX",
    "VSTAR": "VSTAR",
    "VUNION": "V-UNION",
    "TAG": "태그팀",
    "BOX": "박스/미개봉",
}

# ── 언어 ─────────────────────────────────────────────────────────────────────
LANGUAGES: list[tuple[str, tuple[str, ...]]] = [
    ("KO", ("한글", "한판", "국내판", "한국어", "정발", "한글판")),
    ("JP", ("일판", "일본", "일어", "일본어", "jp", "japan")),
    ("EN", ("영문", "영판", "미판", "영어", "eng", "english")),
    ("CN", ("중문", "중국", "간체", "번체")),
]
LANGUAGE_LABELS = {"KO": "한글", "JP": "일판", "EN": "영문", "CN": "중문", "UNK": "미표기"}

# ── 상태 ─────────────────────────────────────────────────────────────────────
CONDITIONS: list[tuple[str, tuple[str, ...]]] = [
    ("SEALED", ("미개봉", "실링", "새제품", "언박싱전")),
    ("MINT", ("민트", "mint", "최상", "s급", "상태최상", "완전깨끗")),
    ("NEAR_MINT", ("니어민트", "near mint", "\bnm\b", "a급", "상태좋음", "깨끗")),
    ("PLAYED", ("플레이드", "played", "b급", "c급", "흠집", "스크래치", "찍힘", "휨", "화이트닝")),
]
CONDITION_LABELS = {
    "SEALED": "미개봉",
    "MINT": "민트/S급",
    "NEAR_MINT": "니어민트/A급",
    "PLAYED": "플레이드/흠집",
    "UNK": "상태 미표기",
}

GRADE_RE = re.compile(
    r"\b(psa|bgs|cgc|ars|sgc)\s*[-:]?\s*(10|9\.5|9|8\.5|8|7|6|5|4|3|2|1)\b", re.IGNORECASE
)
SET_CODE_RE = re.compile(r"\b(sv\d{1,2}[a-z]?|s\d{1,2}[a-z]?|sm\d{1,2}[a-z]?|xy\d{1,2}|bw\d{1,2})\b", re.IGNORECASE)
CARD_NO_RE = re.compile(r"\b(\d{1,3})\s*/\s*(\d{1,3})\b")
BRACKET_RE = re.compile(r"[\[\(【（]([^\]\)】）]{1,20})[\]\)】）]")

# 포켓몬 이름을 부분 문자열로 품고 있는 상품 용어. 이름 매칭 전에 걷어낸다.
# (예: '부스터박스'의 '부스터'가 Flareon 으로 잡히는 것을 막는다)
NOISE_COMPOUNDS = re.compile(
    r"부스터\s*박스|부스터\s*팩|booster\s*box|booster\s*pack|"
    r"스타터\s*덱|하이클래스\s*팩|확장\s*팩|프리미엄\s*트레이너\s*박스",
    re.IGNORECASE,
)


@dataclass
class CardInfo:
    title: str
    trade_type: str = "unknown"
    trade_label: str = "미분류"
    card_name: str | None = None
    card_name_en: str | None = None
    dex: int | None = None
    kind: str | None = None
    rarity: str | None = None
    rarity_label: str | None = None
    language: str = "UNK"
    condition: str = "UNK"
    grade_company: str | None = None
    grade_score: str | None = None
    set_code: str | None = None
    card_no: str | None = None
    quantity: int = 1
    price: int | None = None
    price_max: int | None = None
    price_text: str = ""
    price_source: str | None = None   # 'title' | 'body' | None
    is_bundle: bool = False
    is_per_unit: bool = False
    shipping_included: bool = False
    negotiable: bool = False
    card_key: str = ""
    display_name: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def parse_title(title: str) -> CardInfo:
    """거래글 제목 하나를 구조화된 카드 정보로 변환한다."""
    raw = unicodedata.normalize("NFKC", title or "").strip()
    low = raw.lower()

    info = CardInfo(title=raw)

    info.trade_type = _match_first(low, TRADE_TYPES, default="unknown")
    info.trade_label = TRADE_LABELS[info.trade_type]

    # 감정 등급은 레어도보다 먼저 뽑아둬야 'PSA 10' 의 10이 다른 데 쓰이지 않는다.
    gm = GRADE_RE.search(low)
    if gm:
        info.grade_company = gm.group(1).upper()
        info.grade_score = gm.group(2)

    info.rarity = _match_rarity(low)
    info.rarity_label = RARITY_LABELS.get(info.rarity) if info.rarity else None
    info.language = _match_first(low, LANGUAGES, default="UNK")
    info.condition = _match_condition(low)

    sm = SET_CODE_RE.search(low)
    if sm:
        info.set_code = sm.group(1).upper()

    cm = CARD_NO_RE.search(raw)
    if cm:
        info.card_no = f"{cm.group(1)}/{cm.group(2)}"

    qm = re.search(r"(\d{1,3})\s*(?:장|개|매)", raw)
    if qm:
        info.quantity = max(1, int(qm.group(1)))

    price = parse_price(raw)
    _apply_price(info, price)

    hit = load_pokedex().best(_strip_noise(raw))
    if hit:
        info.card_name = hit.name_ko
        info.card_name_en = hit.name_en
        info.dex = hit.dex
        info.kind = hit.kind

    info.display_name = _display_name(info)
    info.card_key = _card_key(info)
    info.confidence = _confidence(info)
    return info


def _apply_price(info: CardInfo, price: PriceInfo) -> None:
    info.price = price.price
    info.price_max = price.price_max
    info.price_text = price.price_text
    info.is_bundle = price.is_bundle
    info.is_per_unit = price.is_per_unit
    info.shipping_included = price.shipping_included
    info.negotiable = price.negotiable


def _match_first(low: str, table: list[tuple[str, tuple[str, ...]]], default: str) -> str:
    best_pos, best_key = None, default
    for key, words in table:
        for w in words:
            pos = low.find(w.lower())
            if pos >= 0 and (best_pos is None or pos < best_pos):
                best_pos, best_key = pos, key
    return best_key


def _match_rarity(low: str) -> str | None:
    # 감정 등급 표기를 지워 'BGS 9.5' 의 s 같은 조각이 레어도로 잡히지 않게 한다.
    cleaned = GRADE_RE.sub(" ", low)
    for key, pattern in RARITY_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return key
    return None


def _match_condition(low: str) -> str:
    for key, words in CONDITIONS:
        for w in words:
            if w.startswith("\\b"):
                if re.search(w, low):
                    return key
            elif w in low:
                return key
    return "UNK"


def _strip_noise(text: str) -> str:
    """카드 이름을 찾기 전에 가격·번호·대괄호 머리말을 걷어낸다."""
    out = NOISE_COMPOUNDS.sub(" ", text)
    out = BRACKET_RE.sub(" ", out)
    out = GRADE_RE.sub(" ", out)
    out = CARD_NO_RE.sub(" ", out)
    out = re.sub(r"\d[\d,.]*\s*(만원|만|천원|천|원|k|K)", " ", out)
    return out


def _display_name(info: CardInfo) -> str:
    if not info.card_name:
        # 이름을 못 찾았으면 제목 앞부분을 그대로 보여준다.
        stripped = BRACKET_RE.sub(" ", info.title).strip()
        return (stripped[:28] or info.title[:28]) or "제목 없음"
    parts = [info.card_name]
    if info.rarity:
        parts.append(RARITY_LABELS.get(info.rarity, info.rarity))
    return " ".join(parts)


def _card_key(info: CardInfo) -> str:
    """같은 카드를 묶는 키: 이름|레어도|언어.

    세트 코드와 카드 번호는 일부 글에만 적혀 있어서 키에 넣으면 같은 카드가
    잘게 쪼개진다. 그래서 키에서는 빼고 상세 화면의 부가 정보로만 쓴다.
    """
    base = info.card_name or ("?" + re.sub(r"\s+", "", info.title)[:16])
    return f"{base}|{info.rarity or '-'}|{info.language}"


def _confidence(info: CardInfo) -> float:
    """이 글을 시세 데이터로 믿어도 되는지에 대한 0~1 점수."""
    score = 0.0
    if info.card_name:
        score += 0.45
    if info.price is not None:
        score += 0.3
    if info.rarity:
        score += 0.1
    if info.card_no or info.set_code:
        score += 0.08
    if info.trade_type in ("sell", "buy"):
        score += 0.07
    if info.is_bundle:
        score -= 0.15  # 일괄 판매는 단일 카드 시세로 쓰기 어렵다
    if info.quantity > 1:
        score -= 0.05
    return round(max(0.0, min(1.0, score)), 2)
