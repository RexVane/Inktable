'use strict';

const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const tar = require('tar-stream');
const { unzipSync } = require('fflate');
const { id, now, AppError, safeName, hash, stableJson } = require('./core');
const { extensionOf, ALLOWED_EXTENSIONS, ARCHIVE_EXTENSIONS } = require('./parsers');

function validateArchivePath(name) {
  const normalized = String(name || '').replaceAll('\\', '/');
  if (!normalized || normalized.startsWith('/') || /^[A-Za-z]:/.test(normalized) || normalized.includes('\0')) {
    throw new AppError(422, 'ARCHIVE_UNSAFE_PATH', '压缩包包含绝对路径或设备路径');
  }
  const parts = normalized.split('/').filter(Boolean);
  if (parts.some(part => part === '..' || part === '.')) throw new AppError(422, 'ARCHIVE_PATH_TRAVERSAL', '压缩包包含路径穿越条目');
  if (parts.length > 32) throw new AppError(422, 'ARCHIVE_PATH_TOO_DEEP', '压缩包路径层级超过安全预算');
  return parts.map(safeName).join('/');
}

function isNestedArchive(name) { return ARCHIVE_EXTENSIONS.has(extensionOf(name)); }

function isWithin(root, candidate) {
  const left = process.platform === 'win32' ? root.toLowerCase() : root;
  const right = process.platform === 'win32' ? candidate.toLowerCase() : candidate;
  const relative = path.relative(left, right);
  return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function throwDirectorySecurity(code, message) {
  throw new AppError(400, code, message);
}

function samePath(left, right) {
  return isWithin(left, right) && isWithin(right, left);
}

// lstat every component, rather than only the leaf: a directory entry can be
// swapped for a symlink between readdir and the eventual read.
function assertNoSymlinkComponents(target) {
  const resolved = path.resolve(target);
  const parsed = path.parse(resolved);
  let current = parsed.root;
  for (const component of resolved.slice(parsed.root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, component);
    let stat;
    try { stat = fs.lstatSync(current); }
    catch (error) { if (error.code === 'ENOENT') throwDirectorySecurity('DIRECTORY_CHANGED', '授权目录或目录项在检查期间消失'); throw error; }
    if (stat.isSymbolicLink()) throwDirectorySecurity('DIRECTORY_SYMLINK_REJECTED', '授权目录不允许包含符号链接');
  }
}

function resolveAuthorizedRoot(directory) {
  if (!directory) throwDirectorySecurity('DIRECTORY_INVALID', '授权目录不存在或不是目录');
  const requested = path.resolve(String(directory));
  let canonical;
  try {
    assertNoSymlinkComponents(requested);
    canonical = fs.realpathSync(requested);
    const stat = fs.statSync(canonical);
    if (!stat.isDirectory()) throwDirectorySecurity('DIRECTORY_INVALID', '授权目录不存在或不是目录');
    // Check the original spelling again after realpath. This closes the common
    // rename-to-symlink race before a directory walk starts.
    assertNoSymlinkComponents(requested);
    const confirmed = fs.realpathSync(requested);
    if (confirmed !== canonical) throwDirectorySecurity('DIRECTORY_CHANGED', '授权目录在检查期间发生变化');
  } catch (error) {
    if (error instanceof AppError) throw error;
    if (['ENOENT', 'ENOTDIR', 'ELOOP'].includes(error.code)) throwDirectorySecurity('DIRECTORY_INVALID', '授权目录不存在或不是目录');
    throw error;
  }
  return { requested, canonical };
}

function secureDirectoryPath(root, relativePath, { directory = false } = {}) {
  const lexical = path.resolve(root.requested, relativePath || '');
  if (!isWithin(root.requested, lexical) || (relativePath && path.isAbsolute(String(relativePath)))) {
    throwDirectorySecurity('UNSAFE_PATH', '目录项超出授权根目录');
  }
  assertNoSymlinkComponents(lexical);
  let canonical;
  try { canonical = fs.realpathSync(lexical); }
  catch (error) {
    if (['ENOENT', 'ENOTDIR', 'ELOOP'].includes(error.code)) throwDirectorySecurity('DIRECTORY_CHANGED', '目录项在检查期间发生变化');
    throw error;
  }
  if (!isWithin(root.canonical, canonical)) throwDirectorySecurity('UNSAFE_PATH', '目录项超出授权根目录');
  const stat = fs.lstatSync(lexical);
  if (stat.isSymbolicLink()) throwDirectorySecurity('DIRECTORY_SYMLINK_REJECTED', '目录导入不允许符号链接');
  if (directory ? !stat.isDirectory() : !stat.isFile()) throwDirectorySecurity('DIRECTORY_CHANGED', '目录项在检查期间发生变化');
  const confirmed = fs.realpathSync(lexical);
  if (!isWithin(root.canonical, confirmed)) throwDirectorySecurity('UNSAFE_PATH', '目录项超出授权根目录');
  return { lexical, canonical: confirmed, stat };
}

function sameFile(left, right) {
  return left.dev === right.dev && left.ino === right.ino;
}

function readAuthorizedFile(root, relativePath, expectedSize, maxBytes) {
  const checked = secureDirectoryPath(root, relativePath);
  if (checked.stat.size !== expectedSize || checked.stat.size > maxBytes) throwDirectorySecurity('DIRECTORY_CHANGED', '文件在导入期间发生变化');
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0);
  let fd;
  try {
    fd = fs.openSync(checked.lexical, flags);
    const opened = fs.fstatSync(fd);
    if (!opened.isFile() || !sameFile(opened, checked.stat)) throwDirectorySecurity('DIRECTORY_CHANGED', '文件在导入期间被替换');
    const openedPath = fs.realpathSync(checked.lexical);
    if (!isWithin(root.canonical, openedPath)) throwDirectorySecurity('UNSAFE_PATH', '目录项超出授权根目录');
    if (opened.size > maxBytes) throwDirectorySecurity('DIRECTORY_CHANGED', '文件在导入期间超过大小限制');
    const data = fs.readFileSync(fd);
    const after = fs.fstatSync(fd);
    const afterPath = fs.realpathSync(checked.lexical);
    const current = fs.lstatSync(checked.lexical);
    if (!isWithin(root.canonical, afterPath) || current.isSymbolicLink() || !sameFile(opened, after) || !sameFile(opened, current) || after.size !== data.length || data.length > maxBytes) {
      throwDirectorySecurity('DIRECTORY_CHANGED', '文件在导入期间被替换或修改');
    }
    return data;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

async function extractTar(buffer, compressed, limits) {
  const input = compressed ? zlib.gunzipSync(buffer, { maxOutputLength: limits.maxBytes }) : buffer;
  const extract = tar.extract();
  const files = [];
  const seenNames = new Set();
  let total = 0;
  await new Promise((resolve, reject) => {
    extract.on('entry', (header, stream, next) => {
      try {
        const normalized = validateArchivePath(header.name);
        if (header.type === 'symlink' || header.type === 'link' || header.type === 'character-device' || header.type === 'block-device') {
          stream.resume();
          reject(new AppError(422, 'ARCHIVE_LINK_REJECTED', '压缩包包含链接或设备文件'));
          return;
        }
        if (header.type !== 'file') { stream.resume(); stream.on('end', next); return; }
        if (seenNames.has(normalized)) { stream.resume(); stream.on('end', () => reject(new AppError(422, 'ARCHIVE_DUPLICATE_PATH', '压缩包包含规范化后的重复路径'))); return; }
        seenNames.add(normalized);
        if (files.length >= limits.maxFiles) { stream.resume(); reject(new AppError(413, 'ARCHIVE_FILE_LIMIT', '压缩包文件数量超过预算')); return; }
        const chunks = [];
        let size = 0;
        stream.on('data', chunk => {
          size += chunk.length;
          total += chunk.length;
          if (size > limits.maxFileBytes || total > limits.maxBytes) {
            stream.destroy(new AppError(413, 'ARCHIVE_SIZE_LIMIT', '压缩包展开大小超过预算'));
            return;
          }
          chunks.push(chunk);
        });
        stream.on('error', reject);
        stream.on('end', () => { files.push({ name: normalized, buffer: Buffer.concat(chunks) }); next(); });
      } catch (error) { stream.resume(); reject(error); }
    });
    extract.on('finish', resolve);
    extract.on('error', reject);
    extract.end(input);
  });
  return files;
}

function inspectZipCentralDirectory(buffer, limits) {
  const eocd = Buffer.from(buffer).lastIndexOf(Buffer.from([0x50,0x4b,0x05,0x06]));
  if (eocd < 0 || eocd + 22 > buffer.length) throw new AppError(422, 'ARCHIVE_INVALID', 'ZIP 缺少有效中央目录');
  const count = buffer.readUInt16LE(eocd + 10);
  const directorySize = buffer.readUInt32LE(eocd + 12);
  const directoryOffset = buffer.readUInt32LE(eocd + 16);
  if (count === 0xffff || directorySize === 0xffffffff || directoryOffset === 0xffffffff) throw new AppError(413, 'ARCHIVE_ZIP64_UNSUPPORTED', 'ZIP64 压缩包超出首版安全范围');
  if (count > limits.maxFiles || directoryOffset + directorySize > buffer.length) throw new AppError(413, 'ARCHIVE_FILE_LIMIT', 'ZIP 中央目录超过安全预算');
  let cursor = directoryOffset;
  const names = new Set();
  let totalCompressed = 0;
  let totalUncompressed = 0;
  let maxUncompressed = 0;
  let encrypted = false;
  for (let index = 0; index < count; index += 1) {
    if (cursor + 46 > buffer.length || buffer.readUInt32LE(cursor) !== 0x02014b50) throw new AppError(422, 'ARCHIVE_INVALID', 'ZIP 中央目录损坏');
    const flags = buffer.readUInt16LE(cursor + 8);
    const compressed = buffer.readUInt32LE(cursor + 20);
    const uncompressed = buffer.readUInt32LE(cursor + 24);
    const nameLength = buffer.readUInt16LE(cursor + 28);
    const extraLength = buffer.readUInt16LE(cursor + 30);
    const commentLength = buffer.readUInt16LE(cursor + 32);
    const nameStart = cursor + 46;
    const nameEnd = nameStart + nameLength;
    if (nameEnd + extraLength + commentLength > buffer.length) throw new AppError(422, 'ARCHIVE_INVALID', 'ZIP 中央目录条目越界');
    const rawName = buffer.subarray(nameStart, nameEnd).toString(flags & 0x800 ? 'utf8' : 'utf8');
    const normalized = validateArchivePath(rawName);
    if (names.has(normalized)) throw new AppError(422, 'ARCHIVE_DUPLICATE_PATH', '压缩包包含规范化后的重复路径');
    names.add(normalized);
    if (flags & 0x1) encrypted = true;
    totalCompressed += compressed;
    totalUncompressed += uncompressed;
    maxUncompressed = Math.max(maxUncompressed, uncompressed);
    cursor += 46 + nameLength + extraLength + commentLength;
    if (totalUncompressed > limits.maxBytes || maxUncompressed > limits.maxFileBytes) break;
  }
  if (cursor > directoryOffset + directorySize) throw new AppError(422, 'ARCHIVE_INVALID', 'ZIP 中央目录长度不一致');
  return { count, totalCompressed, totalUncompressed, maxUncompressed, encrypted };
}

async function extractArchive(buffer, filename, limits) {
  const extension = extensionOf(filename);
  let files;
  if (extension === '.zip') {
    const central = inspectZipCentralDirectory(buffer, limits);
    if (central.encrypted) throw new AppError(422, 'NEEDS_PASSWORD', '压缩包需要密码或使用了不支持的加密方式');
    if (central.totalUncompressed > limits.maxBytes || central.maxUncompressed > limits.maxFileBytes) throw new AppError(413, 'ARCHIVE_SIZE_LIMIT', '压缩包声明的展开大小超过预算');
    if (central.totalCompressed > 0 && central.totalUncompressed / central.totalCompressed > (limits.maxCompressionRatio || 100)) throw new AppError(413, 'ARCHIVE_COMPRESSION_RATIO', '压缩包声明的展开比例超过安全预算');
    let entries;
    try { entries = unzipSync(new Uint8Array(buffer)); }
    catch (error) {
      if (/encrypted|password/i.test(error.message || '')) throw new AppError(422, 'NEEDS_PASSWORD', '压缩包需要密码或使用了不支持的加密方式');
      throw new AppError(422, 'ARCHIVE_INVALID', 'ZIP 压缩包无效或已损坏');
    }
    files = [];
    const seenNames = new Set();
    let total = 0;
    for (const [name, value] of Object.entries(entries)) {
      if (name.endsWith('/')) continue;
      if (files.length >= limits.maxFiles) throw new AppError(413, 'ARCHIVE_FILE_LIMIT', '压缩包文件数量超过预算');
      const normalized = validateArchivePath(name);
      if (seenNames.has(normalized)) throw new AppError(422, 'ARCHIVE_DUPLICATE_PATH', '压缩包包含规范化后的重复路径');
      seenNames.add(normalized);
      const item = Buffer.from(value);
      total += item.length;
      if (item.length > limits.maxFileBytes || total > limits.maxBytes) throw new AppError(413, 'ARCHIVE_SIZE_LIMIT', '压缩包展开大小超过预算');
      files.push({ name: normalized, buffer: item });
    }
  } else if (extension === '.tar') files = await extractTar(buffer, false, limits);
  else if (extension === '.tar.gz' || extension === '.tgz') files = await extractTar(buffer, true, limits);
  else throw new AppError(415, 'UNSUPPORTED_FORMAT', '不是支持的压缩包格式');
  const expandedBytes = files.reduce((sum, item) => sum + item.buffer.length, 0);
  if (['.zip','.tar.gz','.tgz'].includes(extension) && buffer.length && expandedBytes / buffer.length > (limits.maxCompressionRatio || 100)) {
    throw new AppError(413, 'ARCHIVE_COMPRESSION_RATIO', '压缩包展开比例超过安全预算');
  }
  return files;
}

class IngestService {
  constructor({ db, knowledge, tasks, audit, config }) {
    this.db = db;
    this.knowledge = knowledge;
    this.tasks = tasks;
    this.audit = audit;
    this.config = config;
    tasks.register('archive.import', context => this.archiveTask(context));
    tasks.register('directory.import', context => this.directoryTask(context));
  }

  archiveImport(datasetId, filename, buffer, input = {}, workspaceId = this.config.localWorkspaceId, requestId) {
    this.knowledge.ensureDataset(datasetId, workspaceId);
    if (!Buffer.isBuffer(buffer) || !buffer.length) throw new AppError(400, 'EMPTY_FILE', '压缩包为空');
    if (buffer.length > this.config.maxFileBytes) throw new AppError(413, 'FILE_TOO_LARGE', '压缩包超过上传预算');
    const blob = this.knowledge.blobStore.put(workspaceId, buffer, 'application/octet-stream');
    const idempotencyKey = input.idempotencyKey || `archive:${datasetId}:${blob.sha256}`;
    const existing = this.db.one('SELECT * FROM tasks WHERE workspace_id=? AND idempotency_key=?', workspaceId, idempotencyKey);
    if (existing) {
      const source = this.knowledge.db.one('SELECT * FROM sources WHERE id=? AND workspace_id=?', existing.object_id, workspaceId);
      const task = this.tasks.create({ workspaceId, type: 'archive.import', objectType: 'source', objectId: existing.object_id, idempotencyKey, input: existing.input || {} });
      return { duplicate: true, source, task };
    }
    const source = this.knowledge.createSource(datasetId, { type: 'archive', name: filename, config: { originalBlobId: blob.id } }, workspaceId, requestId);
    const task = this.tasks.create({ workspaceId, type: 'archive.import', objectType: 'source', objectId: source.id,
      idempotencyKey, input: { datasetId, sourceId: source.id, filename, blobId: blob.id } });
    return { duplicate: false, source, task };
  }

  directoryPreview(directory, rules = {}) {
    const authorized = resolveAuthorizedRoot(directory);
    const root = authorized.requested;
    const excluded = (rules.exclude || []).map(item => String(item).toLowerCase());
    const maxFiles = Math.min(Number(rules.maxFiles || this.config.maxArchiveFiles), this.config.maxArchiveFiles);
    const candidates = [];
    const walk = current => {
      // Re-resolve every directory before reading it. A renamed directory can
      // otherwise be replaced with a symlink after its parent was inspected.
      const checkedDirectory = secureDirectoryPath(authorized, path.relative(root, current), { directory: true });
      for (const entry of fs.readdirSync(checkedDirectory.lexical, { withFileTypes: true })) {
        if (candidates.length >= maxFiles) return;
        const absolute = path.join(current, entry.name);
        const relative = path.relative(root, absolute).replaceAll('\\', '/');
        if (excluded.some(pattern => relative.toLowerCase().includes(pattern))) continue;
        if (entry.isSymbolicLink()) continue;
        if (entry.isDirectory()) walk(absolute);
        else if (entry.isFile()) {
          const checkedFile = secureDirectoryPath(authorized, relative);
          const extension = extensionOf(entry.name);
          candidates.push({ relativePath: relative, sizeBytes: checkedFile.stat.size, supported: ALLOWED_EXTENSIONS.has(extension) && !ARCHIVE_EXTENSIONS.has(extension), extension });
        }
      }
    };
    walk(root);
    return { root, count: candidates.length, totalBytes: candidates.reduce((sum, item) => sum + item.sizeBytes, 0), truncated: candidates.length >= maxFiles, candidates };
  }

  directoryImport(datasetId, input, workspaceId = this.config.localWorkspaceId, requestId) {
    this.knowledge.ensureDataset(datasetId, workspaceId);
    const preview = this.directoryPreview(input.directory, input.rules || {});
    const idempotencyKey = input.idempotencyKey || `directory:${datasetId}:${hash(stableJson({ root: preview.root, rules: input.rules || {} }))}`;
    const existing = this.db.one('SELECT * FROM tasks WHERE workspace_id=? AND idempotency_key=?', workspaceId, idempotencyKey);
    if (existing) {
      const source = this.knowledge.db.one('SELECT * FROM sources WHERE id=? AND workspace_id=?', existing.object_id, workspaceId);
      const task = this.tasks.create({ workspaceId, type: 'directory.import', objectType: 'source', objectId: existing.object_id, idempotencyKey, input: existing.input || {} });
      return { duplicate: true, source, preview, task };
    }
    const authorized = resolveAuthorizedRoot(preview.root);
    const source = this.knowledge.createSource(datasetId, { type: 'directory', name: path.basename(preview.root), locationHint: preview.root, config: { rules: input.rules || {}, authorizedAt: now() } }, workspaceId, requestId);
    const task = this.tasks.create({ workspaceId, type: 'directory.import', objectType: 'source', objectId: source.id,
      idempotencyKey, input: { datasetId, sourceId: source.id, root: preview.root, rootRealpath: authorized.canonical, candidates: preview.candidates, rules: input.rules || {} } });
    return { duplicate: false, source, preview, task };
  }

  async archiveTask({ workspaceId, input, checkpoint }) {
    checkpoint(5, '验证压缩包安全预算');
    const blob = this.knowledge.blobStore.get(input.blobId, workspaceId);
    const files = await extractArchive(blob.buffer, input.filename, { maxFiles: this.config.maxArchiveFiles, maxBytes: this.config.maxArchiveBytes, maxFileBytes: this.config.maxFileBytes, maxCompressionRatio: this.config.maxArchiveCompressionRatio });
    const manifest = [];
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      const extension = extensionOf(file.name);
      if (isNestedArchive(file.name)) {
        manifest.push({ path: file.name, sizeBytes: file.buffer.length, status: 'nested_archive_not_expanded' });
      } else if (!ALLOWED_EXTENSIONS.has(extension)) {
        manifest.push({ path: file.name, sizeBytes: file.buffer.length, status: 'unsupported' });
      } else {
        try {
          const registered = await this.knowledge.registerUpload(input.datasetId, input.sourceId, file.name, file.buffer, null, workspaceId);
          manifest.push({ path: file.name, sizeBytes: file.buffer.length, status: registered.duplicate ? 'duplicate' : 'queued', documentId: registered.document.id, taskId: registered.task?.id });
        } catch (error) {
          manifest.push({ path: file.name, sizeBytes: file.buffer.length, status: 'failed', code: error.code || 'IMPORT_FAILED', message: error.message });
        }
      }
      checkpoint(10 + (index + 1) / Math.max(files.length, 1) * 85, '登记压缩包文件', { processed: index + 1, total: files.length });
    }
    const failures = manifest.filter(item => ['failed','unsupported','nested_archive_not_expanded'].includes(item.status));
    this.db.run("UPDATE sources SET status=?,config_json=?,updated_at=? WHERE id=? AND workspace_id=?", failures.length ? 'partial' : 'queued', JSON.stringify({ archiveManifest: manifest }), now(), input.sourceId, workspaceId);
    this.audit.append({ workspaceId, action: 'archive.import', objectType: 'source', objectId: input.sourceId, details: { files: files.length, failures: failures.length } });
    return { status: failures.length ? 'partial' : 'succeeded', files: files.length, failures: failures.length, manifest };
  }

  async directoryTask({ workspaceId, input, checkpoint }) {
    const authorized = resolveAuthorizedRoot(input.root);
    if (input.rootRealpath && !samePath(authorized.canonical, path.resolve(input.rootRealpath))) {
      throwDirectorySecurity('DIRECTORY_CHANGED', '授权目录在导入前已被替换');
    }
    // Use the candidate snapshot captured during authorization. Re-previewing
    // here would turn a queued import into a new authorization decision.
    const candidates = Array.isArray(input.candidates)
      ? input.candidates
      : this.directoryPreview(authorized.requested, input.rules || {}).candidates;
    const preview = { candidates };
    const manifest = [];
    for (let index = 0; index < preview.candidates.length; index += 1) {
      const item = preview.candidates[index];
      if (!item.supported) manifest.push({ path: item.relativePath, status: 'unsupported', sizeBytes: item.sizeBytes });
      else if (item.sizeBytes > this.config.maxFileBytes) manifest.push({ path: item.relativePath, status: 'resource_limit', sizeBytes: item.sizeBytes });
      else {
        try {
          const buffer = readAuthorizedFile(authorized, item.relativePath, item.sizeBytes, this.config.maxFileBytes);
          const registered = await this.knowledge.registerUpload(input.datasetId, input.sourceId, item.relativePath, buffer, null, workspaceId);
          manifest.push({ path: item.relativePath, status: registered.duplicate ? 'duplicate' : 'queued', documentId: registered.document.id, taskId: registered.task?.id });
        } catch (error) { manifest.push({ path: item.relativePath, status: 'failed', code: error.code || 'IMPORT_FAILED', message: error.message }); }
      }
      checkpoint(5 + (index + 1) / Math.max(preview.candidates.length, 1) * 90, '导入授权目录', { processed: index + 1, total: preview.candidates.length });
    }
    const failures = manifest.filter(item => ['failed','unsupported','resource_limit'].includes(item.status));
    this.db.run("UPDATE sources SET status=?,config_json=?,updated_at=? WHERE id=? AND workspace_id=?", failures.length ? 'partial' : 'queued', JSON.stringify({ root: authorized.requested, rules: input.rules || {}, manifest }), now(), input.sourceId, workspaceId);
    this.audit.append({ workspaceId, action: 'directory.import', objectType: 'source', objectId: input.sourceId, details: { files: manifest.length, failures: failures.length } });
    return { status: failures.length ? 'partial' : 'succeeded', files: manifest.length, failures: failures.length, manifest };
  }
}

module.exports = { IngestService, extractArchive, validateArchivePath };
