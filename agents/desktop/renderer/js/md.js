// Markdown of the agent → safe HTML: marked (MIT) parses, DOMPurify (Apache-2.0) cleans; remote pictures are
// not fetched (the page's CSP), links open outside, paths open in the working pane.
import { h, icon, looksLikePath } from './dom.js';

const { marked } = window;
const DOMPurify = window.DOMPurify;

marked.setOptions({ gfm: true, breaks: true });

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('rel', 'noreferrer');
    node.removeAttribute('target');
  }
  if (node.tagName === 'IMG') {
    const src = node.getAttribute('src') || '';
    if (!/^(file:|data:image\/|blob:)/i.test(src)) node.removeAttribute('src');
    node.setAttribute('loading', 'lazy');
  }
  if (node.tagName === 'INPUT') node.setAttribute('disabled', '');
});

const PURIFY = { USE_PROFILES: { html: true }, FORBID_TAGS: ['style', 'form', 'button', 'textarea', 'select', 'iframe', 'object', 'embed'],
  FORBID_ATTR: ['style'] };

export function toHtml(text) {
  let html;
  try {
    html = marked.parse(String(text || ''));
  } catch {
    html = String(text || '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  }
  return DOMPurify.sanitize(html, PURIFY);
}

function enhance(root) {
  for (const pre of root.querySelectorAll('pre')) {
    if (pre.querySelector('.code-head')) continue;
    const code = pre.querySelector('code');
    const lang = ((code && code.className.match(/language-([\w+#-]+)/)) || [])[1] || 'текст';
    const copy = h('button', { class: 'icon-btn sm', title: 'Копировать', 'data-copy': '1' }, icon('copy', 'sm'));
    pre.prepend(h('div', { class: 'code-head' }, h('span', { text: lang }), copy));
  }
  for (const code of root.querySelectorAll('code')) {
    if (code.parentElement && code.parentElement.tagName === 'PRE') continue;
    if (looksLikePath(code.textContent)) {
      code.classList.add('path');
      code.dataset.path = code.textContent.trim();
      code.title = 'Открыть в окне';
    }
  }
  for (const a of root.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    if (/^file:/i.test(href) || looksLikePath(decodeURIComponent(href))) {
      a.classList.add('path');
      a.dataset.path = fileUrlToPath(href);
    }
  }
  return root;
}

export function fileUrlToPath(href) {
  let s = String(href || '');
  if (!/^file:/i.test(s)) return decodeURIComponent(s);
  s = decodeURIComponent(s.replace(/^file:\/\/(localhost)?/i, ''));
  if (/^\/[a-zA-Z]:\//.test(s)) s = s.slice(1).replace(/\//g, '\\');
  return s;
}

export function render(el, text) {
  el.innerHTML = toHtml(text);
  enhance(el);
  return el;
}
