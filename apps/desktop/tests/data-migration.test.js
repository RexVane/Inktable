const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  buildManifest,
  copyDataDirectory,
  pathsOverlap,
  writeJsonAtomic,
} = require('../electron/data-migration');

test('migration copies verified data but never moves control files or the live lock', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'inktable-migration-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const control = path.join(root, 'control');
  const target = path.join(root, 'external', 'Inktable');
  fs.mkdirSync(path.join(control, 'preserved'), { recursive: true });
  fs.writeFileSync(path.join(control, 'library.db'), Buffer.from('sqlite-data'));
  fs.writeFileSync(path.join(control, 'preserved', 'copy.pdf'), Buffer.from('pdf-data'));
  fs.writeFileSync(path.join(control, 'data-dir.json'), '{"dir":"old"}');
  fs.writeFileSync(path.join(control, 'llm.enc'), 'encrypted-secret');
  fs.writeFileSync(path.join(control, 'inktable.lock'), '1234');

  const result = copyDataDirectory(control, target, control);

  assert.equal(result.files, 2);
  assert.equal(fs.readFileSync(path.join(target, 'library.db'), 'utf8'), 'sqlite-data');
  assert.equal(fs.readFileSync(path.join(target, 'preserved', 'copy.pdf'), 'utf8'), 'pdf-data');
  assert.equal(fs.existsSync(path.join(target, 'data-dir.json')), false);
  assert.equal(fs.existsSync(path.join(target, 'llm.enc')), false);
  assert.equal(fs.existsSync(path.join(target, 'inktable.lock')), false);
  assert.equal(fs.existsSync(path.join(control, 'library.db')), true,
    'old data must remain available for rollback');

  const manifest = buildManifest(target, () => true);
  assert.equal(manifest.filter((item) => item.type === 'file').length, 2);
});

test('overlapping paths and existing targets are rejected before copying', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'inktable-overlap-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal(pathsOverlap(root, path.join(root, 'child')), true);
  assert.equal(pathsOverlap(root, path.join(path.dirname(root), 'peer')), false);
  assert.throws(() => copyDataDirectory(root, path.join(root, 'child'), root), /互相包含/);
  const existing = path.join(path.dirname(root), 'existing-target');
  fs.mkdirSync(existing, { recursive: true });
  t.after(() => fs.rmSync(existing, { recursive: true, force: true }));
  assert.throws(() => copyDataDirectory(root, existing, root), /已经存在/);
});

test('the data pointer is replaced atomically without leaving a temp file', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'inktable-pointer-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = path.join(root, 'data-dir.json');
  writeJsonAtomic(config, { dir: '/new/location' });
  assert.deepEqual(JSON.parse(fs.readFileSync(config, 'utf8')), { dir: '/new/location' });
  assert.deepEqual(fs.readdirSync(root), ['data-dir.json']);
});
