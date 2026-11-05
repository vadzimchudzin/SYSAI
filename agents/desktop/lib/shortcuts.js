'use strict';
// The shortcuts «Агент SYSAI» on the desktop and in Start, written by the window itself at installation
// (`electron.exe <app> --sysai-shortcuts`): Electron sets the AppUserModelID on them, so a pinned taskbar
// button stays the shortcut — it starts the launcher of the synchronizer, which always opens the current version.
const fs = require('node:fs');
const path = require('node:path');

const NAME = 'Агент SYSAI.lnk';
const AUMID = 'SYSAI.Agent';

function places(getPath) {
  const roaming = getPath('appData');
  return {
    desktop: path.join(getPath('desktop'), NAME),
    start: path.join(roaming, 'Microsoft', 'Windows', 'Start Menu', 'Programs', NAME),
    pinned: path.join(roaming, 'Microsoft', 'Internet Explorer', 'Quick Launch', 'User Pinned', 'TaskBar', NAME),
  };
}

// {target, args, icon, cwd}: the launcher and «desktop», or electron.exe and the app folder.
function write(shell, getPath, spec) {
  const where = places(getPath);
  const options = { target: spec.target, args: spec.args || '', cwd: spec.cwd || path.dirname(spec.target),
    icon: spec.icon || spec.target, iconIndex: 0, appUserModelId: AUMID,
    description: 'Агент SYSAI — окно агента на этом компьютере' };
  const done = [];
  for (const key of ['desktop', 'start']) {
    fs.mkdirSync(path.dirname(where[key]), { recursive: true });
    if (shell.writeShortcutLink(where[key], 'replace', options) ||
        shell.writeShortcutLink(where[key], 'create', options)) done.push(key);
  }
  if (fs.existsSync(where.pinned) && shell.writeShortcutLink(where.pinned, 'replace', options)) done.push('pinned');
  return { done, places: where };
}

module.exports = { write, places, NAME, AUMID };
