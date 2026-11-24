/* Job photos on the Post work form: pick up to 10, each is shrunk in the browser and uploaded one by one.
   Hidden fields: photo_names (new uploads, comma separated) and remove_photos (ids of saved photos to delete, edit page).   */
(() => {
const root = document.querySelector('[data-photos]'); if (!root) return;
const $ = s => root.querySelector(s), csrf = document.querySelector('meta[name=csrf]')?.content, MAX = +root.dataset.max || 10;
const grid = $('[data-pgrid]'), names = $('[name=photo_names]'), removed = $('[name=remove_photos]'), msg = $('[data-pmsg]'), count = $('[data-pcount]'), file = $('[data-pfile]'), add = $('[data-padd]');
const list = el => (el.value || '').split(',').map(x => x.trim()).filter(Boolean);
const total = () => grid.querySelectorAll('figure').length;
const paint = () => { count.textContent = `${total()} / ${MAX}`; add.style.opacity = total() >= MAX ? .5 : 1; add.style.pointerEvents = total() >= MAX ? 'none' : ''; };
const say = (t, bad) => { msg.textContent = t || ''; msg.classList.toggle('bad', !!bad); };

async function shrink(f) {            // longest side 1600 px, JPEG: a phone photo of 4 MB becomes ~300 KB
  try { const bmp = await createImageBitmap(f), k = Math.min(1, 1600 / Math.max(bmp.width, bmp.height)), c = document.createElement('canvas'); c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height); const b = await new Promise(r => c.toBlob(r, 'image/jpeg', .85)); return b && b.size < f.size ? b : f; } catch (e) { return f; }
}
async function upload(f) {
  const fd = new FormData(); fd.append('photo', await shrink(f), 'photo.jpg');
  const r = await fetch('/orders/photo', {method: 'POST', headers: {'X-CSRFToken': csrf}, body: fd}), j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) throw new Error(j.error || 'Could not upload that photo.'); return j;
}
file.addEventListener('change', async () => {
  const picks = [...file.files]; file.value = ''; say('');
  for (const f of picks) {
    if (total() >= MAX) { say(`You can add up to ${MAX} photos.`, true); break; }
    if (!f.type.startsWith('image/')) { say('Only photos can be added.', true); continue; }
    const fig = document.createElement('figure'); fig.className = 'busy'; fig.innerHTML = '<span class="spin"></span>'; grid.append(fig); paint();
    try { const j = await upload(f); fig.className = ''; fig.dataset.pname = j.name; fig.innerHTML = `<img src="${j.thumb}" alt="Job photo"><button type="button" data-premove aria-label="Remove this photo">×</button>`; names.value = [...list(names), j.name].join(','); }
    catch (e) { fig.remove(); say(e.message, true); }
    paint();
  }
});
grid.addEventListener('click', e => {
  const b = e.target.closest('[data-premove]'); if (!b) return; const fig = b.closest('figure');
  if (fig.dataset.pid) removed.value = [...list(removed), fig.dataset.pid].join(','); else names.value = list(names).filter(n => n !== fig.dataset.pname).join(',');
  fig.remove(); paint(); say('');
});
paint();
})();
