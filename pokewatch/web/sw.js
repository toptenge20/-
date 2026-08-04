/* 서비스 워커: 앱 껍데기를 캐시해 두고, 서버가 꺼져 있어도 마지막 시세를 보여준다.
   CACHE 이름의 버전을 올리면 옛 캐시가 정리된다. */
'use strict';

const VERSION = 'pokewatch-v1';
const SHELL = `${VERSION}-shell`;
const DATA = `${VERSION}-data`;

const SHELL_FILES = [
  '/',
  '/static/styles.css',
  '/static/app.js',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL)
      // 아이콘 하나가 없다고 설치 전체가 실패하지 않도록 개별로 담는다.
      .then((cache) => Promise.all(SHELL_FILES.map((f) => cache.add(f).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // 시세 데이터: network-first. 서버가 살아 있으면 항상 최신을, 꺼져 있으면 캐시를 쓴다.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirst(request));
    return;
  }

  // 화면 이동: 오프라인이면 캐시된 첫 화면으로
  if (request.mode === 'navigate') {
    event.respondWith(networkFirst(request).catch(() => caches.match('/')));
    return;
  }

  // 정적 파일과 대체 카드 이미지: cache-first (뒤에서 조용히 갱신)
  event.respondWith(cacheFirst(request));
});

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      const cache = await caches.open(DATA);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) {
      // 화면에서 '오프라인' 표시를 띄울 수 있도록 헤더를 하나 붙여 준다.
      const headers = new Headers(cached.headers);
      headers.set('X-Pokewatch-Offline', '1');
      return new Response(await cached.blob(), {
        status: cached.status, statusText: cached.statusText, headers,
      });
    }
    throw err;
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) {
    fetch(request)
      .then((r) => r.ok && caches.open(SHELL).then((c) => c.put(request, r)))
      .catch(() => {});
    return cached;
  }
  const response = await fetch(request);
  if (response && response.ok) {
    const cache = await caches.open(SHELL);
    cache.put(request, response.clone());
  }
  return response;
}
