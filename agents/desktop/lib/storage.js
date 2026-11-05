'use strict';
// SYSAI's storage (Хранилище) as the agent sees it: the sections opened to it in «Доступы», by the agent's own
// token (the same API the synchronizer and Hermes use, /agents/api/files). A file is fetched into the window's
// cache and checked against its sha256 before it is shown.
const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');

const TIMEOUT = 60000;
const MAX_UPLOAD = 200 * 1024 * 1024;

class StorageError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.status = status;
  }
}

function need(cfg) {
  if (!cfg.server) throw new StorageError('Компьютер не подключён к SYSAI: нет адреса сервера (sysai-agent status)');
  if (!cfg.token) {
    throw new StorageError('Нет токена агента в профиле Hermes: синхронизатор его ещё не записал (sysai-agent sync)');
  }
}

async function call(cfg, method, route, { body, headers = {}, fetchImpl = globalThis.fetch, timeout = TIMEOUT } = {}) {
  need(cfg);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  let res;
  try {
    res = await fetchImpl(cfg.server + route, {
      method, body, signal: ctrl.signal, headers: { Authorization: `Bearer ${cfg.token}`, ...headers },
      ...(body && typeof body.pipe === 'function' ? { duplex: 'half' } : {}),
    });
  } catch (err) {
    throw new StorageError(err.name === 'AbortError' ? 'SYSAI не ответил вовремя' : `нет связи с SYSAI: ${err.message}`);
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let message = `SYSAI ответил ${res.status}`;
    try {
      const data = await res.json();
      if (data && data.error && data.error.message) message = data.error.message;
    } catch { /* not JSON */ }
    throw new StorageError(message, res.status);
  }
  return res;
}

async function list(cfg, folder = '', q = '', opts = {}) {
  const query = new URLSearchParams({ path: folder || '' });
  if (q) query.set('q', q);
  const res = await call(cfg, 'GET', `/agents/api/files?${query}`, opts);
  return res.json();
}

function cachePath(cacheDir, item) {
  const parts = String(item.path || '').split('/').filter((p) => p && p !== '.' && p !== '..')
    .map((p) => p.replace(/[<>:"\\|?*\u0000-\u001f]/g, '_').replace(/[. ]+$/, '_'));
  if (!parts.length) throw new StorageError('нет пути файла');
  return path.join(cacheDir, ...parts);
}

async function sha256(file) {
  const hash = crypto.createHash('sha256');
  await new Promise((resolve, reject) => {
    fs.createReadStream(file).on('data', (d) => hash.update(d)).on('end', resolve).on('error', reject);
  });
  return hash.digest('hex');
}

// The file on this computer: taken from the cache when its sha256 is the one SYSAI lists, else downloaded.
async function fetchFile(cfg, item, cacheDir, opts = {}) {
  const target = cachePath(cacheDir, item);
  if (item.sha256 && fs.existsSync(target)) {
    try {
      if (await sha256(target) === item.sha256) return target;
    } catch { /* download again */ }
  }
  const query = new URLSearchParams({ path: item.path });
  if (item.version) query.set('v', String(item.version));
  const res = await call(cfg, 'GET', `/agents/api/files/raw?${query}`, opts);
  const data = Buffer.from(await res.arrayBuffer());
  const expected = res.headers.get('x-sha256') || item.sha256 || '';
  const got = crypto.createHash('sha256').update(data).digest('hex');
  if (expected && got !== expected) throw new StorageError('файл пришёл повреждённым (sha256 не сходится) — попробуйте ещё раз');
  await fsp.mkdir(path.dirname(target), { recursive: true });
  const tmp = `${target}.${process.pid}.part`;
  await fsp.writeFile(tmp, data);
  await fsp.rename(tmp, target);
  return target;
}

async function upload(cfg, localPath, folder, opts = {}) {
  const st = await fsp.stat(localPath);
  if (st.isDirectory()) throw new StorageError('папку целиком не загрузить — выберите файлы');
  if (st.size > MAX_UPLOAD) throw new StorageError(`файл больше ${MAX_UPLOAD / 1024 / 1024} МБ`);
  const name = path.basename(localPath);
  const dest = [String(folder || '').replace(/\/+$/, ''), name].filter(Boolean).join('/');
  const query = new URLSearchParams({ path: dest, note: 'загружено из окна агента' });
  const body = await fsp.readFile(localPath);
  const res = await call(cfg, 'PUT', `/agents/api/files/raw?${query}`, {
    ...opts, body, headers: { 'Content-Type': 'application/octet-stream' }, timeout: 10 * 60000,
  });
  return res.json();
}

module.exports = { list, fetchFile, upload, cachePath, sha256, StorageError };
