/* مسموع — عامل الخدمة (Service Worker)
   يوفّر عمل التطبيق دون اتصال بالإنترنت ويدير التحديثات. */

const VERSION = 'masmoo-v3.0.0';
const CORE = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/favicon.png'
];

// الخطوط تُخزَّن عند أول استخدام حتى يعمل التطبيق لاحقاً دون شبكة
const FONT_HOSTS = ['fonts.googleapis.com', 'fonts.gstatic.com'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(VERSION)
      .then(c => c.addAll(CORE))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', e => {
  if (e.data === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // نداءات الـ API: الشبكة أولاً، ولا تُخزَّن
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(req).catch(() => new Response(
      JSON.stringify({ offline: true }),
      { status: 503, headers: { 'Content-Type': 'application/json' } }
    )));
    return;
  }

  // الخطوط: المخزَّن أولاً ثم الشبكة
  if (FONT_HOSTS.includes(url.hostname)) {
    e.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(res => {
        const copy = res.clone();
        caches.open(VERSION).then(c => c.put(req, copy));
        return res;
      }).catch(() => hit))
    );
    return;
  }

  // ملفات التطبيق: المخزَّن أولاً مع تحديث في الخلفية
  e.respondWith(
    caches.match(req).then(hit => {
      const net = fetch(req).then(res => {
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(VERSION).then(c => c.put(req, copy));
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});

// مزامنة البلاغات المؤجَّلة عند عودة الشبكة
self.addEventListener('sync', e => {
  if (e.tag === 'masmoo-sync') {
    e.waitUntil(self.clients.matchAll().then(cs => cs.forEach(c => c.postMessage('SYNC_NOW'))));
  }
});


/* استقبال إشعار البلاغ الجديد ودفعه إلى شاشة الضابط */
self.addEventListener('push', e => {
  let d = { title: 'مسموع — بلاغ جديد', body: 'وصل بلاغ جديد' };
  try { if (e.data) d = Object.assign(d, e.data.json()); } catch (_) {}
  e.waitUntil(
    self.registration.showNotification(d.title, {
      body: d.body,
      icon: 'icons/icon-192.png',
      badge: 'icons/icon-96.png',
      tag: d.tag || 'masmoo-report',
      renotify: true,
      dir: 'rtl',
      lang: 'ar',
      vibrate: [200, 100, 200],
      requireInteraction: d.urgent === true,
      data: { num: d.num || '' }
    }).then(() => self.clients.matchAll().then(cs => cs.forEach(c => c.postMessage('NEW_REPORT'))))
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    for (const c of cs) if ('focus' in c) return c.focus();
    return self.clients.openWindow('./index.html');
  }));
});
