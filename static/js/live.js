/* Live route map (Leaflet + OpenStreetMap, road routes from the free OSM routing service).
   Worker: own position (GPS) + line to the job, position shared with the maker every few seconds while sharing is on.
   Maker : each accepted worker's latest position + line to the job, refreshed every 5 seconds.                         */
(() => {
const el = document.getElementById('live-map'); if (!el) return;
const $ = s => document.getElementById(s), csrf = document.querySelector('meta[name=csrf]')?.content;
const role = el.dataset.role, oid = el.dataset.oid, dest = {lat: +el.dataset.lat, lng: +el.dataset.lng}, approx = el.dataset.approx === '1', blocked = el.dataset.blocked === '1';
const PROFILE = {car: 'routed-car', bike: 'routed-bike', foot: 'routed-foot'}, SPEED = {car: 28, bike: 14, foot: 5};     // km/h, only used if routing is unavailable
const COLORS = ['#22E0A8', '#5B8CFF', '#FFB020', '#FF6B8B', '#B388FF', '#2BD4F0'];
const km = (a, b) => { const r = Math.PI / 180, dLa = (b.lat - a.lat) * r, dLn = (b.lng - a.lng) * r, h = Math.sin(dLa / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLn / 2) ** 2; return 12742 * Math.asin(Math.sqrt(h)); };
const fmtD = k => k < 1 ? Math.round(k * 1000) + ' m' : k.toFixed(k < 10 ? 1 : 0) + ' km', fmtT = s => s < 60 ? '<1 min' : s < 3600 ? Math.round(s / 60) + ' min' : Math.floor(s / 3600) + ' h ' + Math.round((s % 3600) / 60) + ' min';
const ago = s => s < 10 ? 'just now' : s < 60 ? s + ' s ago' : s < 3600 ? Math.floor(s / 60) + ' min ago' : Math.floor(s / 3600) + ' h ago';
const esc = t => String(t).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

(async () => {
  if (!(await NHLoadLeaflet()) || !window.L) { el.innerHTML = '<div class="chat-empty">The map could not load. Check your connection.</div>'; return; }
  const map = L.map(el).setView([dest.lat, dest.lng], 14); NHTiles(map);
  const pin = L.divIcon({className: '', html: '<div class="pinmark"></div>', iconSize: [22, 22], iconAnchor: [11, 22]});
  L.marker([dest.lat, dest.lng], {icon: pin, title: 'Job location'}).addTo(map).bindTooltip('Job location', {permanent: false});
  if (approx) L.circle([dest.lat, dest.lng], {radius: 1000, color: '#FFB020', weight: 1.5, dashArray: '6 6', fillOpacity: .05, interactive: false}).addTo(map);
  const dot = (c, label) => L.divIcon({className: '', html: `<div class="lv-dot" style="--c:${c}"><i></i>${label ? '<b>' + esc(label) + '</b>' : ''}</div>`, iconSize: [30, 30], iconAnchor: [15, 15]});

  // ---- route drawing shared by both roles: a road route when available, else a straight dashed line
  const routes = new Map();         // key -> {line, from, at, ctl}
  async function drawRoute(key, from, color, mode, cb) {
    let r = routes.get(key); if (!r) { r = {line: L.polyline([], {color, weight: 6, opacity: .9}).addTo(map), from: null, at: 0, mode: null}; routes.set(key, r); }
    const moved = !r.from || km(r.from, from) > .075, due = Date.now() - r.at > 12000 || r.mode !== mode;
    if (!(moved && due) && r.line.getLatLngs().length) { cb && cb(r.info); return; }
    r.from = from; r.at = Date.now(); r.mode = mode; r.ctl?.abort(); r.ctl = new AbortController();
    const straight = () => { r.line.setStyle({dashArray: '8 10', opacity: .8}).setLatLngs([[from.lat, from.lng], [dest.lat, dest.lng]]); const d = km(from, dest); r.info = {dist: d, secs: d / SPEED[mode] * 3600, road: false}; cb && cb(r.info); };
    try {
      const u = `https://routing.openstreetmap.de/${PROFILE[mode]}/route/v1/driving/${from.lng},${from.lat};${dest.lng},${dest.lat}?overview=full&geometries=geojson`;
      const j = await (await fetch(u, {signal: r.ctl.signal})).json(), rt = j.routes && j.routes[0]; if (!rt) return straight();
      r.line.setStyle({dashArray: null, opacity: .9}).setLatLngs(rt.geometry.coordinates.map(c => [c[1], c[0]])); r.info = {dist: rt.distance / 1000, secs: rt.duration, road: true}; cb && cb(r.info);
    } catch (e) { if (e.name !== 'AbortError') straight(); }
  }

  if (role === 'worker') {
    let mode = 'car', me = null, meMarker = null, acc = null, watch = null, lastSent = 0, fitted = false, arrived = false;
    const state = t => { $('lv-state').textContent = t; }, say = (t, bad) => { const m = $('lv-msg'); m.textContent = t; m.classList.toggle('bad', !!bad); };
    const fit = () => me ? map.fitBounds([[me.lat, me.lng], [dest.lat, dest.lng]], {padding: [50, 50], maxZoom: 16}) : map.setView([dest.lat, dest.lng], 15);
    $('lv-fit').onclick = fit;
    $('lv-mode').addEventListener('click', e => { const b = e.target.closest('[data-mode]'); if (!b) return; mode = b.dataset.mode; [...$('lv-mode').children].forEach(x => x.classList.toggle('on', x === b)); me && update(); });
    function update() { drawRoute('me', me, COLORS[0], mode, i => { if (!i) return; $('lv-dist').textContent = fmtD(i.dist); $('lv-eta').textContent = fmtT(i.secs) + (i.road ? '' : ' (est.)'); }); }
    async function push() {
      if (!me || Date.now() - lastSent < 5000) return; lastSent = Date.now();
      try { const r = await fetch(`/orders/${oid}/track`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf}, body: JSON.stringify({lat: me.lat, lng: me.lng, acc: Math.round(me.acc || 0)})});
        if (r.status === 403 || r.status === 410) { const j = await r.json().catch(() => ({})); stop(); say(j.error || 'Sharing stopped.', true); } } catch (e) {}
    }
    function onPos(p) {
      me = {lat: p.coords.latitude, lng: p.coords.longitude, acc: p.coords.accuracy};
      if (!meMarker) { meMarker = L.marker([me.lat, me.lng], {icon: dot(COLORS[1], 'You'), zIndexOffset: 500}).addTo(map); acc = L.circle([me.lat, me.lng], {radius: me.acc, color: COLORS[1], weight: 1, fillOpacity: .08, interactive: false}).addTo(map); }
      else { meMarker.setLatLng([me.lat, me.lng]); acc.setLatLng([me.lat, me.lng]).setRadius(me.acc); }
      if (!fitted) { fit(); fitted = true; }
      update(); push();
      if (!arrived && !approx && km(me, dest) < .05) { arrived = true; toast('You have arrived.'); say('You have arrived. Tell the maker you are here.'); }
    }
    function start() {
      if (blocked) return; if (!navigator.geolocation) return say('Your browser can\'t share location.', true);
      if (watch !== null) return; state('Starting…'); say('Waiting for your location…');
      watch = navigator.geolocation.watchPosition(p => { state('On'); say('Sharing your live position. Keep this page open while you travel.'); $('lv-start').hidden = true; $('lv-stop').hidden = false; onPos(p); },
        err => { stop(); say(err.code === 1 ? 'Location is blocked. Allow it in your browser settings to see your route.' : 'Could not get your location. Move to an open area and try again.', true); }, {enableHighAccuracy: true, maximumAge: 3000, timeout: 20000});
    }
    function stop() { if (watch !== null) navigator.geolocation.clearWatch(watch); watch = null; state('Off'); $('lv-start').hidden = false; $('lv-stop').hidden = true; }
    $('lv-start').onclick = start; $('lv-stop').onclick = () => { stop(); say('Sharing is off. The maker can no longer see where you are.'); };
    try { if (!blocked && (await navigator.permissions.query({name: 'geolocation'})).state === 'granted') start(); } catch (e) {}
    fit();
    return;
  }

  // ---- maker: poll every 5 s
  const markers = new Map(); let firstFit = false, idx = new Map();
  const colorOf = id => { if (!idx.has(id)) idx.set(id, COLORS[idx.size % COLORS.length]); return idx.get(id); };
  const fitAll = () => { const pts = [[dest.lat, dest.lng], ...[...markers.values()].map(m => m.getLatLng())]; map.fitBounds(pts, {padding: [50, 50], maxZoom: 16}); };
  $('lv-fit').onclick = fitAll;
  const rows = new Map();
  function render(list) {
    $('lv-list').innerHTML = list.map(w => `<div class="lvrow" data-w="${w.id}"><span class="lvc" style="background:${colorOf(w.id)}"></span><div class="grow"><b>${esc(w.name)}</b><div class="muted sm" data-info>${w.lat == null ? 'Not sharing yet' : esc(rows.get(w.id) || 'Locating…')} ${w.age != null ? '· ' + ago(w.age) : ''}</div></div></div>`).join('') || '<div class="muted sm">No accepted workers yet.</div>';
  }
  let timer = null;
  async function poll() {
    try {
      const j = await (await fetch(`/orders/${oid}/track.json`, {cache: 'no-store'})).json();
      if (!j.active) { clearInterval(timer); $('lv-list').innerHTML = '<div class="notice">This job has ended. Tracking stopped.</div>'; return; }
      render(j.workers);
      for (const w of j.workers) {
        if (w.lat == null) continue; const c = colorOf(w.id), stale = w.age > 120, p = {lat: w.lat, lng: w.lng};
        let m = markers.get(w.id); if (!m) { m = L.marker([w.lat, w.lng], {icon: dot(c, w.name.split(' ')[0]), zIndexOffset: 400}).addTo(map); markers.set(w.id, m); } else m.setLatLng([w.lat, w.lng]);
        m.getElement()?.classList.toggle('stale', stale);
        drawRoute('w' + w.id, p, c, 'car', i => { if (!i) return; rows.set(w.id, `${fmtD(i.dist)} away · about ${fmtT(i.secs)}${i.road ? '' : ' (est.)'}`); const row = document.querySelector(`[data-w="${w.id}"] [data-info]`); if (row) row.textContent = rows.get(w.id) + ' · ' + ago(w.age); });
      }
      if (!firstFit && markers.size) { fitAll(); firstFit = true; }
    } catch (e) {}
  }
  poll(); timer = setInterval(() => { if (!document.hidden) poll(); }, 5000);
})();
})();
