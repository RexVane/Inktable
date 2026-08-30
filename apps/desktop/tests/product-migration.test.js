const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  hasLibraryState,
  legacyUserDataDirectory,
} = require('../electron/product-migration');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ordo-product-migration-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const current = path.join(root, 'Ordo');
  const legacy = path.join(root, 'Inktable');
  fs.mkdirSync(current, { recursive: true });
  fs.mkdirSync(legacy, { recursive: true });
  return { root, current, legacy };
}

test('a real legacy library wins over auxiliary state in an empty Ordo profile', (t) => {
  const { root, current, legacy } = fixture(t);
  fs.writeFileSync(path.join(current, 'llm.enc'), 'new-key-config');
  fs.writeFileSync(path.join(legacy, 'library.db'), 'existing-library');

  assert.equal(legacyUserDataDirectory(root), legacy);
});

test('an existing Ordo library is never replaced by the legacy profile', (t) => {
  const { root, current, legacy } = fixture(t);
  fs.mkdirSync(path.join(current, 'data'), { recursive: true });
  fs.writeFileSync(path.join(current, 'data', 'library.db'), 'current-library');
  fs.writeFileSync(path.join(legacy, 'library.db'), 'legacy-library');

  assert.equal(hasLibraryState(current), true);
  assert.equal(legacyUserDataDirectory(root), null);
});

test('only a valid custom-data pointer counts as current library state', (t) => {
  const { root, current, legacy } = fixture(t);
  const custom = path.join(root, 'custom-data');
  fs.mkdirSync(custom);
  fs.writeFileSync(path.join(current, 'data-dir.json'), JSON.stringify({ dir: custom }));
  fs.writeFileSync(path.join(legacy, 'library.db'), 'legacy-library');

  assert.equal(hasLibraryState(current), false);
  assert.equal(legacyUserDataDirectory(root), legacy);

  fs.writeFileSync(path.join(custom, 'library.db'), 'current-custom-library');
  assert.equal(hasLibraryState(current), true);
  assert.equal(legacyUserDataDirectory(root), null);
});

test('a disconnected legacy custom-data pointer still outranks a new key file', (t) => {
  const { root, current, legacy } = fixture(t);
  fs.writeFileSync(path.join(current, 'llm.enc'), 'new-key-config');
  fs.writeFileSync(path.join(legacy, 'data-dir.json'), JSON.stringify({
    dir: path.join(root, 'offline-external-drive'),
  }));

  assert.equal(legacyUserDataDirectory(root), legacy);
});
