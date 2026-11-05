'use strict';
// What the window knows about this computer: the SYSAI folder the synchronizer keeps (%LOCALAPPDATA%\SYSAI),
// desktop.json it writes when it installs the window (where Hermes and sysai-agent are), agent.json (the agent,
// the server) and the Hermes profile's .env (the agent's token, put there by the synchronizer for Hermes).
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const WIN = process.platform === 'win32';

function localAppData() {
  return process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
  } catch {
    return null;
  }
}

function writeJson(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
  fs.renameSync(tmp, file);
}

function parseEnv(text) {
  const out = {};
  for (const raw of String(text || '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!m) continue;
    let value = m[2].trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    out[m[1]] = value;
  }
  return out;
}

function readEnv(file) {
  try {
    return parseEnv(fs.readFileSync(file, 'utf8'));
  } catch {
    return {};
  }
}

// The SYSAI folder: SYSAI_HOME, else the one this app was installed into (<home>\desktop\app-<version>),
// else the standard place.
function sysaiHome(appRoot) {
  if (process.env.SYSAI_HOME) return process.env.SYSAI_HOME;
  if (appRoot) {
    const guess = path.resolve(appRoot, '..', '..');
    if (fs.existsSync(path.join(guess, 'agent.json'))) return guess;
  }
  return WIN ? path.join(localAppData(), 'SYSAI') : path.join(os.homedir(), '.sysai');
}

// As the synchronizer finds it (sysai_agent/paths.py): %LOCALAPPDATA%\hermes, ~/.hermes elsewhere.
function hermesRoot() {
  const native = WIN ? path.join(localAppData(), 'hermes') : path.join(os.homedir(), '.hermes');
  const env = (process.env.HERMES_HOME || '').trim();
  if (!env) return native;
  const rel = path.relative(native, env);
  if (!rel.startsWith('..') && !path.isAbsolute(rel)) return native;
  return path.basename(path.dirname(env)) === 'profiles' ? path.dirname(path.dirname(env)) : env;
}

function profileDir(agent, desk) {
  if (desk && desk.profile_dir) return desk.profile_dir;
  if (agent && agent.hermes_home) return agent.hermes_home;
  const profile = (agent && (agent.profile || agent.agent)) || '';
  if (!profile) return '';
  return profile === 'default' ? hermesRoot() : path.join(hermesRoot(), 'profiles', profile);
}

function load(home) {
  const desk = readJson(path.join(home, 'desktop', 'desktop.json')) || {};
  const agent = readJson(path.join(home, 'agent.json')) || {};
  const dir = profileDir(agent, desk);
  const env = dir ? readEnv(path.join(dir, '.env')) : {};
  const server = String(env.SYSAI_URL || agent.server || desk.server || '').replace(/\/+$/, '');
  return {
    home,
    connected: Boolean(agent.agent),
    agent: agent.agent || desk.agent || '',
    profile: agent.profile || agent.agent || desk.profile || '',
    profileDir: dir,
    server,
    token: env.SYSAI_AGENT_TOKEN || '',
    hermes: desk.hermes || '',
    sysaiAgent: desk.sysai_agent || '',
    launcher: desk.launcher || '',
    person: desk.person || '',
    synchronizer: desk.synchronizer || '',
  };
}

// The signed rules as the synchronizer saved them — for hints in the window: what the agent may do is checked by
// the synchronizer (its hook and MCP servers) and, for the window's own tools, by lib/rules.js.
function rules(home) {
  const signed = readJson(path.join(home, 'access.json'));
  return signed && signed.doc && typeof signed.doc.rules === 'object' ? signed.doc.rules : null;
}

module.exports = { WIN, sysaiHome, hermesRoot, profileDir, load, rules, readJson, writeJson, parseEnv, readEnv };
