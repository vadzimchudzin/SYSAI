'use strict';
// How the agent reaches this window: Hermes runs lib/uimcp.js (the MCP server the window hands to every session),
// which asks the window here — HTTP on 127.0.0.1 with a token from bridge.json in the SYSAI folder, rewritten at
// every start of the window; the handlers check each request by the signed rules (lib/rules.js). The synchronizer
// tells the window news the same way («notice»).
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const MAX_BODY = 64 * 1024;

class Bridge {
  constructor({ file, handlers = {}, version = '', info = {} }) {
    this.file = file;
    this.handlers = handlers;
    this.version = version;
    this.info = info; // written to bridge.json too: the synchronizer keeps the folders of a running window
    this.token = crypto.randomBytes(24).toString('hex');
    this.server = null;
    this.port = 0;
  }

  _authorized(req) {
    const got = Buffer.from(String(req.headers.authorization || ''));
    const want = Buffer.from(`Bearer ${this.token}`);
    return got.length === want.length && crypto.timingSafeEqual(got, want);
  }

  async _handle(req, res) {
    const send = (status, obj) => {
      res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify(obj));
    };
    if (!this._authorized(req)) return send(401, { error: 'нет доступа' });
    const route = (req.url || '').split('?')[0].replace(/^\/+/, '');
    if (req.method === 'GET' && route === 'ping') return send(200, { ok: true, version: this.version });
    if (req.method !== 'POST' || !this.handlers[route]) return send(404, { error: 'нет такого действия' });
    let size = 0;
    const chunks = [];
    for await (const chunk of req) {
      size += chunk.length;
      if (size > MAX_BODY) return send(413, { error: 'слишком большой запрос' });
      chunks.push(chunk);
    }
    let body;
    try {
      body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    } catch {
      return send(400, { error: 'нужен JSON' });
    }
    try {
      const out = await this.handlers[route](body || {});
      return send(200, { ok: true, ...(out || {}) });
    } catch (err) {
      return send(409, { error: err.message || String(err) });
    }
  }

  start() {
    return new Promise((resolve, reject) => {
      const server = http.createServer((req, res) => {
        this._handle(req, res).catch(() => { try { res.writeHead(500); res.end(); } catch { /* closed */ } });
      });
      server.on('error', reject);
      server.listen(0, '127.0.0.1', () => {
        this.server = server;
        this.port = server.address().port;
        fs.mkdirSync(path.dirname(this.file), { recursive: true });
        const tmp = `${this.file}.${process.pid}.tmp`;
        fs.writeFileSync(tmp, JSON.stringify({ ...this.info, port: this.port, token: this.token, pid: process.pid,
          version: this.version, started: new Date().toISOString() }), { encoding: 'utf8', mode: 0o600 });
        fs.renameSync(tmp, this.file);
        resolve(this.port);
      });
    });
  }

  stop() {
    if (this.server) this.server.close();
    this.server = null;
    try {
      const data = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      if (data.pid === process.pid) fs.unlinkSync(this.file);
    } catch { /* not ours or gone */ }
  }
}

module.exports = { Bridge };
