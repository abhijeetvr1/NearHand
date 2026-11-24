/* Voice notes.
   1) Recorder on the Post work form: optional, 90 seconds max. The recording is uploaded the moment it stops;
      the form then only carries the stored file name (hidden fields audio_name / audio_seconds).
   2) Play buttons on marketplace cards: <button data-play="URL" data-secs="42">. One clip plays at a time.        */
(() => {
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const csrf = $('meta[name=csrf]')?.content;
const fmt = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

// ---------- shared player for cards ----------
const player = new Audio(); player.preload = 'none'; let current = null;
const paint = () => $$('[data-play]').forEach(b => { const on = b.dataset.play === current && !player.paused; b.classList.toggle('playing', on); b.setAttribute('aria-pressed', on); const l = $('.pl', b); if (l) l.textContent = on ? 'Pause' : 'Play'; });
player.addEventListener('ended', () => { current = null; paint(); }); player.addEventListener('pause', paint); player.addEventListener('play', paint);
player.addEventListener('error', () => { current = null; paint(); window.toast && toast('That voice note could not be played.', 'error'); });
document.addEventListener('click', e => {
  const b = e.target.closest('[data-play]'); if (!b) return;
  e.preventDefault(); e.stopPropagation();
  if (current === b.dataset.play && !player.paused) { player.pause(); return; }
  if (current !== b.dataset.play) { player.src = b.dataset.play; current = b.dataset.play; }
  player.play().catch(() => {}); paint();
});
document.addEventListener('nh:refreshed', paint);   // the 5-second refresh re-draws the cards; keep the playing state

// ---------- recorder ----------
// Two recorders run together: MediaRecorder (small compressed file) and a Web Audio tap (raw sound, saved as a WAV).
// If a phone's MediaRecorder returns nothing (it happens on some Android/iOS browsers), the Web Audio copy is used, so
// "empty recording" can only mean the microphone really captured no sound.
const root = $('[data-rec]'); if (!root) return;
const MAX = +root.dataset.max || 90, ui = {
  start: $('[data-rec-start]', root), stop: $('[data-rec-stop]', root), redo: $('[data-rec-redo]', root), del: $('[data-rec-del]', root),
  time: $('[data-rec-time]', root), bar: $('[data-rec-bar]', root), msg: $('[data-rec-msg]', root), prev: $('[data-rec-prev]', root),
  name: $('[name=audio_name]', root), secs: $('[name=audio_seconds]', root), remove: $('[name=remove_audio]', root), live: $('[data-rec-live]', root), idle: $('[data-rec-idle]', root)};
const say = (t, bad) => { ui.msg.textContent = t || ''; ui.msg.classList.toggle('bad', !!bad); };
const AC = window.AudioContext || window.webkitAudioContext;
const supported = !!(navigator.mediaDevices?.getUserMedia && (window.MediaRecorder || AC));
let rec = null, chunks = [], stream = null, t0 = 0, tick = null, actx = null, proc = null, srcNode = null, pcm = [], stopping = false, elapsed = 0;

// mp4 first: it plays on every phone. webm/ogg only if the browser can't do mp4.
function pickType() { for (const t of ['audio/mp4;codecs=mp4a.40.2', 'audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/ogg']) if (window.MediaRecorder?.isTypeSupported?.(t)) return t; return ''; }
function show(url, s) {
  ui.prev.innerHTML = ''; const a = document.createElement('audio'); a.controls = true; a.preload = 'metadata'; a.src = url; ui.prev.append(a);
  ui.prev.hidden = false; ui.idle.hidden = true; ui.live.hidden = true; ui.redo.hidden = false; ui.del.hidden = false; ui.start.hidden = true;
  say(`Voice note saved (${fmt(s)}). It will play on your job in the marketplace.`);
}
function clear() {
  ui.name.value = ''; ui.secs.value = ''; if (ui.remove) ui.remove.value = '1'; ui.prev.hidden = true; ui.prev.innerHTML = '';
  ui.idle.hidden = false; ui.live.hidden = true; ui.start.hidden = false; ui.redo.hidden = true; ui.del.hidden = true; say('');
}
function release() {
  clearInterval(tick); try { proc && (proc.onaudioprocess = null, proc.disconnect()); srcNode && srcNode.disconnect(); } catch (e) {}
  stream?.getTracks().forEach(t => t.stop()); stream = null; if (actx && actx.state !== 'closed') actx.close().catch(() => {}); actx = proc = srcNode = null;
}

// raw sound -> mono 12 kHz 16-bit WAV (about 24 KB per second, so 90 s is ~2.2 MB)
function wavFrom(parts, rate) {
  let n = 0; parts.forEach(p => n += p.length); if (!n) return null;
  const OUT = 12000, ratio = rate / OUT, len = Math.floor(n / ratio), flat = new Float32Array(n); let o = 0; parts.forEach(p => { flat.set(p, o); o += p.length; });
  const pcm16 = new Int16Array(len);
  for (let k = 0; k < len; k++) { const a = Math.floor(k * ratio), b = Math.max(a + 1, Math.floor((k + 1) * ratio)); let sum = 0; for (let j = a; j < b && j < n; j++) sum += flat[j]; const v = Math.max(-1, Math.min(1, sum / (b - a))); pcm16[k] = v < 0 ? v * 0x8000 : v * 0x7fff; }
  const buf = new ArrayBuffer(44 + len * 2), dv = new DataView(buf), w = (off, s) => [...s].forEach((c, i) => dv.setUint8(off + i, c.charCodeAt(0)));
  w(0, 'RIFF'); dv.setUint32(4, 36 + len * 2, true); w(8, 'WAVE'); w(12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, OUT, true); dv.setUint32(28, OUT * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); w(36, 'data'); dv.setUint32(40, len * 2, true);
  new Int16Array(buf, 44).set(pcm16); return new Blob([buf], {type: 'audio/wav'});
}

async function begin() {
  if (!supported) return say('Recording is not supported in this browser. You can skip this: the voice note is optional.', true);
  if (!window.isSecureContext) return say('Recording needs HTTPS (or localhost).', true);
  stopping = false; chunks = []; pcm = []; rec = null;
  try { if (AC) { actx = new AC(); actx.resume && actx.resume(); } } catch (e) { actx = null; }      // created inside the tap: iPhones require that
  try { stream = await navigator.mediaDevices.getUserMedia({audio: true}); }
  catch (e) { release(); return say('Microphone access was blocked. Allow the microphone for this site in your browser settings, then try again. The voice note is optional.', true); }
  if (window.MediaRecorder) {
    const type = pickType();
    try { rec = new MediaRecorder(stream, type ? {mimeType: type, audioBitsPerSecond: 48000} : {audioBitsPerSecond: 48000}); } catch (e) { try { rec = new MediaRecorder(stream); } catch (e2) { rec = null; } }
    if (rec) { rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); }; try { rec.start(); } catch (e) { rec = null; } }
  }
  if (actx) { try {
    srcNode = actx.createMediaStreamSource(stream); proc = actx.createScriptProcessor(4096, 1, 1); const mute = actx.createGain(); mute.gain.value = 0;
    proc.onaudioprocess = e => { if (pcm.length * 4096 < actx.sampleRate * (MAX + 3)) pcm.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
    srcNode.connect(proc); proc.connect(mute); mute.connect(actx.destination); } catch (e) { proc = null; } }
  if (!rec && !proc) { release(); return say('This browser could not start recording. You can skip the voice note.', true); }
  t0 = performance.now(); elapsed = 0;
  ui.start.hidden = true; ui.idle.hidden = true; ui.live.hidden = false; ui.prev.hidden = true; ui.redo.hidden = true; ui.del.hidden = true; say('');
  tick = setInterval(() => { elapsed = Math.min(MAX, (performance.now() - t0) / 1000); ui.time.textContent = `${fmt(elapsed)} / ${fmt(MAX)}`; ui.bar.style.width = (100 * elapsed / MAX) + '%'; if (elapsed >= MAX) end(); }, 200);
}
function end() {
  if (stopping || !stream) return; stopping = true; clearInterval(tick); const dur = Math.min(MAX, (performance.now() - t0) / 1000);
  if (rec && rec.state !== 'inactive') { rec.onstop = () => setTimeout(() => finish(dur), 250); try { rec.requestData(); } catch (e) {} try { rec.stop(); } catch (e) { finish(dur); } }
  else finish(dur);
}
async function finish(dur) {
  const rate = actx ? actx.sampleRate : 44100, mrBlob = chunks.length ? new Blob(chunks, {type: (rec && rec.mimeType) || chunks[0].type || 'audio/webm'}) : null, parts = pcm.slice();
  release(); ui.live.hidden = true;
  if (dur < 1) { clear(); return say('That was too short. Hold the recording for at least 1 second.', true); }
  let blob = mrBlob && mrBlob.size >= 1500 ? mrBlob : null;
  if (!blob) blob = wavFrom(parts, rate);                                                       // fallback: the Web Audio copy
  if (!blob || blob.size < 1500) { clear(); return say('No sound was captured. Check that the microphone is not muted and that this site may use it (iPhone: Settings > Safari > Microphone), then try again.', true); }
  say('Saving your voice note…'); const fd = new FormData(); fd.append('audio', blob, 'note.' + (blob.type.includes('wav') ? 'wav' : blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm')); fd.append('seconds', Math.round(dur));
  try {
    const r = await fetch('/orders/audio', {method: 'POST', headers: {'X-CSRFToken': csrf}, body: fd}), j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || 'Could not save the recording.');
    ui.name.value = j.name; ui.secs.value = j.seconds; if (ui.remove) ui.remove.value = ''; show(j.url, j.seconds);
  } catch (e) { clear(); say(e.message, true); }
}
ui.start.addEventListener('click', begin); ui.stop.addEventListener('click', end);
ui.redo.addEventListener('click', () => { clear(); begin(); }); ui.del.addEventListener('click', clear);
if (!supported) { ui.start.disabled = true; say('Recording is not supported in this browser. The voice note is optional, so you can skip it.'); }
if (root.dataset.url) show(root.dataset.url, +ui.secs.value || 0);
addEventListener('beforeunload', release);
})();
