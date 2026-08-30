const assert = require('node:assert/strict');
const test = require('node:test');

const { trashFileById, validFileId } = require('../electron/file-operations');

test('trash by id resolves paths, confirms, moves all targets, then removes the row', async () => {
  const calls = [];
  const result = await trashFileById(7, {
    sidecarRequest: async (requestPath, method, body) => {
      calls.push(['sidecar', requestPath, method, body]);
      if (requestPath.endsWith('/trash-targets')) {
        return { ok: true, data: { name: '资料.pdf', targets: [
          { kind: 'source', path: '/docs/a.pdf' },
          { kind: 'preserved', path: '/safe/a.pdf' },
        ] } };
      }
      return { ok: true, data: { removed: 1 } };
    },
    confirm: async (target) => { calls.push(['confirm', target]); return true; },
    trashItem: async (filePath) => { calls.push(['trash', filePath]); },
  });

  assert.deepEqual(result, { ok: true, moved: 2, removed: 1 });
  assert.deepEqual(calls.map((call) => call[0]),
    ['sidecar', 'confirm', 'trash', 'trash', 'sidecar']);
  assert.equal(calls.at(-1)[1], '/files/remove');
  assert.deepEqual(calls.at(-1)[3], { file_ids: [7] });
});

test('a partial trash failure retains the library row', async () => {
  const sidecarPaths = [];
  const result = await trashFileById(9, {
    sidecarRequest: async (requestPath) => {
      sidecarPaths.push(requestPath);
      return { ok: true, data: { name: '资料.docx', targets: [
        { kind: 'source', path: '/docs/a.docx' },
        { kind: 'preserved', path: '/safe/a.docx' },
      ] } };
    },
    confirm: async () => true,
    trashItem: async (filePath) => {
      if (filePath.startsWith('/safe')) throw new Error('busy');
    },
  });

  assert.equal(result.ok, false);
  assert.equal(result.partial, true);
  assert.equal(result.moved, 1);
  assert.equal(result.failed, 1);
  assert.deepEqual(sidecarPaths, ['/files/9/trash-targets']);
});

test('cancel and invalid ids never touch the filesystem or remove records', async () => {
  assert.equal(validFileId('../../etc/passwd'), null);
  assert.equal(validFileId(0), null);
  assert.equal(validFileId(3), 3);
  let trashed = false;
  const result = await trashFileById(3, {
    sidecarRequest: async () => ({ ok: true, data: {
      name: 'x', targets: [{ kind: 'source', path: '/docs/x' }],
    } }),
    confirm: async () => false,
    trashItem: async () => { trashed = true; },
  });
  assert.deepEqual(result, { ok: false, cancelled: true });
  assert.equal(trashed, false);
});
