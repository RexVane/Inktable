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

function profilePriority(controlDir) {
  if (hasLibraryState(controlDir)) return 3;
  // Keep a disconnected custom-library pointer ahead of auxiliary settings.
  // The external drive may simply be offline during this launch.
  if (fs.existsSync(path.join(controlDir, 'data-dir.json'))) return 2;
  if (fs.existsSync(path.join(controlDir, 'llm.enc'))) return 1;
  return 0;
}

function legacyUserDataDirectory(appData) {
  const current = path.join(appData, 'Ordo');
  const legacy = path.join(appData, 'Inktable');

  // A real library outranks a custom-data pointer, which in turn outranks an
  // auxiliary key file.  Ties stay on the new profile.  Without this ranking,
  // launching Ordo once and creating only llm.enc can make the next launch
  // ignore the user's complete Inktable library.
  return profilePriority(legacy) > profilePriority(current) ? legacy : null;
}

module.exports = { hasLibraryState, legacyUserDataDirectory };
