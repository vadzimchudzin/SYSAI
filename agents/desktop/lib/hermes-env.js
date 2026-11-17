'use strict';

const fs = require('node:fs');
const path = require('node:path');

// The compatibility shim is staged by sysai-agent sync. Keep it scoped to the
// ACP Hermes child: normal shell Hermes sessions and unrelated Python tools are untouched.
function withAcpMcpPatch(env, home) {
  const next = { ...env };
  const patchDir = path.join(home, 'hermes-acp-patch');
  try {
    if (!fs.statSync(path.join(patchDir, 'sitecustomize.py')).isFile()) return next;
  } catch {
    return next;
  }
  next.PYTHONPATH = [patchDir, String(env.PYTHONPATH || '')].filter(Boolean).join(path.delimiter);
  next.SYSAI_HERMES_ACP_PATCH = '1';
  return next;
}

module.exports = { withAcpMcpPatch };
