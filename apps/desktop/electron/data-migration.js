'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const CONTROL_FILES = new Set(['data-dir.json', 'llm.enc']);
// Old libraries can still be opened in place after the Ordo rename.  Neither
// generation of the process lock is user data, so never copy either one.
const TRANSIENT_ROOT_FILES = new Set(['ordo.lock', 'inktable.lock']);

function normalized(value) {
  const resolved = path.resolve(value);
  return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
}

function pathsOverlap(left, right) {
  const a = normalized(left);
  const b = normalized(right);
  return a === b || a.startsWith(b + path.sep) || b.startsWith(a + path.sep);
}

function hashFile(filePath) {
  const hash = crypto.createHash('sha256');
  const fd = fs.openSync(filePath, 'r');
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    while (true) {
      const read = fs.readSync(fd, buffer, 0, buffer.length, null);
      if (!read) break;
      hash.update(buffer.subarray(0, read));
    }
  } finally {
    fs.closeSync(fd);
  }
  return hash.digest('hex');
}

function makeFilter(root, controlDir) {
  const sharesControlRoot = normalized(root) === normalized(controlDir);
  return (candidate) => {
    const relative = path.relative(root, candidate);
    if (!relative) return true;
    const parts = relative.split(path.sep);
    if (parts.length === 1 && TRANSIENT_ROOT_FILES.has(parts[0])) return false;
    if (sharesControlRoot && parts.length === 1 && CONTROL_FILES.has(parts[0])) return false;
    return true;
  };
}

function buildManifest(root, filter) {
  if (!fs.existsSync(root)) return [];
  const entries = [];
  function visit(current) {
    const children = fs.readdirSync(current, { withFileTypes: true })
      .sort((a, b) => a.name.localeCompare(b.name));
    for (const child of children) {
      const absolute = path.join(current, child.name);
      if (!filter(absolute)) continue;
      const relative = path.relative(root, absolute).split(path.sep).join('/');
      if (child.isSymbolicLink()) {
        entries.push({ path: relative, type: 'symlink', target: fs.readlinkSync(absolute) });
      } else if (child.isDirectory()) {
        entries.push({ path: relative, type: 'dir' });
        visit(absolute);
      } else if (child.isFile()) {
        const stat = fs.statSync(absolute);
        entries.push({ path: relative, type: 'file', size: stat.size,
                       sha256: hashFile(absolute) });
      }
    }
  }
  visit(root);
  return entries;
}

function copyDataDirectory(oldDir, targetDir, controlDir) {
  const source = path.resolve(oldDir);
  const target = path.resolve(targetDir);
  const control = path.resolve(controlDir);
  if (pathsOverlap(source, target)) throw new Error('新旧数据目录不能互相包含');
  if (fs.existsSync(target)) throw new Error('目标数据目录已经存在');
  const stage = `${target}.migrating-${process.pid}-${Date.now()}`;
  const filter = makeFilter(source, control);
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    if (fs.existsSync(source)) {
      fs.cpSync(source, stage, {
        recursive: true,
        force: false,
        errorOnExist: true,
        verbatimSymlinks: true,
        filter,
      });
    } else {
      fs.mkdirSync(stage, { recursive: true });
    }
    const sourceManifest = buildManifest(source, filter);
    const targetManifest = buildManifest(stage, () => true);
    if (JSON.stringify(sourceManifest) !== JSON.stringify(targetManifest)) {
      throw new Error('迁移文件清单或 SHA-256 校验不一致');
    }
    fs.renameSync(stage, target);
    const files = sourceManifest.filter((item) => item.type === 'file');
    return {
      target,
      files: files.length,
      bytes: files.reduce((total, item) => total + item.size, 0),
    };
  } catch (err) {
    try { fs.rmSync(stage, { recursive: true, force: true }); } catch {}
    throw err;
  }
}

function writeJsonAtomic(filePath, payload) {
  const dir = path.dirname(filePath);
  const temp = path.join(dir, `.${path.basename(filePath)}.${process.pid}.tmp`);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(temp, JSON.stringify(payload));
  fs.renameSync(temp, filePath);
}

module.exports = { buildManifest, copyDataDirectory, pathsOverlap, writeJsonAtomic };
