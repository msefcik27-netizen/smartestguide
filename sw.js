// SmartestGuide Service Worker
const CACHE = 'sg-v1';
const OFFLINE_URLS = ['/guest'];

self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(clients.claim());
});

self.addEventListener('fetch', e => {
  // Cache only static assets, not API calls
  if(e.request.url.includes('/api/')) return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
