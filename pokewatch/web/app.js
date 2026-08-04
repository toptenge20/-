'use strict';

const $ = (sel) => document.querySelector(sel);
const state = { view: 'grid', cards: [], loading: false, snapshot: null, offline: false };

// 정적 호스팅(핸드폰만으로 쓰기)에서는 파이썬 서버가 없다. 그때는 수집 버튼을 감춘다.
const IS_STATIC = window.POKEWATCH_MODE === 'static';

// ── 표기 도우미 ──────────────────────────────────────────────────────────────
function won(value) {
  if (value === null || value === undefined) return '가격 미기재';
  if (value >= 10000) {
    const man = value / 10000;
    const rounded = Math.abs(man - Math.round(man)) < 0.01 ? Math.round(man) : man.toFixed(1);
    return `${Number(rounded).toLocaleString('ko-KR')}만원`;
  }
  return `${value.toLocaleString('ko-KR')}원`;
}

function ago(ts) {
  if (!ts) return '-';
  const diff = Date.now() / 1000 - ts;
  if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))}분 전`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}일 전`;
  return new Date(ts * 1000).toLocaleDateString('ko-KR');
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// 글에 썸네일이 없을 때 대신 그려 줄 카드 이미지의 레어도별 색
const RARITY_COLORS = {
  SAR: ['#7c3aed', '#c4b5fd'], SR: ['#b45309', '#fcd34d'], AR: ['#0e7490', '#67e8f9'],
  UR: ['#a16207', '#fde68a'], HR: ['#be123c', '#fda4af'], CHR: ['#0f766e', '#5eead4'],
  CSR: ['#1d4ed8', '#93c5fd'], RR: ['#4338ca', '#a5b4fc'], SA: ['#9d174d', '#f9a8d4'],
  PROMO: ['#374151', '#d1d5db'], EX: ['#c2410c', '#fdba74'], GX: ['#1e40af', '#bfdbfe'],
  V: ['#155e75', '#a5f3fc'], VMAX: ['#86198f', '#f0abfc'], VSTAR: ['#3730a3', '#c7d2fe'],
  BOX: ['#166534', '#86efac'],
};
const DEFAULT_COLORS = ['#334155', '#cbd5e1'];

/* 대체 카드 이미지를 브라우저에서 직접 그린다.
   서버가 없는 정적 호스팅에서도, 오프라인에서도 그대로 보이게 하기 위함이다. */
function placeholderImage(name, rarity) {
  const [dark, light] = RARITY_COLORS[(rarity || '').toUpperCase()] || DEFAULT_COLORS;
  const title = esc(String(name || '?').slice(0, 14));
  const badge = esc((rarity || 'CARD').toUpperCase().slice(0, 6));
  const F = 'system-ui,-apple-system,sans-serif';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 336" width="240" height="336">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0%" stop-color="${dark}"/><stop offset="100%" stop-color="${light}"/>
</linearGradient></defs>
<rect width="240" height="336" rx="14" fill="url(#g)"/>
<rect x="12" y="12" width="216" height="312" rx="9" fill="none" stroke="rgba(255,255,255,.45)" stroke-width="2"/>
<circle cx="120" cy="132" r="52" fill="rgba(255,255,255,.22)"/>
<circle cx="120" cy="132" r="52" fill="none" stroke="rgba(255,255,255,.6)" stroke-width="3"/>
<path d="M68 132h104" stroke="rgba(255,255,255,.6)" stroke-width="3"/>
<circle cx="120" cy="132" r="16" fill="#fff"/>
<text x="120" y="232" text-anchor="middle" font-family="${F}" font-size="20" font-weight="700" fill="#fff">${title}</text>
<text x="120" y="262" text-anchor="middle" font-family="${F}" font-size="13" fill="rgba(255,255,255,.85)">${badge}</text>
<text x="120" y="300" text-anchor="middle" font-family="${F}" font-size="11" fill="rgba(255,255,255,.6)">이미지 없음</text>
</svg>`;
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}

function thumbUrl(card) {
  return card.thumbnail || placeholderImage(card.card_name || card.display_name, card.rarity);
}

function trendHtml(trend) {
  if (!trend || trend.percent === null) return '<span class="trend flat">─</span>';
  const arrow = { up: '▲', down: '▼', flat: '─' }[trend.direction];
  return `<span class="trend ${trend.direction}">${arrow} ${trend.percent > 0 ? '+' : ''}${trend.percent}%</span>`;
}

// ── 그래프 (외부 라이브러리 없이 SVG 로 직접 그린다) ─────────────────────────
// 색은 카드에 표시되는 추이 화살표와 같은 기준을 쓴다 (상승=빨강, 하락=초록).
const TREND_COLORS = { up: '#f87171', down: '#4ade80', flat: '#94a3b8' };

function sparkline(history, trend, width = 190, height = 32) {
  if (!history || history.length < 2) return '<svg class="spark"></svg>';
  const prices = history.map((h) => h.price);
  const min = Math.min(...prices), max = Math.max(...prices);
  const span = max - min || 1;
  const step = width / (history.length - 1);
  const pts = history.map((h, i) => [i * step, height - 3 - ((h.price - min) / span) * (height - 8)]);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const area = `${line} L${width},${height} L0,${height} Z`;
  const color = TREND_COLORS[(trend && trend.direction) || 'flat'];
  return `<svg class="spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    <path d="${area}" fill="${color}" opacity=".13"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="1.8"
          stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function priceChart(history) {
  if (!history || history.length < 2) {
    return '<p class="empty" style="padding:36px">시세 그래프를 그리기에 데이터가 부족합니다.</p>';
  }
  const W = 620, H = 170, padL = 58, padR = 14, padT = 16, padB = 26;
  const prices = history.map((h) => h.price);
  const min = Math.min(...prices), max = Math.max(...prices);
  const pad = (max - min) * 0.15 || max * 0.1 || 1;
  const lo = Math.max(0, min - pad), hi = max + pad;
  const x = (i) => padL + (i / (history.length - 1)) * (W - padL - padR);
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

  const line = history.map((h, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(h.price).toFixed(1)}`).join(' ');
  const area = `${line} L${x(history.length - 1).toFixed(1)},${H - padB} L${padL},${H - padB} Z`;

  const ticks = [lo, (lo + hi) / 2, hi].map((v) =>
    `<line x1="${padL}" x2="${W - padR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"
           stroke="currentColor" opacity=".12"/>
     <text x="${padL - 8}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end" font-size="10"
           fill="currentColor" opacity=".55">${won(Math.round(v))}</text>`).join('');

  const dots = history.map((h, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(h.price).toFixed(1)}" r="2.6" fill="#60a5fa">
       <title>${new Date(h.date * 1000).toLocaleDateString('ko-KR')} · ${won(h.price)} (${h.count}건)</title>
     </circle>`).join('');

  const first = new Date(history[0].date * 1000).toLocaleDateString('ko-KR');
  const last = new Date(history[history.length - 1].date * 1000).toLocaleDateString('ko-KR');

  return `<svg class="chart" viewBox="0 0 ${W} ${H}">
    ${ticks}
    <path d="${area}" fill="#60a5fa" opacity=".12"/>
    <path d="${line}" fill="none" stroke="#60a5fa" stroke-width="2" stroke-linejoin="round"/>
    ${dots}
    <text x="${padL}" y="${H - 8}" font-size="10" fill="currentColor" opacity=".55">${first}</text>
    <text x="${W - padR}" y="${H - 8}" font-size="10" text-anchor="end" fill="currentColor" opacity=".55">${last}</text>
  </svg>`;
}

// ── 데이터 로딩 ──────────────────────────────────────────────────────────────
function filters() {
  return {
    q: $('#q').value.trim(),
    trade: $('#trade').value,
    rarity: $('#rarity').value,
    lang: $('#lang').value,
    days: $('#days').value ? parseInt($('#days').value, 10) : null,
    graded: $('#graded').checked,
    named: $('#named').checked,
    nobundle: $('#nobundle').checked,
  };
}

/** 서버(또는 정적 파일)에서 매물 스냅샷을 한 번 받아 온다. */
async function loadData() {
  state.snapshot = await Pokewatch.loadSnapshot();
  setOffline(!!state.snapshot.offline);
}

/** 걸러내고 묶는 일은 전부 브라우저에서 한다. 그래서 필터가 즉시 반응한다. */
function refresh() {
  if (!state.snapshot) return;
  const snap = state.snapshot;
  const rows = Pokewatch.filterListings(snap.listings, filters());
  const cards = Pokewatch.aggregate(rows, snap.labels);

  const sort = $('#sort').value;
  cards.sort(sortComparator(sort));
  state.cards = cards.slice(0, 400);

  renderStats(Pokewatch.overview(rows, cards), snap);
  render();
}

function sortComparator(sort) {
  if (sort === 'price') return (a, b) => (b.median_price || 0) - (a.median_price || 0);
  if (sort === 'recent') return (a, b) => (b.last_seen || 0) - (a.last_seen || 0);
  if (sort === 'trend') return (a, b) => (b.trend.percent || 0) - (a.trend.percent || 0);
  if (sort === 'name') return (a, b) => a.display_name.localeCompare(b.display_name, 'ko');
  return (a, b) => b.listing_count - a.listing_count;
}

async function reload() {
  if (state.loading) return;
  state.loading = true;
  try {
    await loadData();
    refresh();
  } catch (e) {
    setOffline(true);
    toast('데이터를 불러오지 못했습니다: ' + e.message);
  } finally {
    state.loading = false;
  }
}

function setOffline(isOffline) {
  state.offline = isOffline;
  $('#offline-bar').hidden = !isOffline;
  if (!IS_STATIC) $('#collect-btn').disabled = isOffline;
}

function renderStats(o, snap) {
  const cells = [
    ['카드 종류', (o.cards || 0).toLocaleString('ko-KR'), '종'],
    ['매물 글', (o.listings || 0).toLocaleString('ko-KR'), '건'],
    ['가격 인식', `${o.priced || 0}`, `/ ${o.listings || 0}건`],
    ['전체 중앙값', won(o.median_price), ''],
    ['제목→카드 인식률', `${snap.totals.identified_rate || 0}`, '% (전체 기준)'],
    ['마지막 수집', ago(snap.last_collect_at), ''],
  ];
  $('#stats').innerHTML = cells.map(([k, v, s]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v">${esc(v)}${s ? `<small>${esc(s)}</small>` : ''}</div></div>`
  ).join('');

  const cafes = (snap.cafes || []).join(', ');
  $('#source-line').textContent = snap.is_demo
    ? '예제 데이터로 보고 있습니다 — config.json 을 채우고 “새로 수집”을 누르세요'
    : (cafes ? `수집 대상: ${cafes}` : '네이버 카페 장터 글을 모아 카드별 시세로 정리합니다');
  $('#foot-note').textContent =
    `카드 ${o.cards || 0}종 · 매물 ${o.listings || 0}건 · 제목에서 자동 추출한 값이라 실제 거래가와 다를 수 있습니다`
    + (IS_STATIC ? ` · ${ago(snap.generated_at)} 갱신` : '');
}

// ── 렌더링 ──────────────────────────────────────────────────────────────────
function render() {
  const box = $('#results');
  const empty = $('#empty');

  if (!state.cards.length) {
    box.innerHTML = '';
    empty.hidden = false;
    empty.textContent = '조건에 맞는 카드가 없습니다. 필터를 넓히거나 먼저 수집해 보세요.';
    return;
  }
  empty.hidden = true;
  box.className = state.view === 'grid' ? 'grid' : 'tablewrap';
  box.innerHTML = state.view === 'grid' ? state.cards.map(cardHtml).join('') : tableHtml(state.cards);

  box.querySelectorAll('[data-key]').forEach((el) => {
    el.addEventListener('click', () => openDetail(el.dataset.key));
  });
}

function cardHtml(c) {
  const badges = [];
  if (c.rarity) badges.push(`<span class="badge rarity">${esc(c.rarity_label || c.rarity)}</span>`);
  if (c.language && c.language !== 'UNK') badges.push(`<span class="badge lang">${esc(c.language_label)}</span>`);
  if (c.grades && c.grades.length) badges.push(`<span class="badge grade">${esc(c.grades[0])}</span>`);
  if (c.card_no) badges.push(`<span class="badge">${esc(c.card_no)}</span>`);

  const price = c.median_price !== null
    ? `<span class="price">${won(c.median_price)}</span>`
    : `<span class="price none">가격 미기재</span>`;
  const range = c.min_price !== null && c.min_price !== c.max_price
    ? `<div class="range">${won(c.min_price)} ~ ${won(c.max_price)} · 매물 ${c.listing_count}건</div>`
    : `<div class="range">매물 ${c.listing_count}건 · ${ago(c.last_seen)}</div>`;

  return `<article class="card" data-key="${esc(c.card_key)}">
    <img class="thumb" src="${esc(thumbUrl(c))}" alt="${esc(c.display_name)}" loading="lazy"
         onerror="this.onerror=null;this.src='${placeholderImage(c.card_name || c.display_name, c.rarity)}'">
    <div class="body">
      <div class="name">${esc(c.card_name || c.display_name)}
        ${c.card_name_en ? `<span class="en">${esc(c.card_name_en)}${c.dex ? ` · No.${c.dex}` : ''}</span>` : ''}
      </div>
      <div class="badges">${badges.join('')}</div>
      ${sparkline(c.history, c.trend)}
      <div class="price-row">${price}${trendHtml(c.trend)}</div>
      ${range}
    </div>
  </article>`;
}

function tableHtml(cards) {
  const rows = cards.map((c) => `<tr data-key="${esc(c.card_key)}">
    <td class="name">${esc(c.card_name || c.display_name)}</td>
    <td>${esc(c.rarity_label || c.rarity || '-')}</td>
    <td>${esc(c.language_label || '-')}</td>
    <td class="num">${won(c.median_price)}</td>
    <td class="num">${won(c.min_price)}</td>
    <td class="num">${won(c.max_price)}</td>
    <td class="num">${c.listing_count}</td>
    <td class="num">${trendHtml(c.trend)}</td>
    <td class="num">${ago(c.last_seen)}</td>
  </tr>`).join('');

  return `<table><thead><tr>
      <th>카드</th><th>레어도</th><th>언어</th><th style="text-align:right">중앙값</th>
      <th style="text-align:right">최저</th><th style="text-align:right">최고</th>
      <th style="text-align:right">매물</th><th style="text-align:right">추이</th>
      <th style="text-align:right">최근</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

// ── 상세 패널 ────────────────────────────────────────────────────────────────
function openDetail(key) {
  const drawer = $('#detail');
  drawer.hidden = false;

  // 상세는 기간 필터만 적용하고 나머지는 푼다. 그래야 시세 추이가 온전히 보인다.
  const snap = state.snapshot;
  const rows = Pokewatch.filterListings(snap.listings, { days: filters().days });
  const [card, listings] = Pokewatch.selectCard(rows, key, snap.labels);

  $('#detail-body').innerHTML = card
    ? detailHtml(card, listings)
    : '<p class="empty">해당 카드를 찾지 못했습니다.</p>';
}

function detailHtml(c, listings) {
  const kpis = [
    ['중앙값', won(c.median_price)],
    ['최저가', won(c.min_price)],
    ['최고가', won(c.max_price)],
    ['평균가', won(c.avg_price)],
    ['매물 수', `${c.listing_count}건`],
    ['최근 등록', ago(c.last_seen)],
  ];

  const mix = (c.trade_mix || []).map((m) => `<span class="badge">${esc(m.label)} ${m.count}</span>`).join(' ');
  const cond = (c.condition_mix || []).map((m) => `<span class="badge">${esc(m.label)} ${m.count}</span>`).join(' ');
  const grades = (c.grades || []).map((g) => `<span class="badge grade">${esc(g)}</span>`).join(' ');

  const rows = listings.slice(0, 60).map((l) => `
    <a class="listing" href="${esc(l.url || '#')}" target="_blank" rel="noopener">
      <span class="p">${won(l.price)}</span>
      <div class="t">${esc(l.subject)}</div>
      <div class="m">
        <span>${esc(tradeLabel(l.trade_type))}</span>
        <span>${esc(l.writer || '')}</span>
        <span>${ago(l.written_at)}</span>
        ${l.grade_company ? `<span>${esc(l.grade_company)} ${esc(l.grade_score)}</span>` : ''}
        ${l.shipping_included ? '<span>택포</span>' : ''}
        ${l.is_bundle ? '<span>일괄</span>' : ''}
        ${l.menu_name ? `<span>${esc(l.menu_name)}</span>` : ''}
      </div>
    </a>`).join('');

  return `
    <div class="detail-head">
      <img src="${esc(thumbUrl(c))}" alt="${esc(c.display_name)}">
      <div>
        <h2 id="detail-title">${esc(c.card_name || c.display_name)}</h2>
        <div class="badges">
          ${c.rarity ? `<span class="badge rarity">${esc(c.rarity_label || c.rarity)}</span>` : ''}
          ${c.language !== 'UNK' ? `<span class="badge lang">${esc(c.language_label)}</span>` : ''}
          ${c.card_no ? `<span class="badge">${esc(c.card_no)}</span>` : ''}
          ${c.set_code ? `<span class="badge">${esc(c.set_code)}</span>` : ''}
          ${grades}
        </div>
        <p class="sub" style="margin-top:8px">
          ${esc(c.card_name_en || '')}${c.dex ? ` · 전국도감 No.${c.dex}` : ''}
          ${trendHtml(c.trend)} ${c.trend.basis ? `<small>(판매글 ${c.trend.basis}건 기준)</small>` : ''}
        </p>
      </div>
    </div>

    <div class="kpis">
      ${kpis.map(([k, v]) => `<div class="kpi"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`).join('')}
    </div>

    <div class="section-title">시세 추이 (일자별 중앙값)</div>
    ${priceChart(c.history)}

    <div class="section-title">거래 유형 / 상태</div>
    <div class="badges">${mix} ${cond}</div>

    <div class="section-title">매물 ${listings.length}건</div>
    ${rows || '<p class="empty">매물이 없습니다.</p>'}
  `;
}

function tradeLabel(t) {
  return { sell: '판매', buy: '구매', trade: '교환', free: '나눔', info: '시세/문의' }[t] || '미분류';
}

// ── 기타 UI ─────────────────────────────────────────────────────────────────
function toast(msg) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function loadFacets() {
  if (!state.snapshot) return;
  const f = Pokewatch.facets(state.snapshot.listings, state.snapshot.labels);
  fill('#rarity', f.rarity, '레어도 전체');
  fill('#lang', f.language, '언어 전체');
}

function fill(sel, items, allLabel) {
  const el = $(sel);
  const current = el.value;
  el.innerHTML = `<option value="">${allLabel}</option>` +
    (items || []).map((i) => `<option value="${esc(i.value)}">${esc(i.label)} (${i.count})</option>`).join('');
  el.value = current;
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// ── 앱 설치 / 오프라인 ───────────────────────────────────────────────────────
function setupApp() {
  // 서비스 워커는 보안 컨텍스트에서만 등록된다. localhost 는 되지만 http://192.168.x.x
  // 처럼 LAN IP 로 접속하면 브라우저가 막는다. 그때는 그냥 일반 웹페이지로 동작한다.
  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => {
      // 상대 경로여야 한다. 하위 경로(예: .../저장소이름/)에 올려도 그 범위로 등록된다.
      navigator.serviceWorker.register('sw.js').catch((e) => {
        console.info('서비스 워커 등록 안 됨:', e.message);
      });
    });
  }

  // 안드로이드/데스크톱 크롬의 설치 배너를 우리 버튼으로 받는다.
  let deferred = null;
  const btn = $('#install-btn');
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferred = e;
    btn.hidden = false;
  });
  btn.addEventListener('click', async () => {
    if (!deferred) return;
    deferred.prompt();
    const { outcome } = await deferred.userChoice;
    deferred = null;
    btn.hidden = true;
    if (outcome === 'accepted') toast('홈화면에 설치했습니다.');
  });
  window.addEventListener('appinstalled', () => { btn.hidden = true; });

  window.addEventListener('online', () => { setOffline(false); reload(); });
  window.addEventListener('offline', () => setOffline(true));

  showIosInstallHint();
}

/* 아이폰 사파리는 beforeinstallprompt 를 지원하지 않는다.
   설치 버튼이 뜰 수 없으므로 '공유 → 홈 화면에 추가' 를 한 번 알려 준다. */
function showIosInstallHint() {
  const isIos = /iP(hone|ad|od)/.test(navigator.userAgent);
  const standalone = window.navigator.standalone === true
    || window.matchMedia('(display-mode: standalone)').matches;
  if (!isIos || standalone || localStorage.getItem('pokewatch-ios-hint') === 'done') return;

  const bar = $('#ios-hint');
  bar.hidden = false;
  $('#ios-hint-close').addEventListener('click', () => {
    bar.hidden = true;
    localStorage.setItem('pokewatch-ios-hint', 'done');
  });
}

// 홈화면 바로가기(manifest shortcuts)로 열면 ?sort=price 같은 값이 붙어 온다.
function applyUrlParams() {
  const q = new URLSearchParams(location.search);
  for (const id of ['sort', 'days', 'trade', 'rarity', 'lang', 'q']) {
    const v = q.get(id);
    if (v !== null && $('#' + id)) $('#' + id).value = v;
  }
}

function init() {
  const saved = localStorage.getItem('pokewatch-theme');
  if (saved) document.documentElement.dataset.theme = saved;

  $('#q').addEventListener('input', debounce(refresh, 180));
  ['trade', 'rarity', 'lang', 'days', 'sort', 'graded', 'named', 'nobundle']
    .forEach((id) => $('#' + id).addEventListener('change', refresh));

  document.querySelectorAll('.viewtoggle button').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.viewtoggle button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      state.view = b.dataset.view;
      render();
    });
  });

  $('#theme-btn').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('pokewatch-theme', next);
  });

  // 정적 호스팅에는 수집기가 없다 (클라우드가 대신 수집한다).
  if (IS_STATIC) {
    $('#collect-btn').hidden = true;
  } else {
    $('#collect-btn').addEventListener('click', async () => {
      $('#collect-btn').disabled = true;
      $('#collect-btn').textContent = '수집 중…';
      try {
        const r = await fetch('/api/collect', { method: 'POST' }).then((x) => x.json());
        if (r.status !== 'started') throw new Error('이미 수집이 진행 중입니다.');
        toast('수집을 시작했습니다. 끝나면 자동으로 갱신됩니다.');
        const timer = setInterval(async () => {
          const s = await fetch('/api/status').then((x) => x.json());
          if (s.collecting) return;
          clearInterval(timer);
          const errs = (s.last_report && s.last_report.errors) || [];
          toast(errs.length ? '수집 오류: ' + errs[0] : '수집이 끝났습니다.');
          $('#collect-btn').textContent = '새로 수집';
          $('#collect-btn').disabled = false;
          reload();
        }, 2500);
      } catch (e) {
        toast(e.message);
        $('#collect-btn').textContent = '새로 수집';
        $('#collect-btn').disabled = false;
      }
    });
  }

  document.querySelectorAll('[data-close]').forEach((el) =>
    el.addEventListener('click', () => { $('#detail').hidden = true; }));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $('#detail').hidden = true;
    if (e.key === '/' && document.activeElement !== $('#q')) { e.preventDefault(); $('#q').focus(); }
  });

  setupApp();
  applyUrlParams();

  loadData()
    .then(() => { loadFacets(); refresh(); })
    .catch((e) => {
      setOffline(true);
      $('#empty').hidden = false;
      $('#empty').textContent = '데이터를 불러오지 못했습니다: ' + e.message;
    });

  // 새 시세가 올라오면 따라 갱신되게 한다 (정적 모드에서는 새 스냅샷을 확인).
  setInterval(() => { if (!document.hidden && !state.offline) reload(); }, 300000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) reload(); });
}

init();
