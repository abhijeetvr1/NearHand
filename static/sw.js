/* NearHand service worker: shows phone notifications for new messages and updates, even when the app is closed. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = {title: 'NearHand', body: e.data ? e.data.text() : ''}; }
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    if (wins.some(w => w.visibilityState === 'visible' && w.focused)) { wins.forEach(w => w.postMessage({type: 'push', data: d})); return; }  // the open page already shows it
    await self.registration.showNotification(d.title || 'NearHand', {body: d.body || '', icon: '/static/images/icons/icon-192.png', badge: '/static/images/icons/icon-192.png',
      tag: d.tag || 'nearhand', renotify: true, vibrate: [90, 40, 90], data: {url: d.url || '/'}});
  })());
});
self.addEventListener('notificationclick', e => {
  e.notification.close(); const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const w of wins) { await w.focus(); try { await w.navigate(url); } catch (_) {} return; }
    await self.clients.openWindow(url);
  })());
});
