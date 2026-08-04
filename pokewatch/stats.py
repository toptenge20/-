"""매물 목록을 카드 단위 시세로 묶는다."""

from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict

from .parsing import CONDITION_LABELS, LANGUAGE_LABELS, RARITY_LABELS, TRADE_LABELS

DAY = 86_400


def aggregate_cards(rows: list[dict], history_days: int = 90) -> list[dict]:
    """같은 카드끼리 묶어 카드별 시세 요약을 만든다."""
    mapping = _language_map(rows)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[resolve_group(r, mapping)].append(r)

    cards = [_summarize(key, items, history_days) for key, items in grouped.items()]
    cards.sort(key=lambda c: (-c["listing_count"], -(c["median_price"] or 0)))
    return cards


def select_card(rows: list[dict], card_key: str, history_days: int = 365):
    """넓게 가져온 매물에서 특정 카드 그룹 하나와 그 매물만 골라낸다."""
    cards = aggregate_cards(rows, history_days=history_days)
    if not cards:
        return None, []
    card = next((c for c in cards if c["card_key"] == card_key), cards[0])
    mapping = _language_map(rows)
    listings = [r for r in rows if resolve_group(r, mapping) == card["card_key"]]
    return card, listings


def _base_key(card_key: str) -> str:
    """'리자몽|SAR|KO' → '리자몽|SAR' (언어를 뗀 부분)."""
    return card_key.rsplit("|", 1)[0]


def _language_map(rows: list[dict]) -> dict[str, str]:
    """언어가 안 적힌 글을, 같은 카드의 언어가 하나뿐일 때 그쪽으로 붙여 준다.

    '리자몽 SAR 18만' 처럼 언어 표기를 생략한 글이 흔해서, 그대로 두면 같은
    카드가 '한글'과 '미표기'로 쪼개진다. 다만 한글·일판이 둘 다 있는 카드라면
    어느 쪽인지 알 수 없으므로 '미표기'로 남겨 둔다.
    """
    known: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        lang = r.get("language") or "UNK"
        if lang != "UNK":
            known[_base_key(r["card_key"])][lang] += 1
    return {base: next(iter(c)) for base, c in known.items() if len(c) == 1}


def resolve_group(row: dict, mapping: dict[str, str]) -> str:
    """이 매물이 속할 카드 그룹 키."""
    key = row["card_key"]
    if (row.get("language") or "UNK") != "UNK":
        return key
    base = _base_key(key)
    folded = mapping.get(base)
    return f"{base}|{folded}" if folded else key


def _summarize(card_key: str, items: list[dict], history_days: int) -> dict:
    items = sorted(items, key=lambda r: r["written_at"] or 0, reverse=True)
    newest = items[0]
    language = card_key.rsplit("|", 1)[-1] or "UNK"

    # 시세 계산에는 판매글 중 단일 카드 가격만 쓴다. 구매 희망가·일괄가는 왜곡이 크다.
    priced = [r for r in items if r.get("price")]
    sale_prices = [
        r["price"] for r in priced
        if r.get("trade_type") == "sell" and not r.get("is_bundle") and not r.get("is_per_unit")
    ]
    pool = sale_prices or [r["price"] for r in priced]

    thumbnail = next((r["thumbnail"] for r in items if r.get("thumbnail")), None)

    rarity = newest.get("rarity")
    rarity_label = RARITY_LABELS.get(rarity or "")
    name = newest.get("card_name") or newest.get("display_name") or card_key
    display = " ".join(p for p in (name, rarity_label) if p)

    summary = {
        "card_key": card_key,
        "display_name": display,
        "card_name": newest.get("card_name"),
        "card_name_en": newest.get("card_name_en"),
        "dex": newest.get("dex"),
        "kind": newest.get("kind"),
        "rarity": rarity,
        "rarity_label": rarity_label,
        "language": language,
        "language_label": LANGUAGE_LABELS.get(language, "미표기"),
        # 세트/번호는 일부 글에만 적혀 있으므로 가장 많이 언급된 값을 대표로 쓴다.
        "set_code": _most_common(items, "set_code"),
        "card_no": _most_common(items, "card_no"),
        "thumbnail": thumbnail,
        "listing_count": len(items),
        "priced_count": len(priced),
        "identified": bool(newest.get("card_name")),
        "last_seen": newest.get("written_at") or 0,
        "url": newest.get("url"),
    }

    if pool:
        summary.update(
            {
                "min_price": min(pool),
                "max_price": max(pool),
                "median_price": int(statistics.median(pool)),
                "avg_price": int(statistics.fmean(pool)),
                "latest_price": next(
                    (r["price"] for r in priced if r.get("trade_type") == "sell"), priced[0]["price"]
                ),
            }
        )
    else:
        summary.update(
            {"min_price": None, "max_price": None, "median_price": None,
             "avg_price": None, "latest_price": None}
        )

    summary["trend"] = _trend(priced)
    summary["history"] = _history(priced, history_days)
    summary["grades"] = sorted({
        f"{r['grade_company']} {r['grade_score']}" for r in items if r.get("grade_company")
    })
    summary["trade_mix"] = _counts(items, "trade_type", TRADE_LABELS)
    summary["condition_mix"] = _counts(items, "condition", CONDITION_LABELS)
    return summary


def _most_common(items: list[dict], field: str) -> str | None:
    tally = Counter(r[field] for r in items if r.get(field))
    return tally.most_common(1)[0][0] if tally else None


def _counts(items: list[dict], field: str, labels: dict) -> list[dict]:
    tally: dict[str, int] = defaultdict(int)
    for r in items:
        tally[r.get(field) or "UNK"] += 1
    return [
        {"key": k, "label": labels.get(k, k), "count": v}
        for k, v in sorted(tally.items(), key=lambda kv: -kv[1])
    ]


def _trend(priced: list[dict]) -> dict:
    """최근 절반 구간과 이전 절반 구간의 중앙값을 비교한 변동률."""
    sale = [r for r in priced if r.get("trade_type") == "sell" and not r.get("is_bundle")]
    sale = sorted(sale, key=lambda r: r["written_at"] or 0)
    if len(sale) < 4:
        return {"direction": "flat", "percent": None, "basis": len(sale)}

    mid = len(sale) // 2
    old = statistics.median([r["price"] for r in sale[:mid]])
    new = statistics.median([r["price"] for r in sale[mid:]])
    if not old:
        return {"direction": "flat", "percent": None, "basis": len(sale)}

    pct = round((new - old) / old * 100, 1)
    direction = "up" if pct > 3 else "down" if pct < -3 else "flat"
    return {"direction": direction, "percent": pct, "basis": len(sale)}


def _history(priced: list[dict], days: int) -> list[dict]:
    """일자별 중앙값 시계열 (스파크라인용)."""
    cutoff = int(time.time()) - days * DAY
    buckets: dict[int, list[int]] = defaultdict(list)
    for r in priced:
        ts = r.get("written_at") or 0
        if ts < cutoff or not r.get("price"):
            continue
        if r.get("trade_type") == "buy":
            continue
        buckets[ts - (ts % DAY)].append(r["price"])

    return [
        {"date": day, "price": int(statistics.median(vals)), "count": len(vals)}
        for day, vals in sorted(buckets.items())
    ]


def overview(rows: list[dict], cards: list[dict]) -> dict:
    prices = [r["price"] for r in rows if r.get("price")]
    identified = sum(1 for r in rows if r.get("card_name"))
    return {
        "listings": len(rows),
        "priced": len(prices),
        "cards": len(cards),
        "identified_rate": round(identified / len(rows) * 100) if rows else 0,
        "median_price": int(statistics.median(prices)) if prices else None,
        "total_value": sum(prices),
        "newest": max((r.get("written_at") or 0 for r in rows), default=0),
    }
