'use strict';
// What the page may ask of the main process — nothing more: the page has no Node, no require, no file:// writes.
// Every call answers {ok, value} or {ok: false, error}; here an error becomes an exception with Russian text.
const { contextBridge, ipcRenderer, webUtils } = require('electron');

async function call(channel, ...args) {
  const res = await ipcRenderer.invoke(channel, ...args);
  if (!res || !res.ok) throw new Error((res && res.error) || 'нет ответа');
  return res.value;
}

function on(channel) {
  return (fn) => {
    const listener = (_event, payload) => fn(payload);
    ipcRenderer.on(channel, listener);
    return () => ipcRenderer.removeListener(channel, listener);
  };
}

contextBridge.exposeInMainWorld('sysai', {
  info: () => call('app:info'),
  ready: () => ipcRenderer.send('ui:ready'),
  acp: {
    state: () => call('acp:state'),
    restart: () => call('acp:restart'),
    request: (method, params) => call('acp:request', method, params),
    notify: (method, params) => call('acp:notify', method, params),
    respond: (id, result) => call('acp:respond', id, result),
    onState: on('acp:state'),
    onNotification: on('acp:notification'),
    onRequest: on('acp:request'),
    onRequestGone: on('acp:request-gone'),
  },
  fs: {
    places: () => call('fs:places'),
    drives: () => call('fs:drives'),
    list: (dir) => call('fs:list', dir),
    info: (path) => call('fs:info', path),
    text: (path) => call('fs:text', path),
    bytes: (path) => call('fs:bytes', path),
    url: (path) => call('fs:url', path),
    paste: (name, bytes) => call('fs:paste', name, bytes),
  },
  shell: {
    open: (path) => call('shell:open', path),
    reveal: (path) => call('shell:reveal', path),
    run: (path) => call('shell:run', path),
    external: (url) => call('shell:external', url),
  },
  dialog: { files: (opts) => call('dialog:files', opts || {}) },
  clipboard: { write: (text) => call('clipboard:text', String(text ?? '')) },
  storage: {
    list: (folder, q) => call('storage:list', folder || '', q || ''),
    fetch: (item) => call('storage:fetch', item),
    upload: (paths, folder) => call('storage:upload', paths, folder),
  },
  win: {
    overlay: (color) => call('win:overlay', color),
    logs: () => call('app:logs'),
    onShow: on('ui:show'),
    onNotice: on('ui:notice'),
  },
  pathForFile: (file) => {
    try { return webUtils.getPathForFile(file) || ''; } catch { return ''; }
  },
});
