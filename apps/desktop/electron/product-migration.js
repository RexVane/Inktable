'use strict';

const fs = require('fs');
const path = require('path');

function pointerHasLibrary(controlDir) {
  try {
    const config = JSON.parse(fs.readFileSync(path.join(controlDir, 'data-dir.json'), 'utf8'));
    return config && typeof config.dir === 'string'
      && fs.existsSync(path.join(config.dir, 'library.db'));
  } catch {
    return false;
  }
}

function hasLibraryState(controlDir) {
  return fs.existsSync(path.join(controlDir, 'library.db'))
    || fs.existsSync(path.join(controlDir, 'data', 'library.db'))
    || pointerHasLibrary(controlDir);
}

function legacyUserDataDirectory(appData) {
  const current = path.join(appData, 'Ordo');
  const legacy = path.join(appData, 'Inktable');

  // A current data pointer is an explicit user choice even while its external
  // disk is offline.  Never silently fall back to an old local library in that
  // case.  Auxiliary state (llm.enc) is different: it must not hide a complete
  // legacy library after the product rename.
  if (hasLibraryState(current)
      || fs.existsSync(path.join(current, 'data-dir.json'))) return null;
  if (hasLibraryState(legacy)
      || fs.existsSync(path.join(legacy, 'data-dir.json'))) return legacy;
  if (fs.existsSync(path.join(current, 'llm.enc'))) return null;
  return fs.existsSync(path.join(legacy, 'llm.enc')) ? legacy : null;
}

function linuxDataHome(home, environment) {
  const configured = String((environment && environment.XDG_DATA_HOME) || '').trim();
  return configured && path.isAbsolute(configured)
    ? configured
    : path.join(home, '.local', 'share');
}

function defaultDataDirectory({ platform, home, userData, environment }) {
  const macLegacy = path.join(home, 'Library', 'Application Support', 'Inktable');
  let fallback;
  let candidates;
  if (platform === 'linux') {
    const dataHome = linuxDataHome(home, environment || {});
    const current = path.join(dataHome, 'Ordo');
    const legacy = path.join(dataHome, 'Inktable');
    fallback = current;
    candidates = [
      path.join(current, 'data'), current,
      // Preserve the first Linux builds, which followed Electron userData.
      path.join(userData, 'data'), userData,
      path.join(legacy, 'data'), legacy,
      // Older cross-platform builds used the macOS-shaped path everywhere.
      path.join(macLegacy, 'data'), macLegacy,
    ];
  } else {
    fallback = path.join(userData, 'data');
    candidates = [fallback, userData, path.join(macLegacy, 'data'), macLegacy];
  }

  const seen = new Set();
  for (const candidate of candidates) {
    const normalized = path.resolve(candidate);
    if (seen.has(normalized)) continue;
    seen.add(normalized);
    if (fs.existsSync(path.join(candidate, 'library.db'))) return candidate;
  }
  return fallback;
}

module.exports = {
  defaultDataDirectory,
  hasLibraryState,
  legacyUserDataDirectory,
  linuxDataHome,
};
