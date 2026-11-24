/* Location pickers. Maps: Leaflet for pins/circles + MapLibre vector tiles (OpenFreeMap, built on OpenStreetMap). Place names: Nominatim. No API key, no Google.
   Boundaries: in 'india' mode (MAP_BOUNDARY, the default) the tiles' own border lines are removed and India's official external boundary
   (Survey of India, incl. all of Jammu & Kashmir and Ladakh) is drawn from /static/data/india_official.geojson. Set MAP_BOUNDARY=osm to keep OpenStreetMap's borders.
   - NHMap(el, {lat, lng, radius, onPick}) -> {detect, invalidate, place, setRadius, search}
   - <div class="mapbox" data-map data-lat="#lat" data-lng="#lng" data-area="#area" data-radius="#radius" data-autodetect>  (inline pickers)
   - header "Your work area" dialog (#loc-dialog) with a radius slider                                                      */
(() => {
const $ = s => document.querySelector(s);
const csrf = $('meta[name=csrf]')?.content;
const post = (u, b) => fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf}, body: JSON.stringify(b || {})});
const dark = () => document.documentElement.dataset.theme !== 'light';
const INDIA = [22.6, 79.0];

// Libraries are served from /static/vendor (Leaflet, MapLibre GL, and the Leaflet<->MapLibre bridge), so the map has no CDN dependency.
const V = '/static/vendor/', BOUNDARY = $('meta[name=map-boundary]')?.content || 'india';
const addCss = h => new Promise(r => { const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = h; l.onload = l.onerror = () => r(); document.head.appendChild(l); });
const addJs = h => new Promise((res, rej) => { const s = document.createElement('script'); s.src = h; s.onload = res; s.onerror = () => rej(new Error('load ' + h)); document.head.appendChild(s); });
let lp = null;
const loadLeaflet = () => window.L ? Promise.resolve(true) : (lp = lp || (async () => {
  try {
    await Promise.all([addCss(V + 'leaflet/leaflet.css'), addCss(V + 'maplibre-gl.css'), addJs(V + 'leaflet/leaflet.js')]);
    try { await addJs(V + 'maplibre-gl.js'); await addJs(V + 'leaflet-maplibre-gl.js'); } catch (e) { console.warn('vector map unavailable, using image tiles', e); }
    return true;
  } catch (e) { return false; }
})());

// ---- base map. Vector tiles with India's official boundary; falls back to plain image tiles if WebGL or the style can't load.
const webgl = () => { try { const c = document.createElement('canvas'); return !!(c.getContext('webgl2') || c.getContext('webgl')); } catch (e) { return false; } };
const OFM = 'https://tiles.openfreemap.org/styles/', ATTR = '<a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> © <a href="https://openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> · © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';
const INDIA_NOTE = ' · India boundary: Survey of India (via DataMeet)';
const lineColor = () => dark() ? '#7b8592' : '#6f7782';
let boundaryData = null;
const officialBoundary = async () => boundaryData || (boundaryData = await (await fetch(V.replace('vendor/', 'data/') + 'india_official.geojson')).json());

async function vectorBase(map) {
  const st = await (await fetch(OFM + (dark() ? 'dark' : 'positron'))).json();
  if (BOUNDARY === 'india') {
    st.layers = st.layers.filter(l => l['source-layer'] !== 'boundary');            // drop every border line the tiles draw (the cut J&K / Ladakh edges)
    st.sources.india_official = {type: 'geojson', data: await officialBoundary()};
    const at = st.layers.findIndex(l => l.type === 'symbol');                        // draw our line under the labels
    st.layers.splice(at < 0 ? st.layers.length : at, 0, {id: 'india-official-outline', type: 'line', source: 'india_official',
      layout: {'line-join': 'round', 'line-cap': 'round'}, paint: {'line-color': lineColor(), 'line-width': ['interpolate', ['linear'], ['zoom'], 0, .7, 5, 1.2, 10, 2]}});
  }
  const gl = L.maplibreGL({style: st}).addTo(map);
  (window.NHGL = window.NHGL || []).push(gl.getMaplibreMap());       // handy for debugging in the console
  map.attributionControl?.addAttribution(ATTR + (BOUNDARY === 'india' ? INDIA_NOTE : ''));
}
async function imageBase(map) {
  L.tileLayer('https://{s}.basemaps.cartocdn.com/' + (dark() ? 'dark_all' : 'rastertiles/voyager') + '/{z}/{x}/{y}{r}.png', {maxZoom: 18, subdomains: 'abcd', attribution: '© OpenStreetMap, © CARTO'}).addTo(map);
  if (BOUNDARY === 'india') {                                                        // best effort: draw the official line over the image tiles
    try { L.geoJSON(await officialBoundary(), {interactive: false, style: {color: lineColor(), weight: 1.6, opacity: 1}}).addTo(map); } catch (e) {}
  }
}
async function addBase(map) {
  if (window.maplibregl && L.maplibreGL && webgl()) { try { return await vectorBase(map); } catch (e) { console.warn('vector style failed, using image tiles', e); } }
  return imageBase(map);
}

// --- place names (reverse) and place search (forward) via Nominatim; small cache + one request at a time keeps us inside their usage policy
const cache = new Map();
async function nominatim(path) {
  if (cache.has(path)) return cache.get(path);
  try { const r = await fetch('https://nominatim.openstreetmap.org/' + path, {headers: {'Accept-Language': navigator.language || 'en'}}); const j = await r.json(); cache.set(path, j); return j; } catch (e) { return null; }
}
async function areaName(la, ln) {
  const j = await nominatim(`reverse?format=jsonv2&zoom=12&lat=${la.toFixed(3)}&lon=${ln.toFixed(3)}`), a = j?.address || {};
  const n = a.suburb || a.city_district || a.city || a.town || a.village || a.county || a.state_district || a.state; return n ? n + ' Area' : '';
}
async function findPlace(q) { const j = await nominatim(`search?format=jsonv2&limit=1&q=${encodeURIComponent(q)}`); return j && j[0] ? {lat: +j[0].lat, lng: +j[0].lon} : null; }

const permDenied = async () => { try { return (await navigator.permissions.query({name: 'geolocation'})).state === 'denied'; } catch (e) { return false; } };
const detectWith = place => () => new Promise(res => {
  if (!navigator.geolocation) { toast('Your browser can\'t share location. Tap the map or search for your area.', 'error'); return res(false); }
  navigator.geolocation.getCurrentPosition(p => { place(p.coords.latitude, p.coords.longitude, true); res(true); },
    () => { toast('Location is blocked. Tap the map or search for your area instead.', 'error'); res(false); }, {enableHighAccuracy: true, timeout: 12000});
});

function leaflet_(el, {lat, lng, radius, onPick}) {
  const has = lat != null && !isNaN(lat), icon = L.divIcon({className: '', html: '<div class="pinmark"></div>', iconSize: [22, 22], iconAnchor: [11, 22]});
  const map = L.map(el).setView(has ? [lat, lng] : INDIA, has ? 13 : 5);
  addBase(map);
  let marker = null, circle = null, km = radius || 0;
  const draw = () => { if (!marker) return; const p = marker.getLatLng(); if (circle) { circle.setLatLng(p); circle.setRadius(km * 1000); } else if (km) circle = L.circle(p, {radius: km * 1000, color: '#22E0A8', weight: 1.5, fillColor: '#22E0A8', fillOpacity: .08, interactive: false}).addTo(map); if (!km && circle) { circle.remove(); circle = null; } };
  const fit = () => { if (circle && km) map.fitBounds(circle.getBounds(), {padding: [20, 20], maxZoom: 15}); };
  const mk = (la, ln) => { marker = L.marker([la, ln], {draggable: true, icon}).addTo(map).on('drag', draw).on('dragend', () => { const p = marker.getLatLng(); place(p.lat, p.lng); }); };
  const place = async (la, ln, fly) => {
    la = +la.toFixed(5); ln = +ln.toFixed(5); marker ? marker.setLatLng([la, ln]) : mk(la, ln); draw();
    if (fly) { km ? fit() : map.flyTo([la, ln], Math.max(map.getZoom(), 13)); }
    onPick && onPick(la, ln, await areaName(la, ln));
  };
  if (has) { mk(lat, lng); draw(); setTimeout(fit, 50); }
  map.on('click', e => place(e.latlng.lat, e.latlng.lng));
  return {detect: detectWith(place), invalidate: () => map.invalidateSize(), place,
    setRadius: k => { km = +k || 0; draw(); fit(); },
    search: async q => { const p = await findPlace(q); if (!p) { toast('Could not find that place. Try a bigger area name.', 'error'); return false; } place(p.lat, p.lng, true); return true; },
    has: () => !!marker};
}

window.NHLoadLeaflet = loadLeaflet;
window.NHTiles = map => addBase(map);      // same base map (and official India boundary) as the pickers
// "Allow location" in the pop-up after registering: detect, name the area, save it as the work area, reload
window.NHAllowLocation = () => new Promise(res => {
  if (!navigator.geolocation) { toast('Your browser can\'t share location. You can set your work area from the pin at the top.', 'error'); return res(false); }
  navigator.geolocation.getCurrentPosition(async p => { const la = p.coords.latitude, ln = p.coords.longitude, name = await areaName(la, ln);
    await post('/api/location', {lat: la, lng: ln, name, radius: 25, save: true}); res(true); location.reload(); },
    () => { toast('Location is blocked. You can set your work area from the pin at the top.', 'error'); res(false); }, {enableHighAccuracy: true, timeout: 12000});
});

window.NHMap = async (el, opts = {}) => {
  try { if (await loadLeaflet()) return leaflet_(el, opts); } catch (e) { console.error(e); }
  el.innerHTML = '<div class="chat-empty">The map could not load. Check your connection, or type your area name.</div>'; return null;
};

// 1) inline pickers
document.querySelectorAll('[data-map]').forEach(el => {
  const start = async () => {
    const la = $(el.dataset.lat), ln = $(el.dataset.lng), ar = $(el.dataset.area), rad = el.dataset.radius ? $(el.dataset.radius) : null, msg = $('#loc-state');
    const ctl = await NHMap(el, {lat: parseFloat(la.value), lng: parseFloat(ln.value), radius: rad ? +rad.value : 0, onPick: (a, b, name) => {
      la.value = a.toFixed(5); ln.value = b.toFixed(5); la.dispatchEvent(new Event('change')); 
      if (name && ar && (!ar.value || ar.dataset.auto === '1')) { ar.value = name; ar.dataset.auto = '1'; }
      if (msg) msg.textContent = 'Pin set. Only an approximate point (about 1 km) is saved.'; }});
    if (!ctl) return;
    ar?.addEventListener('input', () => { ar.dataset.auto = '0'; });
    rad?.addEventListener('input', () => ctl.setRadius(+rad.value));
    document.querySelectorAll('[data-detect]').forEach(b => b.addEventListener('click', () => ctl.detect()));
    const sb = el.dataset.search ? $(el.dataset.search) : null;
    sb?.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); sb.value.trim() && ctl.search(sb.value.trim()); } });
    $('[data-search-go]')?.addEventListener('click', () => sb?.value.trim() && ctl.search(sb.value.trim()));
    // ask for the location automatically the first time (people can still tap the map or search instead)
    if (el.hasAttribute('data-autodetect') && isNaN(parseFloat(la.value)) && !(await permDenied())) { if (msg) msg.textContent = 'Finding your location…'; ctl.detect(); }
  };
  if (el.offsetWidth > 0) start(); else new ResizeObserver((_, ro) => { if (el.offsetWidth > 0) { ro.disconnect(); start(); } }).observe(el);   // wait until a hidden tab is shown
});

// 2) header "work area" chooser (location + radius)
const dlg = $('#loc-dialog');
if (dlg) {
  let ctl = null, picked = null; const slider = $('#loc-radius'), out = $('#loc-radius-out');
  const radius = () => +slider.value, paint = () => { out.textContent = radius() + ' km'; };
  const ensure = async () => {
    if (!ctl) ctl = await NHMap($('#loc-map'), {lat: parseFloat(dlg.dataset.lat), lng: parseFloat(dlg.dataset.lng), radius: radius(), onPick: (a, b, n) => { picked = {lat: a, lng: b, name: n}; $('#loc-label').textContent = n || 'Selected spot'; $('#loc-save').disabled = false; }});
    setTimeout(() => ctl?.invalidate(), 250);
  };
  paint();
  slider.addEventListener('input', () => { paint(); ctl?.setRadius(radius()); if (picked) $('#loc-save').disabled = false; });
  document.addEventListener('click', e => { if (e.target.closest('[data-loc-open]')) { e.preventDefault(); if (!dlg.open) dlg.showModal(); ensure(); } });
  $('#loc-detect').addEventListener('click', async () => { await ensure(); ctl?.detect(); });
  $('#loc-search')?.addEventListener('keydown', async e => { if (e.key === 'Enter') { e.preventDefault(); await ensure(); const v = e.target.value.trim(); v && ctl?.search(v); } });
  $('#loc-close').addEventListener('click', () => dlg.close());
  $('#loc-save').addEventListener('click', async () => {
    if (!picked && dlg.dataset.lat) picked = {lat: parseFloat(dlg.dataset.lat), lng: parseFloat(dlg.dataset.lng), name: dlg.dataset.name};
    if (!picked) return; await post('/api/location', {...picked, radius: radius(), save: dlg.dataset.auth === '1'}); location.reload(); });
  $('#loc-clear')?.addEventListener('click', async () => { await post('/api/location/clear'); location.reload(); });
}
})();
