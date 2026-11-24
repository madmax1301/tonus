import { immutable, assets } from '$app/manifest';
import { version } from '$app/env';
import { self as sw } from '$app/service-worker';

const CACHE = `tonus-${version}`;
// Die Manifest-Pfade sind relativ zum Base-Pfad; der Service Worker liegt
// direkt unter dem Base-Pfad, also ergibt das Auflösen gegen seine eigene
// URL die absoluten Pfade (wie früher `build`/`files` aus $service-worker).
const PRECACHE = [...immutable, ...assets].map(({ path }) => new URL(path, sw.location.href).pathname);

sw.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)));
});

sw.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      for (const key of await caches.keys()) {
        if (key !== CACHE) await caches.delete(key);
      }
      await sw.clients.claim();
    })()
  );
});

sw.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== sw.location.origin) return;

  // Niemals cachen: Queue-Status und Job-Progress müssen live sein.
  // Ein gecachter Fortschrittsbalken wäre schlimmer als gar keiner.
  if (url.pathname.includes('/api/')) return;

  // Gehashte Build-Assets sind immutable — Cache First.
  if (PRECACHE.includes(url.pathname)) {
    event.respondWith(caches.match(req).then((hit) => hit ?? fetch(req)));
    return;
  }

  // Alles andere: Netz zuerst, Cache als Fallback wenn offline.
  event.respondWith(
    (async () => {
      try {
        const res = await fetch(req);
        if (res.ok) (await caches.open(CACHE)).put(req, res.clone());
        return res;
      } catch {
        const hit = await caches.match(req);
        if (hit) return hit;
        throw new Error('offline and not cached');
      }
    })()
  );
});

sw.addEventListener('message', (event) => {
  if (event.data?.type === 'SKIP_WAITING') sw.skipWaiting();
});
