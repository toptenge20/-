"""파이썬(stats.py)과 자바스크립트(web/data.js)의 집계 결과가 같은지 확인한다.

터미널 `top` 명령은 파이썬으로, 대시보드 화면은 자바스크립트로 집계한다.
같은 규칙을 두 언어로 적어 둔 셈이라 한쪽만 고치면 조용히 어긋난다.
이 스크립트는 같은 데이터에 두 구현을 돌려 결과를 맞춰 본다.

    python3 tools/check_parity.py        # node 가 있어야 한다

node 가 없으면 건너뛴다(테스트를 실패시키지 않는다).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pokewatch import db, stats  # noqa: E402
from pokewatch.demo import generate  # noqa: E402
from pokewatch.export import build_snapshot  # noqa: E402

# 비교할 항목. history/trade_mix 등 중첩 구조도 그대로 비교한다.
FIELDS = [
    "card_key", "display_name", "card_name", "rarity", "language", "language_label",
    "set_code", "card_no", "listing_count", "priced_count",
    "min_price", "max_price", "median_price", "avg_price", "latest_price",
    "last_seen", "grades", "trend", "history", "trade_mix", "condition_mix",
]

JS_DRIVER = """
const Pokewatch = require(process.argv[2]);
const snap = JSON.parse(require('fs').readFileSync(process.argv[3], 'utf8'));
const rows = Pokewatch.filterListings(snap.listings, JSON.parse(process.argv[4]));
const cards = Pokewatch.aggregate(rows, snap.labels);
process.stdout.write(JSON.stringify({
  overview: Pokewatch.overview(rows, cards),
  cards,
}));
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("  node 가 없어 건너뜁니다.")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = db.connect(tmp / "parity.db")
        generate(conn, count=600, seed=11)

        snapshot = build_snapshot(conn)
        (tmp / "snap.json").write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        (tmp / "driver.js").write_text(JS_DRIVER, encoding="utf-8")

        # 여러 필터 조합에서 비교한다.
        cases = [
            {},
            {"named": True},
            {"named": True, "nobundle": True},
            {"named": True, "trade": "sell"},
            {"named": True, "lang": "JP"},
            {"named": True, "rarity": "SAR", "nobundle": True},
            {"named": True, "priced": True, "graded": True},
            {"q": "리자몽"},
        ]

        failures = 0
        for case in cases:
            py_rows = _py_filter(db.fetch_listings(conn), case)
            py_cards = stats.aggregate_cards(py_rows)
            py = {c["card_key"]: c for c in py_cards}

            out = subprocess.run(
                [node, str(tmp / "driver.js"), str(ROOT / "pokewatch" / "web" / "data.js"),
                 str(tmp / "snap.json"), json.dumps(case)],
                capture_output=True, text=True, check=True,
            )
            js_result = json.loads(out.stdout)
            js = {c["card_key"]: c for c in js_result["cards"]}

            label = json.dumps(case, ensure_ascii=False) or "{}"
            if set(py) != set(js):
                only_py, only_js = set(py) - set(js), set(js) - set(py)
                print(f"  ✗ {label}: 카드 집합 불일치 (py만 {sorted(only_py)[:3]}, js만 {sorted(only_js)[:3]})")
                failures += 1
                continue

            diffs = []
            for key in py:
                for field in FIELDS:
                    a, b = py[key].get(field), js[key].get(field)
                    if not _same(a, b):
                        diffs.append(f"{key}.{field}: py={a!r} js={b!r}")
            if diffs:
                print(f"  ✗ {label}: {len(diffs)}개 항목 불일치")
                for d in diffs[:5]:
                    print(f"      {d}")
                failures += 1
            else:
                print(f"  ✓ {label}  카드 {len(py)}종 일치")

        conn.close()

    if failures:
        print(f"\n  {failures}개 조합이 어긋납니다. stats.py 와 web/data.js 를 맞추세요.")
        return 1
    print("\n  파이썬과 자바스크립트 집계 결과가 모두 같습니다.")
    return 0


def _py_filter(rows: list[dict], case: dict) -> list[dict]:
    """data.js 의 filterListings 와 같은 조건을 파이썬 쪽에도 적용한다."""
    out = []
    q = (case.get("q") or "").lower()
    for r in rows:
        if q:
            hay = f"{r.get('card_name') or ''} {r.get('card_name_en') or ''} {r.get('subject') or ''}".lower()
            if q not in hay:
                continue
        if case.get("trade") and r.get("trade_type") != case["trade"]:
            continue
        if case.get("lang") and r.get("language") != case["lang"]:
            continue
        if case.get("rarity") and r.get("rarity") != case["rarity"]:
            continue
        if case.get("graded") and not r.get("grade_company"):
            continue
        if case.get("named") and not r.get("card_name"):
            continue
        if case.get("nobundle") and r.get("is_bundle"):
            continue
        if case.get("priced") and r.get("price") is None:
            continue
        out.append(r)
    return out


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return a == b
        return abs(a - b) < 1e-6
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_same(a[k], b[k]) for k in a)
    return a == b


if __name__ == "__main__":
    sys.exit(main())
