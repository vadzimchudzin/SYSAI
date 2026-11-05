// Popover menus and toasts: one look for every floating list in the window.
import { h, icon } from './dom.js';

let current = null;

export function closeMenu() {
  if (!current) return;
  const { el, cleanup } = current;
  current = null;
  cleanup();
  el.remove();
}

// items: {header} | {sep: true} | {label, sub, value, checked, icon}
export function openMenu(anchor, items, { onPick, above = false, align = 'left', minWidth } = {}) {
  closeMenu();
  const el = h('div', { class: 'menu', role: 'menu' });
  const buttons = [];
  for (const item of items) {
    if (item.header) el.append(h('div', { class: 'mh', text: item.header }));
    else if (item.sep) el.append(h('hr'));
    else {
      const b = h('button', { class: 'mi', role: 'menuitem' },
        item.icon ? icon(item.icon) : null,
        h('span', { class: 'txt' }, item.label, item.sub ? h('small', { text: item.sub }) : null),
        item.checked ? h('span', { class: 'ok' }, icon('check', 'sm')) : null);
      b.addEventListener('click', (e) => { e.stopPropagation(); closeMenu(); if (onPick) onPick(item.value, item); });
      buttons.push(b);
      el.append(b);
    }
  }
  if (minWidth) el.style.minWidth = `${minWidth}px`;
  document.body.append(el);
  const r = anchor.getBoundingClientRect();
  const mw = el.offsetWidth;
  const mh = el.offsetHeight;
  let left = align === 'right' ? r.right - mw : r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
  let top = above ? r.top - mh - 6 : r.bottom + 6;
  if (!above && top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 6);
  if (above && top < 8) top = r.bottom + 6;
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  let hot = buttons.findIndex((b) => b.querySelector('.ok'));
  const mark = () => buttons.forEach((b, i) => b.classList.toggle('hot', i === hot));
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); closeMenu(); anchor.focus?.(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); hot = (hot + 1) % buttons.length; mark(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); hot = (hot - 1 + buttons.length) % buttons.length; mark(); }
    else if (e.key === 'Enter' && hot >= 0) { e.preventDefault(); buttons[hot].click(); }
  };
  const onDown = (e) => { if (!el.contains(e.target) && !anchor.contains(e.target)) closeMenu(); };
  const onBlur = () => closeMenu();
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('mousedown', onDown, true);
  window.addEventListener('blur', onBlur);
  window.addEventListener('resize', onBlur);
  current = { el, anchor, cleanup: () => {
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('mousedown', onDown, true);
    window.removeEventListener('blur', onBlur);
    window.removeEventListener('resize', onBlur);
  } };
  return el;
}

export function menuOpenFor(anchor) {
  return Boolean(current && current.anchor === anchor);
}

let toasts = null;
export function toast(text, kind = 'info', ms = 5000) {
  if (!toasts) {
    toasts = h('div', { class: 'toasts' });
    document.body.append(toasts);
  }
  const el = h('div', { class: `toast ${kind}` }, icon(kind === 'err' ? 'alert' : kind === 'ok' ? 'check' : 'info'), h('span', { text }));
  toasts.append(el);
  const close = () => el.remove();
  el.addEventListener('click', close);
  setTimeout(close, ms);
  return el;
}
