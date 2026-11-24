/* Static dotted globe (facing India) + India dot map. Dots "pop" at random places on India and fade away. */
(() => {
const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
const rad = d => d * Math.PI / 180, rand = a => a[Math.floor(Math.random() * a.length)];
const ease = t => { const c = 1.9; return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); };  // easeOutBack

async function load() {
  const [land, act] = await Promise.all([fetch('/static/data/land.json').then(r => r.json()), fetch('/api/activity').then(r => r.json()).catch(() => ({points: [], stats: {}, feed: []}))]);
  return {land, act};
}
function fit(c) { const d = Math.min(devicePixelRatio || 1, 2), r = c.getBoundingClientRect(); c.width = Math.round(r.width * d); c.height = Math.round(r.height * d); return {w: r.width, h: r.height, d}; }

// Shared engine: draw a cached static layer, then animate pops on top.
function engine(c, paintStatic, project, pool, real) {
  const ctx = c.getContext('2d'), layer = document.createElement('canvas'); let S = fit(c), pops = [], next = 0;
  const repaint = () => { S = fit(c); layer.width = c.width; layer.height = c.height; const L = layer.getContext('2d'); L.setTransform(S.d, 0, 0, S.d, 0, 0); paintStatic(L, S); };
  const spawn = now => {
    const useReal = real.length && Math.random() < .3, p = useReal ? rand(real) : rand(pool);
    pops.push({la: p[0], ln: p[1], born: now, life: 2000 + Math.random() * 1600, col: (useReal && p[2] === 'job') || Math.random() < .18 ? '242,184,75' : '124,246,214'});
  };
  const drawPop = (p, now) => {
    const xy = project(p.la, p.ln, S); if (!xy) return false; const t = (now - p.born) / p.life; if (t >= 1) return false;
    const k = Math.max(.55, Math.min(1, S.w / 420)), grow = (t < .16 ? ease(t / .16) : 1) * k, a = t < .6 ? 1 : 1 - (t - .6) / .4, [x, y] = xy;
    const g = ctx.createRadialGradient(x, y, 0, x, y, 16 * grow + 1); g.addColorStop(0, `rgba(${p.col},${.5 * a})`); g.addColorStop(1, `rgba(${p.col},0)`);
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, 16 * grow + 1, 0, 7); ctx.fill();
    ctx.strokeStyle = `rgba(${p.col},${(1 - t) * .6})`; ctx.lineWidth = 1.2; ctx.beginPath(); ctx.arc(x, y, (5 + t * 20) * k, 0, 7); ctx.stroke();
    ctx.fillStyle = `rgba(${p.col},${a})`; ctx.beginPath(); ctx.arc(x, y, 3.4 * grow + .4, 0, 7); ctx.fill(); return true;
  };
  function frame(now) {
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, c.width, c.height); ctx.drawImage(layer, 0, 0);
    ctx.setTransform(S.d, 0, 0, S.d, 0, 0);
    if (now > next) { spawn(now); next = now + 260 + Math.random() * 520; }
    pops = pops.filter(p => drawPop(p, now));
    requestAnimationFrame(frame);
  }
  repaint(); addEventListener('resize', repaint);
  if (reduce) { for (let i = 0; i < 8; i++) { spawn(performance.now() - 600); } ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.drawImage(layer, 0, 0); ctx.setTransform(S.d, 0, 0, S.d, 0, 0); pops.forEach(p => drawPop(p, performance.now())); }
  else requestAnimationFrame(frame);
}

window.NHGlobe = async function (c, opts = {}) {
  // India is magnified toward the centre of the disc (K > 1) so it reads big while the whole globe stays visible.
  const {land, act} = await load(), rot = rad(-79), tilt = rad(opts.tilt ?? 20), K = opts.zoom ?? 2.1;
  const world = land.world.map(([a, b]) => [rad(a), rad(b)]), ind = land.india.map(([a, b]) => [rad(a), rad(b)]);
  const P = (la, ln) => { const x = Math.cos(la) * Math.sin(ln + rot), y = Math.sin(la), z = Math.cos(la) * Math.cos(ln + rot);
    let X = x, Y = -(y * Math.cos(tilt) - z * Math.sin(tilt)); const Z = y * Math.sin(tilt) + z * Math.cos(tilt), r = Math.hypot(X, Y);
    if (r > 1e-6) { const r2 = 1 - Math.pow(1 - Math.min(r, 1), K); X *= r2 / r; Y *= r2 / r; } return [X, Y, Z]; };
  const geom = S => ({R: Math.min(S.w, S.h) * .46, cx: S.w / 2, cy: S.h / 2});
  const paint = (L, S) => {
    const {R, cx, cy} = geom(S), gr = Math.min(R * 1.22, S.w / 2 - 1, S.h / 2 - 1), g = L.createRadialGradient(cx - R * .3, cy - R * .35, R * .1, cx, cy, gr);
    g.addColorStop(0, 'rgba(34,224,168,.14)'); g.addColorStop(.7, 'rgba(8,40,32,.5)'); g.addColorStop(1, 'rgba(34,224,168,0)');
    L.fillStyle = g; L.beginPath(); L.arc(cx, cy, gr, 0, 7); L.fill();
    L.strokeStyle = 'rgba(34,224,168,.3)'; L.lineWidth = 1.2; L.beginPath(); L.arc(cx, cy, R, 0, 7); L.stroke();
    const [ix, iy] = P(rad(22), rad(79)), glow = L.createRadialGradient(cx + ix * R, cy + iy * R, 0, cx + ix * R, cy + iy * R, R * .8);
    glow.addColorStop(0, 'rgba(34,224,168,.2)'); glow.addColorStop(1, 'rgba(34,224,168,0)'); L.fillStyle = glow; L.beginPath(); L.arc(cx + ix * R, cy + iy * R, R * .8, 0, 7); L.fill();
    for (const [la, ln] of world) { const [x, y, z] = P(la, ln); if (z <= 0) continue; L.fillStyle = `rgba(150,235,210,${.1 + z * .32})`; L.fillRect(cx + x * R - .7, cy + y * R - .7, 1.5, 1.5); }
    for (const [la, ln] of ind) { const [x, y, z] = P(la, ln); if (z <= 0) continue; L.fillStyle = `rgba(52,240,180,${.55 + z * .45})`; L.beginPath(); L.arc(cx + x * R, cy + y * R, 2 + z * .9, 0, 7); L.fill(); }
  };
  const real = act.points.map(p => [rad(p.lat), rad(p.lng), p.k]);
  const project = (la, ln, S) => { const {R, cx, cy} = geom(S), [x, y, z] = P(la, ln); return z > .05 ? [cx + x * R, cy + y * R] : null; };
  engine(c, paint, project, ind, real); return act;
};

window.NHIndia = async function (c) {
  const {land, act} = await load(), ind = land.india, real = act.points.map(p => [p.lat, p.lng, p.k]);
  const xy = (la, ln, S) => { const k = Math.min(S.w * .96 / (31.4), S.h * .96 / 31.6 * Math.cos(rad(22)) ) / Math.cos(rad(22)); const s = Math.min(S.w * .96 / 31.4 / Math.cos(rad(22)), S.h * .96 / 31.6);
    return [S.w / 2 + (ln - 83) * s * Math.cos(rad(22)), S.h / 2 - (la - 21.8) * s]; };
  const paint = (L, S) => { const r = Math.max(1.3, Math.min(2.8, S.w / 115)); for (const [la, ln] of ind) { const [x, y] = xy(la, ln, S); L.fillStyle = 'rgba(34,224,168,.55)'; L.beginPath(); L.arc(x, y, r, 0, 7); L.fill(); } };
  engine(c, paint, (la, ln, S) => xy(la, ln, S), ind, real); return act;
};
})();
