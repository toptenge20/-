"""포켓몬/서포트 카드 이름 사전과 이름 매칭."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "pokemon_ko.json"


@dataclass(frozen=True)
class NameHit:
    name_ko: str
    name_en: str | None
    dex: int | None
    kind: str  # "pokemon" | "trainer"
    start: int
    end: int


class Pokedex:
    """제목 안에서 포켓몬/서포트 이름을 찾아내는 사전.

    긴 이름이 짧은 이름을 포함하는 경우(예: '리자드' vs '리자몽', '피카츄' vs '피카')가
    많아 항상 최장 일치를 우선한다.
    """

    def __init__(self, data: dict):
        self._entries: dict[str, tuple[str, str | None, int | None, str]] = {}

        for ko, en, dex in data.get("pokemon", []):
            self._entries[ko] = (ko, en, dex, "pokemon")
        for row in data.get("trainers", []):
            ko, en = row[0], row[1]
            self._entries[ko] = (ko, en, None, "trainer")

        # 영문명으로도 찾을 수 있게 역방향 등록
        self._alias: dict[str, str] = {}
        for ko, (_, en, _, _) in list(self._entries.items()):
            if en:
                self._alias[_norm(en)] = ko
        for alias, target in data.get("aliases", {}).items():
            self._alias[_norm(alias)] = target

        # 최장 일치를 위해 길이 내림차순으로 정렬된 후보 목록
        keys = list(self._entries) + list(self._alias)
        keys.sort(key=len, reverse=True)
        self._pattern = re.compile(
            "|".join(re.escape(k) for k in keys if k), re.IGNORECASE
        )

    def find(self, text: str) -> list[NameHit]:
        """텍스트에서 발견된 카드 이름을 등장 순서대로 반환(중복 제거)."""
        hits: list[NameHit] = []
        seen: set[str] = set()
        for m in self._pattern.finditer(text):
            resolved = self._resolve(m.group(0))
            if resolved is None:
                continue
            ko, en, dex, kind = resolved
            if ko in seen:
                continue
            seen.add(ko)
            hits.append(NameHit(ko, en, dex, kind, m.start(), m.end()))
        return hits

    def best(self, text: str) -> NameHit | None:
        hits = self.find(text)
        if not hits:
            return None
        # 가장 앞에 나온 이름을 대표로 본다(제목은 보통 "[판매] 카드명 ..." 형태).
        return hits[0]

    def _resolve(self, token: str):
        if token in self._entries:
            return self._entries[token]
        target = self._alias.get(_norm(token))
        if target and target in self._entries:
            return self._entries[target]
        return None


def _norm(s: str) -> str:
    return re.sub(r"[\s._'-]+", "", s).lower()


@lru_cache(maxsize=1)
def load_pokedex(path: str | None = None) -> Pokedex:
    p = Path(path) if path else DATA_PATH
    with open(p, encoding="utf-8") as f:
        return Pokedex(json.load(f))
