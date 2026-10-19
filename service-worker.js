/*
 * [4단계] PWA 오프라인 지원용 Service Worker
 *
 * 전략: "Cache First, then Network, and always re-cache what we fetch"
 * - 캐시에 있으면 그걸 바로 씀 (오프라인에서도 동작)
 * - 캐시에 없으면 네트워크로 받아오고, 받아온 건 다음을 위해 캐시에 저장
 *
 * 이 전략 덕분에 Pyodide가 받아오는 수십 개의 CDN 파일(core 런타임 + numpy/skyfield/
 * sgp4/jplephem 패키지 .whl 등)을 하나하나 목록으로 관리할 필요가 없습니다.
 * 사용자가 인터넷이 되는 상태에서 "최초 1회" 페이지를 정상적으로 사용하기만 하면,
 * 그 과정에서 요청된 모든 파일이 자동으로 캐시에 쌓이고, 그 다음부터는 오프라인에서도
 * 똑같이 동작합니다.
 *
 * ⚠️ 코드를 수정해서 배포한 뒤 사용자 브라우저의 캐시를 강제로 갱신하려면
 *    CACHE_NAME의 숫자를 올리면 됩니다 (예: v1 -> v2).
 */

const CACHE_NAME = 'leop-scheduler-v1';

const PRECACHE_URLS = [
  './',
  './index.html',
  './manifest.json',
  './icon.svg',
  './py/scheduler_full.py',
  './py/tab3_engine.py',
  './py/scheduler_core.py',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;

      return fetch(event.request)
        .then((networkResponse) => {
          if (networkResponse && (networkResponse.ok || networkResponse.type === 'opaque')) {
            const clone = networkResponse.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return networkResponse;
        })
        .catch(() => new Response('오프라인 상태이고, 이 파일은 아직 캐시되지 않았습니다.', {
          status: 503, statusText: 'Offline and not cached',
        }));
    })
  );
});
