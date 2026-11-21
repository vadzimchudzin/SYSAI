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
  auto: {
    get: () => call('auto:get'),
    set: (on, project) => call('auto:set', Boolean(on), project || ''),
    recent: () => call('auto:recent'),
    undo: (id) => call('auto:undo', id),
    onState: on('auto:state'),
    onToggle: on('auto:toggle'),
  },
  fs: {
    places: () => call('fs:places'),
    drives: () => call('fs:drives'),
    list: (dir) => call('fs:list', dir),
    info: (path) => call('fs:info', path),
    text: (path) => call('fs:text', path),
    bytes: (path) => call('fs:bytes', path),
    url: (path) => call('fs:url', path),
    attachments: (paths) => call('fs:attachments', paths),
    paste: (name, bytes) => call('fs:paste', name, bytes),
  },
  shell: {
    open: (path) => call('shell:open', path),
    reveal: (path) => call('shell:reveal', path),
    run: (path) => call('shell:run', path),
    external: (url) => call('shell:external', url),
  },
  // «Открыть в программе»: detect — что в папке проекта есть (по файлу на формат), open — открыть среду вендора
  // и положить туда результат (у Haiwell — программой в буфер обмена).
  apps: {
    detect: (dir) => call('apps:detect', dir),
    plan: (dir, facts) => call('apps:plan', dir, facts || {}),
    open: (id, file) => call('apps:open', id, file),
  },
  dialog: { files: (opts) => call('dialog:files', opts || {}) },
  // «Проверить обновление»: check — что предлагает SYSAI и что стоит здесь, now — попросить синхронизатор
  // поставить это сейчас (окно само ничего не устанавливает).
  updates: {
    check: () => call('update:check'),
    now: () => call('update:now'),
    status: () => call('update:status'),
    onOffered: on('update:offered'), // синхронизатор услышал новую сборку: проверить сразу
  },
  // Соты (рой): включены ли они для профиля, сколько разрешено, запуск и живое состояние каждой соты
  swarm: {
    allowed: () => call('swarm:allowed'),
    state: () => call('swarm:state'),
    start: (opts) => call('swarm:start', opts || {}),
    stop: () => call('swarm:stop'),
    onState: (fn) => {
      const h = (_e, payload) => fn(payload);
      ipcRenderer.on('swarm:state', h);
      return () => ipcRenderer.removeListener('swarm:state', h);
    },
  },
  conv: { export: (data) => call('conv:export', data) },
  clipboard: { write: (text) => call('clipboard:text', String(text ?? '')) },
  storage: {
    list: (folder, q) => call('storage:list', folder || '', q || ''),
    fetch: (item) => call('storage:fetch', item),
    upload: (paths, folder) => call('storage:upload', paths, folder),
  },
  projects: {
    list: () => call('projects:list'),
    create: (name, scope) => call('projects:create', name, scope),
    info: (name) => call('projects:info', name),
    add: (name, paths) => call('projects:add', name, paths),
    tidy: (name) => call('projects:tidy', name),
    dialog: (name, dialog) => call('projects:dialog', name, dialog),
    role: (name, rel, role) => call('projects:role', name, rel, role),
    attach: (name, item, role) => call('projects:attach', name, item, role),
    // «Удалить проект»: 'trash' — в папку .trash проектов, 'erase' — стереть вместе с материалами и результатами
    delete: (name, mode) => call('projects:delete', name, mode),
  },
  // Разговоры этого компьютера: своя копия, чтобы перезапуск и обновление их не теряли
  chats: {
    list: () => call('chats:list'),
    read: (id) => call('chats:read', id),
    write: (doc) => call('chats:write', doc),
    delete: (id) => call('chats:delete', id),
  },
  // Вопрос системным окном (кнопки называет страница): ответ — номер нажатой кнопки
  ui: { confirm: (opts) => call('ui:confirm', opts) },
  canvas: {
    state: () => call('canvas:state'),
    open: (path, window) => call('canvas:open', path, window),
    pick: () => call('canvas:pick'),
    window: (ref) => call('canvas:window', ref),
    edit: (edits) => call('canvas:edit', edits),
    undo: () => call('canvas:undo'),
    reload: () => call('canvas:reload'),
    close: () => call('canvas:close'),
    easybuilder: () => call('canvas:eb'),
    pointer: (p) => ipcRenderer.send('canvas:pointer', p),
    onState: on('canvas:state'),
    onCursors: on('canvas:cursors'),
    onEdited: on('canvas:edited'),
    onFocus: on('canvas:focus'),
  },
  win: {
    overlay: (color) => call('win:overlay', color),
    logs: () => call('app:logs'),
    relaunch: () => call('app:relaunch'),
    onShow: on('ui:show'),
    onNotice: on('ui:notice'),
  },
  pathForFile: (file) => {
    try { return webUtils.getPathForFile(file) || ''; } catch { return ''; }
  },
});
