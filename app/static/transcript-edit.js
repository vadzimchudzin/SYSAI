// Corrections of one replica on the meeting page: the editor opens under the replica (✎), the forms post as usual.
(() => {
  const root = document.getElementById('meeting-transcript');
  if (!root) return;

  // A link to a replica (#seg-N) or to the reference block lands inside the folded transcript: unfold the way to it.
  const focusReplica = () => {
    const id = location.hash.slice(1);
    if (!id.startsWith('seg-') && id !== 'diarization-reference') return;
    const target = document.getElementById(id);
    if (!target || !root.contains(target)) return;
    for (let node = target; node && node !== document.body; node = node.parentElement) {
      if (node.tagName === 'DETAILS') node.open = true;
    }
    if (id.startsWith('seg-')) {
      target.classList.add('seg-flash');
      setTimeout(() => target.classList.remove('seg-flash'), 2600);
    }
    requestAnimationFrame(() => target.scrollIntoView({block: id.startsWith('seg-') ? 'center' : 'start'}));
  };

  const close = () => root.querySelectorAll('.seg-editor-wrap').forEach(node => node.remove());

  const syncGuest = select => {
    const guest = select.form && select.form.querySelector('[data-seg-guest]');
    if (guest) guest.hidden = select.value !== 'guest';
  };

  root.addEventListener('click', async event => {
    const link = event.target.closest('[data-seg-edit]');
    if (link) {
      event.preventDefault();
      const replica = link.closest('p.seg');
      const opened = root.querySelector('.seg-editor-wrap');
      const same = opened && opened.previousElementSibling === replica;
      close();
      if (same || !replica) return;
      const wrap = document.createElement('div');
      wrap.className = 'seg-editor-wrap';
      wrap.setAttribute('aria-live', 'polite');
      wrap.textContent = 'Открываю правку…';
      replica.after(wrap);
      try {
        const response = await fetch(`${link.getAttribute('href')}?fragment=1`, {credentials: 'same-origin'});
        if (!response.ok) throw new Error(String(response.status));
        wrap.innerHTML = await response.text();
        wrap.querySelectorAll('[data-seg-who]').forEach(syncGuest);
        const first = wrap.querySelector('select');
        if (first) first.focus({preventScroll: true});
        wrap.scrollIntoView({block: 'nearest'});
      } catch (error) {
        wrap.textContent = 'Не удалось открыть правку. Обновите страницу и попробуйте ещё раз.';
      }
      return;
    }
    if (event.target.closest('[data-seg-close]')) {
      event.preventDefault();
      close();
    }
  });

  root.addEventListener('change', event => {
    const select = event.target.closest('[data-seg-who]');
    if (select) syncGuest(select);
  });

  root.addEventListener('keydown', event => {
    if (event.key === 'Escape' && root.querySelector('.seg-editor-wrap')) close();
  });

  root.addEventListener('submit', event => {
    const button = event.target.querySelector('button:not([type="button"])');
    if (button) button.disabled = true;  // one correction per press: a double tap must not split twice
  });

  window.addEventListener('hashchange', focusReplica);
  focusReplica();
})();
