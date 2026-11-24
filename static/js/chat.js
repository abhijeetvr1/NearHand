/* WhatsApp-style chat: bubbles, ticks, date separators, live polling, sound.
   Messages are plain UTF-8 text (Hindi, emoji, line breaks); they are always inserted with textContent, never as HTML. */
(() => {
const $ = s => document.querySelector(s), raw = $('#chat-data'); if (!raw) return;
const data = JSON.parse(raw.textContent), box = $('#msgs'), form = $('#composer'), csrf = $('meta[name=csrf]').content;
window.NH_ACTIVE_CHAT = data.user;
const TICK = '<svg width="16" height="11" viewBox="0 0 16 11" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M1 6l3.2 3.2L11 2"/><path d="M6 8l.6.6L14 2"/></svg>';
const time = ts => new Date(ts).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
const label = ts => { const d = new Date(ts), t = new Date(), y = new Date(); y.setDate(t.getDate() - 1); return d.toDateString() === t.toDateString() ? 'Today' : d.toDateString() === y.toDateString() ? 'Yesterday' : d.toLocaleDateString([], {day: 'numeric', month: 'long', year: 'numeric'}); };
const list = [], els = {}; let day = '', lastIn = 0;       // lastIn = newest id received FROM the other person (what we ask the server for)
const nearBottom = () => box.scrollHeight - box.scrollTop - box.clientHeight < 140;

function bubble(m) {
  const k = new Date(m.ts).toDateString(); if (k !== day) { day = k; box.insertAdjacentHTML('beforeend', `<div class="sep">${label(m.ts)}</div>`); }
  const d = document.createElement('div'); d.className = 'bub ' + (m.mine ? 'me' : 'them'); d.dataset.id = m.id;
  d.innerHTML = `<span class="txt"></span><span class="m">${time(m.ts)}${m.mine ? `<span class="tick2 ${m.read ? 'seen' : ''}">${TICK}</span>` : ''}</span>`;
  d.querySelector('.txt').textContent = m.body; box.appendChild(d); els[m.id] = d;
}
function renderAll() { box.innerHTML = ''; day = ''; list.forEach(bubble); }
function add(m) {
  if (els[m.id]) return false;
  if (!m.mine) lastIn = Math.max(lastIn, m.id);
  const newest = !list.length || m.id > list[list.length - 1].id;
  list.push(m); list.sort((a, b) => a.id - b.id);
  newest ? bubble(m) : renderAll();          // a late arrival is slotted into the right place
  return true;
}
data.msgs.forEach(add); box.scrollTop = box.scrollHeight;
if (!data.msgs.length) box.insertAdjacentHTML('beforeend', '<div class="chat-empty" id="hello">Say hello. Keep payments and personal details inside the agreed order.</div>');

const ta = form?.querySelector('textarea'), grow = () => { if (!ta) return; ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 140) + 'px'; };
ta?.addEventListener('input', grow);
ta?.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !matchMedia('(pointer: coarse)').matches) { e.preventDefault(); form.requestSubmit(); } });   // phones: Enter = new line, the button sends
let sending = false;
form?.addEventListener('submit', async e => {
  e.preventDefault(); const i = ta || form.querySelector('input'), body = i.value.replace(/\s+$/, ''); if (!body.trim() || sending) return;
  sending = true; i.value = ''; grow(); i.focus();
  try {
    const r = await fetch(`/chat/${data.user}/send`, {method: 'POST', headers: {'Content-Type': 'application/json; charset=utf-8', 'X-CSRFToken': csrf}, body: JSON.stringify({body})});
    const j = await r.json().catch(() => ({})); if (!r.ok || !j.ok) throw new Error(j.error || 'Message not sent.');
    $('#hello')?.remove(); add(j.msg); box.scrollTop = box.scrollHeight;
  } catch (err) { i.value = body; grow(); toast(err.message || 'Message not sent. Check your connection.', 'error'); }   // the text comes back so nothing is lost
  finally { sending = false; }
});
async function poll() {
  try {
    const r = await fetch(`/chat/${data.user}/poll?after=${lastIn}`, {cache: 'no-store'}); if (!r.ok) return; const j = await r.json(), stick = nearBottom();
    let got = 0; j.msgs.forEach(m => { if (add(m)) got++; });
    if (got) { $('#hello')?.remove(); window.nhSound && nhSound('msg'); if (stick) box.scrollTop = box.scrollHeight; }
    j.seen.forEach(id => els[id]?.querySelector('.tick2')?.classList.add('seen'));
  } catch (e) {}
}
setInterval(poll, 2500); document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
