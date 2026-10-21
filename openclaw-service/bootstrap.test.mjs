import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { buildConfig, writeBootstrap } from './bootstrap.mjs';
function env() {
  return {
    OPENCLAW_GATEWAY_TOKEN: 'test-only-not-a-real-token-0000000000000000',
    SYSAI_BASE_URL: 'https://backend.example.test',
    SYSAI_MCP_TOKEN: 'test-only-mcp-secret', SYSAI_MODEL_PROXY_TOKEN: 'test-only-proxy-secret',
    SYSAI_MODEL_ID: 'test/model',
    SYSAI_MCP_TOOLS_JSON: JSON.stringify([{ name: 'memory_read', category: 'memory' }, { name: 'task_read', category: 'tasks' }])
  };
}
test('native MCP fixed transport and provider route', () => {
  const { config } = buildConfig(env());
  assert.equal(config.mcp.servers.sysai.transport, 'streamable-http');
  assert.equal(config.mcp.servers.sysai.url, 'https://backend.example.test/execution/mcp');
  assert.equal(config.models.providers['sysai-proxy'].baseUrl, 'https://backend.example.test/v1');
  assert.equal(config.models.providers['sysai-proxy'].api, 'openai-completions');
  assert.equal(config.agents.defaults.model.primary, 'sysai-proxy/test/model');
});
test('exact approved names only, no wildcard/plugin broad grants', () => {
  const { config } = buildConfig(env());
  assert.equal(config.tools.profile, 'minimal');
  assert.deepEqual(config.tools.alsoAllow, ['sysai__memory_read', 'sysai__task_read']);
  assert.deepEqual(config.mcp.servers.sysai.toolFilter.include, ['memory_read', 'task_read']);
  assert(config.tools.deny.includes('session_status'));
  for (const name of ['exec', 'process', 'read', 'write', 'browser', 'web_fetch', 'sessions_spawn']) assert(config.tools.deny.includes(name));
  assert.equal(config.tools.codeMode.enabled, false);
  assert.equal(config.tools.elevated.enabled, false);
  assert.deepEqual(config.channels, {});
});
test('gateway auth and Responses only', () => {
  const { config, port } = buildConfig({ ...env(), PORT: '12345' });
  assert.equal(port, 12345); assert.equal(config.gateway.port, port);
  assert.equal(config.gateway.bind, 'lan'); assert.equal(config.gateway.auth.mode, 'token');
  assert.equal(config.gateway.http.endpoints.responses.enabled, true);
  assert.equal(config.gateway.controlUi.enabled, false);
});
test('source config contains secret references, not secret values', () => {
  const e = env(); const serialized = JSON.stringify(buildConfig(e).config);
  for (const name of ['SYSAI_MCP_TOKEN', 'SYSAI_MODEL_PROXY_TOKEN', 'OPENCLAW_GATEWAY_TOKEN']) assert(!serialized.includes(e[name]));
  assert(serialized.includes('${SYSAI_MCP_AUTHORIZATION}'));
  assert(serialized.includes('SYSAI_MODEL_PROXY_TOKEN'));
});
test('reject arbitrary origins, credentials and path injection', () => {
  for (const url of ['http://backend.example.test', 'https://user:pass@backend.example.test', 'https://backend.example.test/path', 'https://backend.example.test/?token=x', 'https://localhost']) {
    assert.throws(() => buildConfig({ ...env(), SYSAI_BASE_URL: url }));
  }
});
test('reject missing credentials, empty/broad/forbidden tool grants', () => {
  for (const key of ['OPENCLAW_GATEWAY_TOKEN', 'SYSAI_MCP_TOKEN', 'SYSAI_MODEL_PROXY_TOKEN', 'SYSAI_MODEL_ID', 'SYSAI_MCP_TOOLS_JSON']) assert.throws(() => buildConfig({ ...env(), [key]: '' }));
  for (const manifest of [[], [{ name: '*', category: 'memory' }], [{ name: 'exec', category: 'memory' }], [{ name: 'admin', category: 'arbitrary' }], [{ name: 'task_read', category: 'tasks' }]]) {
    assert.throws(() => buildConfig({ ...env(), SYSAI_MCP_TOOLS_JSON: JSON.stringify(manifest) }));
  }
});
test('reject invalid port/model metadata', () => {
  for (const port of ['0', '22', '10000x', '65536']) assert.throws(() => buildConfig({ ...env(), PORT: port }));
  assert.throws(() => buildConfig({ ...env(), SYSAI_MODEL_ID: 'model\ninvalid' }));
});
test('bootstrap writes restrictive files and ephemeral-memory instructions', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openclaw-bootstrap-'));
  try {
    const built = writeBootstrap({ ...env(), OPENCLAW_STATE_DIR: dir });
    const config = JSON.parse(fs.readFileSync(path.join(dir, 'openclaw.json'), 'utf8'));
    assert.equal(config.gateway.auth.mode, 'token');
    assert.equal(fs.statSync(path.join(dir, 'openclaw.json')).mode & 0o777, 0o600);
    const instructions = fs.readFileSync(path.join(built.workspace, 'AGENTS.md'), 'utf8');
    assert(instructions.includes('ephemeral')); assert(instructions.includes('sole Telegram consumer'));
    assert(instructions.includes('actual evidence')); assert(!instructions.includes(env().SYSAI_MCP_TOKEN));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('headless free runtime allows only required provider and disables local memory', () => {
  const { config } = buildConfig(env());
  assert.deepEqual(config.plugins.allow, ['openai']);
  assert.equal(config.plugins.slots.memory, 'none');
  assert.equal(config.plugins.entries.openai.enabled, true);
  assert.equal(config.models.providers['sysai-proxy'].models[0].maxTokens, 2048);
});
test('fresh runtime starts official gateway directly without doctor preflight', () => {
  const source = fs.readFileSync(new URL('./start.sh', import.meta.url), 'utf8');
  assert(source.includes('exec node /app/openclaw.mjs gateway'));
  assert(!source.includes('/app/docker-entrypoint.mjs'));
  assert(source.includes('--max-old-space-size=256'));
});
