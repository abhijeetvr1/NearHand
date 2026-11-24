(() => {
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const csrf = $('meta[name=csrf]')?.content;
const post = (url, body) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf}, body: JSON.stringify(body || {})});

// toasts
const box = $('#toasts');
window.toast = (t, kind = '') => { const d = document.createElement('div'); d.className = 'toast ' + kind; d.textContent = t; d.setAttribute('role', 'status'); box.append(d); setTimeout(() => d.remove(), 6000); };
$$('[data-flash]').forEach(f => toast(f.dataset.flash, f.dataset.kind === 'error' ? 'error' : ''));

// confirm dialogs: <form data-confirm="Delete this?">
const dlg = $('#confirm');
document.addEventListener('submit', e => {
  const f = e.target, m = f.dataset.confirm;
  if (!m || f.dataset.ok) return;
  e.preventDefault(); $('#confirm-msg').textContent = m; dlg.showModal();
  $('#confirm-yes').onclick = () => { f.dataset.ok = 1; dlg.close(); f.requestSubmit(); };
}, true);
$('#confirm-no')?.addEventListener('click', () => dlg.close());

// support panel
const sp = $('#support');
$('#fab')?.addEventListener('click', () => { sp.classList.toggle('open'); });
$('#ticket-form')?.addEventListener('submit', async e => {
  e.preventDefault(); const d = Object.fromEntries(new FormData(e.target));
  const r = await post('/support/ticket', d), j = await r.json();
  if (j.ok) { toast('Ticket sent. We\'ll reply by email.'); e.target.reset(); sp.classList.remove('open'); } else toast(j.error || 'Could not send.', 'error');
});

// location
async function setLoc() {
  if (!navigator.geolocation) return toast('Your browser can\'t share location.', 'error');
  navigator.geolocation.getCurrentPosition(async p => {
    await post('/api/location', {lat: p.coords.latitude, lng: p.coords.longitude});
    toast('Location on. Showing distance to each job.'); location.reload();
  }, () => toast('Location is off. You can still browse all work.', 'error'), {timeout: 10000});
}
$$('[data-loc-off]').forEach(b => b.addEventListener('click', async () => { await post('/api/location/clear'); location.reload(); }));

// live filters (and the quiet 5-second refresh of the marketplace grid)
const ff = $('#filters'), grid = $('#grid');
if (ff && grid) {
  let t, ctl, last = grid.innerHTML.trim();
  const run = async (quiet) => {
    if (quiet === true && ($('dialog[open]') || document.hidden)) return;
    const qs = new URLSearchParams(new FormData(ff)); [...qs].forEach(([k, v]) => { if (!v) qs.delete(k); });
    history.replaceState(null, '', '?' + qs); qs.set('partial', 1);
    ctl?.abort(); ctl = new AbortController();
    if (quiet !== true) grid.innerHTML = '<div class="cards"><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>';
    try {
      const html = (await (await fetch('/market?' + qs, {signal: ctl.signal, cache: 'no-store'})).text()).trim();
      if (quiet === true && html === last) return;                  // nothing new: leave the page exactly as it is
      last = html; grid.innerHTML = html; const cn = grid.querySelector('[data-count]'); if (cn && $('#count-n')) $('#count-n').textContent = cn.dataset.count;
      document.dispatchEvent(new Event('nh:refreshed'));
    } catch (e) {}
  };
  window.NHRefreshGrid = () => run(true);
  ff.addEventListener('input', () => { clearTimeout(t); t = setTimeout(run, 250); });
  ff.addEventListener('change', () => { clearTimeout(t); t = setTimeout(run, 50); });
  ff.addEventListener('submit', e => { e.preventDefault(); run(); });
}

// scroll reveal + count-up
const io = new IntersectionObserver(es => es.forEach(x => { if (x.isIntersecting) { x.target.classList.add('in'); io.unobserve(x.target); } }), {threshold: .15});
$$('.reveal').forEach(el => io.observe(el));
window.countUp = (el, to, pre = '') => { const t0 = performance.now(), d = 1200; const f = n => { const p = Math.min(1, (n - t0) / d); el.textContent = pre + Math.round(to * (1 - Math.pow(1 - p, 3))).toLocaleString('en-IN'); if (p < 1) requestAnimationFrame(f); }; requestAnimationFrame(f); };
// filter sheet: a native <dialog>, so it is always above everything and fully hidden when closed
const fsheet = $('#sheet');
$$('[data-sheet-open]').forEach(b => b.addEventListener('click', () => { if (fsheet && !fsheet.open) fsheet.showModal(); }));
$$('[data-sheet-close]').forEach(b => b.addEventListener('click', () => fsheet?.close()));
fsheet?.addEventListener('click', e => { if (e.target === fsheet) fsheet.close(); });   // tap on the dim area closes it
// password show/hide
$$('[data-pw]').forEach(b => b.addEventListener('click', () => { const i = b.parentElement.querySelector('input'); i.type = i.type === 'password' ? 'text' : 'password'; b.textContent = i.type === 'password' ? 'Show' : 'Hide'; }));
// wallet quick amounts
$$('[data-amt]').forEach(b => b.addEventListener('click', () => { const i = b.closest('form').querySelector('[name=amount]'); i.value = b.dataset.amt; i.focus(); }));

// custom radius, share, chime
$$('input[name=radius_choice]').forEach(r => r.addEventListener('change', () => { $('#custom-r').hidden = r.value !== 'custom' || !r.checked; }));

// ---------- sound: tiny WAV chimes played through <audio>, unlocked by the first tap so they also play in background tabs ----------
const soundOn = () => localStorage.getItem('nh-sound') !== 'off';
const paintSound = () => { document.documentElement.dataset.sound = soundOn() ? 'on' : 'off'; };
paintSound();
function wav(freqs, step = .13, vol = .45) {
  const sr = 22050, per = Math.floor(sr * step), len = Math.floor(sr * .3), n = per * (freqs.length - 1) + len, d = new Float32Array(n);
  freqs.forEach((f, k) => { for (let i = 0; i < len; i++) { const t = i / sr; d[per * k + i] += Math.sin(2 * Math.PI * f * t) * Math.min(1, t / .008) * Math.exp(-t * 11); } });
  const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf), w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); w(8, 'WAVEfmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, d[i] * vol)) * 32767, true);
  return URL.createObjectURL(new Blob([buf], {type: 'audio/wav'}));
}
const clips = {}; const clip = k => clips[k] || (clips[k] = new Audio(wav(k === 'msg' ? [880, 660] : [660, 880, 1100])));
let unlocked = false;
const unlock = () => { if (unlocked) return; unlocked = true; ['ping', 'msg'].forEach(k => { const a = clip(k); a.muted = true; a.play().then(() => { a.pause(); a.currentTime = 0; a.muted = false; }).catch(() => { a.muted = false; unlocked = false; }); }); };
['pointerdown', 'touchend', 'keydown', 'click'].forEach(ev => addEventListener(ev, unlock, {passive: true}));
window.nhSound = (kind = 'ping') => { if (!soundOn()) return; try { const a = clip(kind); a.currentTime = 0; a.volume = 1; a.play().catch(() => {}); } catch (e) {} if (navigator.vibrate) try { navigator.vibrate([90, 50, 90]); } catch (e) {} };
$$('[data-sound-toggle]').forEach(b => b.addEventListener('click', () => { localStorage.setItem('nh-sound', soundOn() ? 'off' : 'on'); paintSound(); if (soundOn()) nhSound('ping'); toast(soundOn() ? 'Sound on' : 'Sound off'); }));
const mute = $('#mute'); if (mute) { const paint = () => mute.textContent = soundOn() ? 'Sound on' : 'Sound off'; paint(); mute.onclick = () => { localStorage.setItem('nh-sound', soundOn() ? 'off' : 'on'); paintSound(); paint(); }; nhSound('ping'); }

// ---------- phone / browser notifications (service worker + Web Push) ----------
const nhPush = window.nhPush = {
  supported: () => 'serviceWorker' in navigator && 'Notification' in window && 'PushManager' in window && window.isSecureContext,
  reg: null,
  async init() { if (!('serviceWorker' in navigator) || !window.isSecureContext) return; try { this.reg = await navigator.serviceWorker.register('/sw.js'); } catch (e) {} },
  state() { return !this.supported() ? 'unsupported' : Notification.permission; },
  async enable() {
    if (!this.supported()) { toast('Notifications need HTTPS (or localhost) and a supported browser.', 'error'); return false; }
    const perm = await Notification.requestPermission(); if (perm !== 'granted') { toast('Notifications are blocked in your browser settings.', 'error'); return false; }
    try {
      const reg = this.reg || await navigator.serviceWorker.ready, {key} = await (await fetch('/push/key')).json(); if (!key) return false;
      const pad = '='.repeat((4 - key.length % 4) % 4), bin = atob((key + pad).replace(/-/g, '+').replace(/_/g, '/')), arr = Uint8Array.from(bin, c => c.charCodeAt(0));
      const sub = (await reg.pushManager.getSubscription()) || await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: arr});
      await post('/push/subscribe', sub.toJSON()); toast('Notifications are on.'); paintPush(); return true;
    } catch (e) { toast('Could not turn on notifications.', 'error'); return false; }
  },
  async show(title, body, tag, url) { try { const reg = this.reg || await navigator.serviceWorker.ready; await reg.showNotification(title, {body, tag, renotify: true, icon: '/static/images/icons/icon-192.png', badge: '/static/images/icons/icon-192.png', data: {url}, vibrate: [120, 60, 120]}); } catch (e) {} }
};
const paintPush = () => { const s = nhPush.state(); $$('[data-push-state]').forEach(e => { e.textContent = s === 'granted' ? 'Notifications on' : s === 'denied' ? 'Blocked in browser' : s === 'unsupported' ? 'Needs HTTPS' : 'Turn on notifications'; e.disabled = s === 'denied' || s === 'unsupported'; }); const bar = $('#pushbar'); if (bar) bar.hidden = s !== 'default' || localStorage.getItem('nh-push-skip') === '1'; };
if (B_AUTH()) { nhPush.init().then(paintPush); } function B_AUTH() { return document.body.dataset.auth === '1'; }
$$('[data-push-enable]').forEach(b => b.addEventListener('click', () => nhPush.enable()));
$('#pushbar-skip')?.addEventListener('click', () => { localStorage.setItem('nh-push-skip', '1'); $('#pushbar').hidden = true; });

// ---------- live updates: new jobs, notifications, chat, wallet. Keeps polling in background tabs (slower) ----------
const B = document.body.dataset; let nid = +B.nid || 0, mid = +B.mid || 0, oid = +B.oid || 0, busy = false; const baseTitle = document.title.replace(/^\(\d+\)\s*/, '');
const badge = (k, n) => $$(`[data-badge="${k}"]`).forEach(e => { e.textContent = n; e.hidden = !n; });
async function poll() {
  if (busy) return; busy = true;
  try {
    const r = await fetch(`/api/live?nid=${nid}&mid=${mid}&oid=${oid}`, {cache: 'no-store'}); if (!r.ok) return; const j = await r.json(); let sound = null; const bg = document.hidden;
    $$('[data-live=open]').forEach(e => e.textContent = j.orders.open); $$('[data-live=hour]').forEach(e => e.textContent = j.orders.hour);
    if (j.orders.new > 0 && B.page === 'market' && oid) { sound = 'ping'; toast(`${j.orders.new} new job${j.orders.new > 1 ? 's' : ''} posted`); window.NHRefreshGrid && NHRefreshGrid(); }
    oid = Math.max(oid, j.orders.max);
    if (B.auth === '1') {
      (j.notifs || []).forEach(n => { nid = Math.max(nid, n.id); if (!(n.url || '').startsWith('/chat')) { toast(n.text); sound = sound || 'ping'; if (bg) nhPush.show('NearHand', n.text, n.url || 'general', n.url || '/dashboard'); } });
      (j.msgs || []).forEach(m => { mid = Math.max(mid, m.id); if (window.NH_ACTIVE_CHAT !== m.user || bg) { if (window.NH_ACTIVE_CHAT !== m.user) toast(`${m.name}: ${m.body}`); sound = 'msg'; if (bg) nhPush.show(m.name, m.body, '/chat/' + m.user, '/chat/' + m.user); } });
      badge('n', j.unread_n); badge('m', j.unread_m);
      $$('[data-balance]').forEach(e => e.textContent = '₹' + (+j.balance).toLocaleString('en-IN'));
      const total = (j.unread_n || 0) + (j.unread_m || 0); document.title = (total ? `(${total}) ` : '') + baseTitle;
    }
    if (sound) nhSound(sound);
  } catch (e) {} finally { busy = false; }
}
let timer = null; const schedule = () => { clearInterval(timer); timer = setInterval(poll, document.hidden ? 15000 : 5000); };
schedule(); document.addEventListener('visibilitychange', () => { schedule(); if (!document.hidden) poll(); }); addEventListener('focus', poll); addEventListener('online', poll);

// ---------- animated tabs (Home posted/working, Wallet add/withdraw, Profile sections) ----------
(() => {
  const tabs = $$('[data-wtab]'); if (!tabs.length) return;
  const panes = $$('[data-wpane]'), bar = tabs[0].closest('[data-tabs]'); let cur = Math.max(0, tabs.findIndex(t => t.classList.contains('on')));
  const place = () => { if (!bar) return; bar.style.setProperty('--n', tabs.length); bar.style.setProperty('--i', cur); const ind = bar.querySelector('.ind'), t = tabs[cur]; if (ind && t) { ind.style.width = t.offsetWidth + 'px'; ind.style.transform = `translateX(${t.offsetLeft}px)`; } };
  const show = i => {
    if (i === cur || i < 0 || i >= tabs.length) return; const dir = i > cur ? 'r' : 'l'; cur = i;
    tabs.forEach((t, j) => { t.classList.toggle('on', j === i); t.setAttribute('aria-selected', j === i); });
    panes.forEach(p => { const on = p.dataset.wpane === tabs[i].dataset.wtab; p.hidden = !on; if (on) { p.dataset.dir = dir; p.style.animation = 'none'; void p.offsetHeight; p.style.animation = ''; } });
    place(); const u = tabs[i].getAttribute('href'); if (u && history.replaceState) history.replaceState(null, '', u);
    tabs[i].scrollIntoView({inline: 'center', block: 'nearest', behavior: 'smooth'});
  };
  tabs.forEach((t, i) => t.addEventListener('click', e => { e.preventDefault(); show(i); }));
  $$('[data-goto-tab]').forEach(b => b.addEventListener('click', e => { e.preventDefault(); show(tabs.findIndex(t => t.dataset.wtab === b.dataset.gotoTab)); scrollTo({top: 0, behavior: 'smooth'}); }));
  place(); addEventListener('resize', place); document.fonts?.ready.then(place);
  // swipe left/right between tabs (Home and Wallet only)
  const sw = $('[data-swipe]'); if (sw) { let x0 = null, y0 = null; sw.addEventListener('touchstart', e => { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; }, {passive: true});
    sw.addEventListener('touchend', e => { if (x0 == null) return; const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0; x0 = null; if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.6) show(cur + (dx < 0 ? 1 : -1)); }, {passive: true}); }
})();
$$('[data-limit]').forEach(i => { const c = $(i.dataset.limit), f = () => { c.textContent = `${i.value.length}/${i.maxLength} · ${i.value.trim() ? i.value.trim().split(/\s+/).length : 0}/10 words`; }; i.addEventListener('input', f); f(); });

// generic dialog openers + tap outside to close
document.addEventListener('click', e => {
  const o = e.target.closest('[data-open-dialog]'); if (o) { e.preventDefault(); $(o.dataset.openDialog)?.showModal(); return; }
  const c = e.target.closest('[data-close-dialog]'); if (c) { c.closest('dialog')?.close(); return; }
  const sh = e.target.closest('[data-share]'); if (sh) { (async () => { try { navigator.share ? await navigator.share({url: location.href, title: document.title}) : (await navigator.clipboard.writeText(location.href), toast('Link copied.')); } catch (_) {} })(); return; }
  const cp = e.target.closest('[data-copy]'); if (cp) { navigator.clipboard?.writeText(cp.dataset.copy).then(() => toast('Copied ' + cp.dataset.copy), () => toast(cp.dataset.copy)); }
});
$$('dialog').forEach(d => d.addEventListener('click', e => { const r = d.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close(); }));
// top-up dialog
const tu = $('#topup'); if (tu) { if (tu.dataset.open === '1') tu.showModal(); $('#topup-skip')?.addEventListener('click', () => { fetch('/orders/topup-skip', {method: 'POST', headers: {'X-CSRFToken': csrf}}); tu.close(); }); }



// photo viewer on job pages: tap a photo -> full screen. Swipe, arrow keys, Esc and the phone's Back button all work.
(() => {
  const lb = $('#lightbox'); if (!lb) return; const img = $('#lb-img'), cnt = $('#lb-count');
  let list = [], i = 0, from = null, pushed = false, x0 = 0, y0 = 0;
  const show = () => { const u = list[i].dataset.lightbox; img.alt = `Photo ${i + 1} of ${list.length}`; img.src = u; cnt.textContent = list.length > 1 ? `${i + 1} / ${list.length}` : ''; lb.classList.toggle('single', list.length < 2);
    [1, -1].forEach(d => { const n = list[(i + d + list.length) % list.length]; if (n) new Image().src = n.dataset.lightbox; }); };       // warm the neighbours so swiping is instant
  const step = d => { if (list.length < 2) return; i = (i + d + list.length) % list.length; show(); };
  const hide = () => { if (lb.hidden) return; lb.hidden = true; document.documentElement.classList.remove('lb-open'); img.removeAttribute('src'); from?.focus?.({preventScroll: true}); };
  const close = () => { if (pushed) { pushed = false; history.back(); } hide(); };
  const open = (g) => { list = [...g.closest('[data-gallery]').querySelectorAll('[data-lightbox]')]; i = Math.max(0, list.indexOf(g)); from = g; show(); lb.hidden = false; document.documentElement.classList.add('lb-open'); $('.lb-x', lb).focus({preventScroll: true});
    if (!pushed) { history.pushState({lb: 1}, ''); pushed = true; } };
  document.addEventListener('click', e => {
    const g = e.target.closest('[data-lightbox]'); if (g) { e.preventDefault(); open(g); return; }
    const a = e.target.closest('[data-lb]'); if (!a || lb.hidden) return;
    if (a.dataset.lb === 'bg') { if (e.target === a) close(); } else if (a.dataset.lb === 'close') close(); else step(a.dataset.lb === 'next' ? 1 : -1);
  });
  addEventListener('popstate', () => { pushed = false; hide(); });                                  // Back button closes the viewer instead of leaving the page
  document.addEventListener('keydown', e => { if (lb.hidden) return; if (e.key === 'Escape') close(); else if (e.key === 'ArrowRight') step(1); else if (e.key === 'ArrowLeft') step(-1); });
  lb.addEventListener('touchstart', e => { const t = e.changedTouches[0]; x0 = t.clientX; y0 = t.clientY; }, {passive: true});
  lb.addEventListener('touchend', e => { const t = e.changedTouches[0], dx = t.clientX - x0, dy = t.clientY - y0;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.3) step(dx < 0 ? 1 : -1); else if (dy > 110 && Math.abs(dy) > Math.abs(dx) * 1.3) close(); }, {passive: true});
  img.addEventListener('error', () => { cnt.textContent = 'This photo could not be loaded.'; });
})();
// "Allow location" pop-up shown once after registering
const wd = $('#welcome'); if (wd) { wd.showModal(); $('#welcome-skip')?.addEventListener('click', () => wd.close()); $('#welcome-allow')?.addEventListener('click', async e => { e.target.disabled = true; const ok = await window.NHAllowLocation?.(); if (!ok) { e.target.disabled = false; wd.close(); } }); }

// radius presets (Post work + work-area pickers): <button data-radius-set="5" data-radius-target="#radius">
document.addEventListener('click', e => { const b = e.target.closest('[data-radius-set]'); if (!b) return; const i = $(b.dataset.radiusTarget); if (!i) return; i.value = b.dataset.radiusSet; i.dispatchEvent(new Event('input', {bubbles: true})); });
$$('[data-range-out]').forEach(i => { const o = $(i.dataset.rangeOut), f = () => { if (o) o.textContent = i.value + ' km'; }; i.addEventListener('input', f); f(); });
// demo QR follows the amount
$$('[data-qr-amount]').forEach(i => { const img = $(i.dataset.qrAmount); let t; i.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { const v = Math.max(0, Math.min(100000, parseInt(i.value || '0', 10) || 0)); img.src = '/wallet/qr.svg?amount=' + v; }, 250); }); });

// ---------- every 5 seconds: quietly refresh the live parts of the page (no reload, nothing you are typing is lost) ----------
(() => {
  const regions = () => $$('[data-region]');
  if (!regions().length && !(ff && grid)) return;
  let busy = false;
  async function tick() {
    if (busy || document.hidden || $('dialog[open]')) return; busy = true;
    try {
      if (ff && grid) window.NHRefreshGrid && NHRefreshGrid();
      if (regions().length) {
        const r = await fetch(location.pathname + location.search, {cache: 'no-store', headers: {'X-Soft-Refresh': '1'}}); if (!r.ok) return;
        const doc = new DOMParser().parseFromString(await r.text(), 'text/html'); let changed = false;
        regions().forEach(cur => {
          const nu = doc.querySelector(`[data-region="${cur.dataset.region}"]`); if (!nu) return;
          if (cur.matches(':focus-within') || cur.querySelector('details[open]')) return;     // someone is using it: don't pull it away
          if (cur.innerHTML.trim() !== nu.innerHTML.trim()) { cur.innerHTML = nu.innerHTML; changed = true; }
        });
        if (changed) document.dispatchEvent(new Event('nh:refreshed'));
      }
    } catch (e) {} finally { busy = false; }
  }
  setInterval(tick, 5000); document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
})();

// charts (admin)
if (window.Chart && window.CHARTS) {
  const col = '#0E6B5B';
  Object.entries(window.CHARTS).forEach(([id, d]) => { const el = document.getElementById('c-' + id); if (!el) return;
    const pie = Array.isArray(d.labels) && d.pie;
    new Chart(el, {type: pie ? 'doughnut' : (d.bar ? 'bar' : 'line'), data: {labels: d.labels, datasets: [{data: d.data, borderColor: col, backgroundColor: pie ? ['#0E6B5B', '#3FB59E', '#D98A12', '#8FA9A2', '#B93A28', '#5F6D68', '#A8D5CB', '#E3B05C'] : col + '33', fill: !pie && !d.bar, tension: .3, pointRadius: 0}]}, options: {plugins: {legend: {display: pie}}, scales: pie ? {} : {y: {beginAtZero: true, ticks: {precision: 0}}, x: {ticks: {maxTicksLimit: 6}}}}}); });
}
})();
