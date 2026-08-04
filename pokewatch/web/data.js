'use strict';

/* 매물 목록을 걸러내고 카드별 시세로 묶는다.
 *
 * 파이썬 서버가 있든(로컬 앱) 없든(정적 호스팅) 화면이 똑같이 동작하도록,
 * 필터링과 집계를 여기 한 곳에서만 한다. 서버는 스냅샷을 내려주기만 한다.
 * 파이썬 쪽 stats.py 는 터미널 `top` 명령이 쓰고, 규칙은 서로 같다
 * (tools/check_parity.py 로 두 결과가 일치하는지 확인한다). */

const Pokewatch = (() => {
  const DAY = 86400;

  // ── 불러오기 ──────────────────────────────────────────────────────────────
  async function loadSnapshot() {
    const url = window.POKEWATCH_MODE === 'static' ? 'data/snapshot.json' : '/api/snapshot';
    const res = await fetch(url, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`스냅샷을 불러오지 못했습니다 (HTTP ${res.status})`);
    const snap = await res.json();
    snap.offline = res.headers.get('X-Pokewatch-Offline') === '1';
    // 최신 글이 위로 오게 한 번만 정렬해 둔다.
    snap.listings.sort((a, b) => (b.written_at || 0) - (a.written_at || 0));
    return snap;
  }

  // ── 필터 ─────────────────────────────────────────────────────────────────
  function filterListings(listings, f) {
    const q = f.q ? f.q.trim().toLowerCase() : null;
    const since = f.days ? Math.floor(Date.now() / 1000) - f.days * DAY : null;

    return listings.filter((l) => {
      if (q) {
        const hay = `${l.card_name || ''} ${l.card_name_en || ''} ${l.subject || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (f.trade && l.trade_type !== f.trade) return false;
      if (f.lang && l.language !== f.lang) return false;
      if (f.rarity && l.rarity !== f.rarity) return false;
      if (f.condition && l.condition !== f.condition) return false;
      if (f.graded && !l.grade_company) return false;
      if (f.named && !l.card_name) return false;
      if (f.nobundle && l.is_bundle) return false;
      if (f.priced && l.price == null) return false;
      if (f.minPrice != null && !(l.price >= f.minPrice)) return false;
      if (f.maxPrice != null && !(l.price <= f.maxPrice)) return false;
      if (since && (l.written_at || 0) < since) return false;
      if (f.minConfidence != null && (l.confidence || 0) < f.minConfidence) return false;
      return true;
    });
  }

  // ── 같은 카드로 묶기 ──────────────────────────────────────────────────────
  const baseKey = (cardKey) => cardKey.slice(0, cardKey.lastIndexOf('|'));

  /* 언어를 안 적은 글은, 그 카드의 언어가 한 가지뿐일 때만 그쪽으로 합친다.
     한글·일판이 섞여 있으면 어느 쪽인지 알 수 없으므로 '미표기'로 남긴다. */
  function languageMap(listings) {
    const known = new Map();
    for (const l of listings) {
      const lang = l.language || 'UNK';
      if (lang === 'UNK') continue;
      const base = baseKey(l.card_key);
      if (!known.has(base)) known.set(base, new Set());
      known.get(base).add(lang);
    }
    const map = new Map();
    for (const [base, langs] of known) {
      if (langs.size === 1) map.set(base, [...langs][0]);
    }
    return map;
  }

  function resolveGroup(listing, map) {
    if ((listing.language || 'UNK') !== 'UNK') return listing.card_key;
    const base = baseKey(listing.card_key);
    const folded = map.get(base);
    return folded ? `${base}|${folded}` : listing.card_key;
  }

  function aggregate(listings, labels, historyDays = 90) {
    const map = languageMap(listings);
    const groups = new Map();
    for (const l of listings) {
      const key = resolveGroup(l, map);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(l);
    }
    const cards = [];
    for (const [key, items] of groups) cards.push(summarize(key, items, labels, historyDays));
    cards.sort((a, b) => (b.listing_count - a.listing_count)
      || ((b.median_price || 0) - (a.median_price || 0)));
    return cards;
  }

  function selectCard(listings, cardKey, labels) {
    const cards = aggregate(listings, labels, 365);
    if (!cards.length) return [null, []];
    const card = cards.find((c) => c.card_key === cardKey) || cards[0];
    const map = languageMap(listings);
    const rows = listings.filter((l) => resolveGroup(l, map) === card.card_key);
    return [card, rows];
  }

  // ── 카드 하나의 시세 요약 ─────────────────────────────────────────────────
  function summarize(cardKey, items, labels, historyDays) {
    items = items.slice().sort((a, b) => (b.written_at || 0) - (a.written_at || 0));
    const newest = items[0];
    const language = cardKey.slice(cardKey.lastIndexOf('|') + 1) || 'UNK';

    const priced = items.filter((l) => l.price != null);
    // 시세는 판매글의 단일 카드 가격만 쓴다. 구매 희망가·일괄가는 왜곡이 크다.
    const salePrices = priced
      .filter((l) => l.trade_type === 'sell' && !l.is_bundle && !l.is_per_unit)
      .map((l) => l.price);
    const pool = salePrices.length ? salePrices : priced.map((l) => l.price);

    const rarity = newest.rarity || null;
    const rarityLabel = rarity ? (labels.rarity[rarity] || rarity) : null;
    const name = newest.card_name || newest.display_name || cardKey;

    const card = {
      card_key: cardKey,
      display_name: [name, rarityLabel].filter(Boolean).join(' '),
      card_name: newest.card_name,
      card_name_en: newest.card_name_en,
      dex: newest.dex,
      kind: newest.kind,
      rarity,
      rarity_label: rarityLabel,
      language,
      language_label: labels.language[language] || '미표기',
      set_code: mostCommon(items, 'set_code'),
      card_no: mostCommon(items, 'card_no'),
      thumbnail: (items.find((l) => l.thumbnail) || {}).thumbnail || null,
      listing_count: items.length,
      priced_count: priced.length,
      identified: !!newest.card_name,
      last_seen: newest.written_at || 0,
      url: newest.url,
      grades: [...new Set(items.filter((l) => l.grade_company)
        .map((l) => `${l.grade_company} ${l.grade_score}`))].sort(),
      trade_mix: mix(items, 'trade_type', labels.trade),
      condition_mix: mix(items, 'condition', labels.condition),
    };

    if (pool.length) {
      const sellFirst = priced.find((l) => l.trade_type === 'sell');
      Object.assign(card, {
        min_price: Math.min(...pool),
        max_price: Math.max(...pool),
        median_price: Math.round(median(pool)),
        avg_price: Math.round(pool.reduce((s, v) => s + v, 0) / pool.length),
        latest_price: sellFirst ? sellFirst.price : priced[0].price,
      });
    } else {
      Object.assign(card, {
        min_price: null, max_price: null, median_price: null, avg_price: null, latest_price: null,
      });
    }

    card.trend = trend(priced);
    card.history = history(priced, historyDays);
    return card;
  }

  /* 최근 절반과 이전 절반의 중앙값을 비교한 변동률 */
  function trend(priced) {
    const sale = priced
      .filter((l) => l.trade_type === 'sell' && !l.is_bundle)
      .sort((a, b) => (a.written_at || 0) - (b.written_at || 0));
    if (sale.length < 4) return { direction: 'flat', percent: null, basis: sale.length };

    const mid = Math.floor(sale.length / 2);
    const oldV = median(sale.slice(0, mid).map((l) => l.price));
    const newV = median(sale.slice(mid).map((l) => l.price));
    if (!oldV) return { direction: 'flat', percent: null, basis: sale.length };

    const pct = Math.round(((newV - oldV) / oldV) * 1000) / 10;
    const direction = pct > 3 ? 'up' : pct < -3 ? 'down' : 'flat';
    return { direction, percent: pct, basis: sale.length };
  }

  /* 일자별 중앙값 시계열 (그래프용) */
  function history(priced, days) {
    const cutoff = Math.floor(Date.now() / 1000) - days * DAY;
    const buckets = new Map();
    for (const l of priced) {
      const ts = l.written_at || 0;
      if (ts < cutoff || l.price == null || l.trade_type === 'buy') continue;
      const day = ts - (ts % DAY);
      if (!buckets.has(day)) buckets.set(day, []);
      buckets.get(day).push(l.price);
    }
    return [...buckets.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([date, vals]) => ({ date, price: Math.round(median(vals)), count: vals.length }));
  }

  function overview(listings, cards) {
    const prices = listings.filter((l) => l.price != null).map((l) => l.price);
    return {
      listings: listings.length,
      priced: prices.length,
      cards: cards.length,
      median_price: prices.length ? Math.round(median(prices)) : null,
      total_value: prices.reduce((s, v) => s + v, 0),
      newest: listings.reduce((m, l) => Math.max(m, l.written_at || 0), 0),
    };
  }

  function facets(listings, labels) {
    const out = {};
    for (const [field, key] of [['rarity', 'rarity'], ['language', 'language'],
      ['trade_type', 'trade'], ['condition', 'condition']]) {
      const tally = new Map();
      for (const l of listings) {
        const v = l[field];
        if (!v) continue;
        tally.set(v, (tally.get(v) || 0) + 1);
      }
      out[field] = [...tally.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([value, count]) => ({ value, count, label: labels[key][value] || value }));
    }
    return out;
  }

  // ── 작은 도구들 ──────────────────────────────────────────────────────────
  function median(values) {
    const s = values.slice().sort((a, b) => a - b);
    const m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  function mostCommon(items, field) {
    const tally = new Map();
    for (const l of items) {
      if (!l[field]) continue;
      tally.set(l[field], (tally.get(l[field]) || 0) + 1);
    }
    let best = null, bestN = 0;
    for (const [v, n] of tally) if (n > bestN) { best = v; bestN = n; }
    return best;
  }

  function mix(items, field, labels) {
    const tally = new Map();
    for (const l of items) {
      const k = l[field] || 'UNK';
      tally.set(k, (tally.get(k) || 0) + 1);
    }
    return [...tally.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([key, count]) => ({ key, label: labels[key] || key, count }));
  }

  return { loadSnapshot, filterListings, aggregate, selectCard, overview, facets, median };
})();

// 파이썬과 결과가 같은지 비교하는 스크립트에서도 쓸 수 있게 한다.
if (typeof module !== 'undefined' && module.exports) module.exports = Pokewatch;
