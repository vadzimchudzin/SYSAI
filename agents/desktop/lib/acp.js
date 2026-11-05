'use strict';
// The window talks to Hermes the way editors do: ACP (Agent Client Protocol) — JSON-RPC 2.0, one message per
// line, over the stdio of `hermes -p <profile> acp`. Hermes 0.22 runs the work in its gateway daemon; this
// process is its viewer, so closing the window does not stop a task the gateway carries on.
const { spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');

const PROTOCOL_VERSION = 1;
const TAIL = 64 * 1024;          // what is kept of stderr, to explain a crash
const MAX_LINE = 64 * 1024 * 1024; // a message with a big image in it is still one line

class AcpError extends Error {
  constructor(message, code = -32603, data = undefined) {
    super(message);
    this.code = code;
    this.data = data;
  }
}

// Windows refuses to start a .cmd/.bat directly (Node, CVE-2024-27980): it runs through cmd.exe.
function commandLine(command, args, platform = process.platform) {
  if (platform === 'win32' && /\.(cmd|bat)$/i.test(command)) {
    const quote = (s) => (/[\s"&|<>^()]/.test(s) ? `"${String(s).replace(/"/g, '""')}"` : String(s));
    const line = [command, ...args].map(quote).join(' ');
    return { file: process.env.ComSpec || 'cmd.exe', args: ['/d', '/s', '/c', `"${line}"`], verbatim: true };
  }
  return { file: command, args, verbatim: false };
}

class AcpClient extends EventEmitter {
  constructor({ command, args = [], env = process.env, cwd, spawnImpl = spawn } = {}) {
    super();
    this.command = command;
    this.args = args;
    this.env = env;
    this.cwd = cwd;
    this.spawnImpl = spawnImpl;
    this.child = null;
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = '';
    this.stderr = '';
    this.exited = null;
  }

  start() {
    if (this.child) return;
    const { file, args, verbatim } = commandLine(this.command, this.args);
    const child = this.spawnImpl(file, args, {
      cwd: this.cwd, env: this.env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
      windowsVerbatimArguments: verbatim,
    });
    this.child = child;
    this.exited = null;
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => this._data(chunk));
    child.stderr.on('data', (chunk) => {
      this.stderr = (this.stderr + chunk).slice(-TAIL);
      this.emit('stderr', chunk);
    });
    child.stdin.on('error', () => {}); // the process is gone: «exit» says so
    child.on('error', (err) => this._exit(null, null, err));
    child.on('exit', (code, signal) => this._exit(code, signal));
  }

  _exit(code, signal, err) {
    if (this.exited) return;
    this.exited = { code, signal, error: err ? err.message : '' };
    this.child = null;
    const why = err ? `не удалось запустить Hermes: ${err.message}` : `Hermes остановился (код ${code ?? signal})`;
    for (const { reject, timer } of this.pending.values()) {
      if (timer) clearTimeout(timer);
      reject(new AcpError(why, -32000));
    }
    this.pending.clear();
    this.emit('exit', { ...this.exited, stderr: this.stderr });
  }

  _data(chunk) {
    this.buffer += chunk;
    if (this.buffer.length > MAX_LINE) this.buffer = this.buffer.slice(-MAX_LINE);
    let nl;
    while ((nl = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, nl).trim();
      this.buffer = this.buffer.slice(nl + 1);
      if (line) this._line(line);
    }
  }

  _line(line) {
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      this.emit('noise', line); // not ours: a library printed to stdout
      return;
    }
    if (!msg || typeof msg !== 'object' || msg.jsonrpc !== '2.0') return;
    if (msg.method && msg.id !== undefined && msg.id !== null) {
      this.emit('request', { id: msg.id, method: msg.method, params: msg.params || {} });
    } else if (msg.method) {
      this.emit('notification', { method: msg.method, params: msg.params || {} });
    } else if (msg.id !== undefined && this.pending.has(msg.id)) {
      const { resolve, reject, timer } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (timer) clearTimeout(timer);
      if (msg.error) reject(new AcpError(msg.error.message || 'ошибка Hermes', msg.error.code, msg.error.data));
      else resolve(msg.result === undefined ? null : msg.result);
    }
  }

  _send(obj) {
    if (!this.child || !this.child.stdin.writable) throw new AcpError('Hermes не запущен', -32000);
    this.child.stdin.write(JSON.stringify(obj) + '\n');
  }

  request(method, params = {}, { timeout = 0 } = {}) {
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      let timer = null;
      if (timeout > 0) {
        timer = setTimeout(() => {
          this.pending.delete(id);
          reject(new AcpError(`Hermes не ответил за ${Math.round(timeout / 1000)} с (${method})`, -32001));
        }, timeout);
      }
      this.pending.set(id, { resolve, reject, timer });
      try {
        this._send({ jsonrpc: '2.0', id, method, params });
      } catch (err) {
        this.pending.delete(id);
        if (timer) clearTimeout(timer);
        reject(err);
      }
    });
  }

  notify(method, params = {}) {
    this._send({ jsonrpc: '2.0', method, params });
  }

  respond(id, result) {
    this._send({ jsonrpc: '2.0', id, result: result === undefined ? null : result });
  }

  respondError(id, code, message) {
    this._send({ jsonrpc: '2.0', id, error: { code, message } });
  }

  initialize(clientInfo, { elicitation = true, timeout = 90000 } = {}) {
    const clientCapabilities = { fs: { readTextFile: false, writeTextFile: false }, terminal: false };
    if (elicitation) clientCapabilities.elicitation = { form: {} };
    return this.request('initialize', { protocolVersion: PROTOCOL_VERSION, clientCapabilities, clientInfo },
      { timeout });
  }

  stop() {
    const child = this.child;
    if (!child) return;
    try { child.stdin.end(); } catch { /* gone */ }
    setTimeout(() => { if (this.child === child) child.kill(); }, 1500).unref?.();
  }

  get running() {
    return Boolean(this.child);
  }
}

module.exports = { AcpClient, AcpError, PROTOCOL_VERSION, commandLine };
