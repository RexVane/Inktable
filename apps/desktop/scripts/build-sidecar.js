#!/usr/bin/env node
'use strict';

/** Build the platform sidecar before electron-builder.
 *
 * Packaging without services/api/dist used to succeed and produce an app that
 * could never start. Keep this as an explicit child process (rather than a
 * shell-specific npm command) so Windows and macOS share the same release path.
 */
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const desktopDir = path.resolve(__dirname, '..');
const apiDir = path.resolve(desktopDir, '..', '..', 'services', 'api');
const binary = path.join(apiDir, 'dist', process.platform === 'win32'
  ? 'ordo-sidecar.exe' : 'ordo-sidecar');

const uv = process.platform === 'win32' ? 'uv.exe' : 'uv';
const result = spawnSync(
  uv,
  ['run', '--group', 'dev', 'pyinstaller', 'sidecar.spec', '--clean', '--noconfirm'],
  { cwd: apiDir, stdio: 'inherit', shell: false },
);
if (result.error) {
  console.error(`[dist] 无法启动 ${uv}: ${result.error.message}`);
  process.exit(1);
}
if (result.status !== 0) process.exit(result.status || 1);
if (!fs.existsSync(binary) || fs.statSync(binary).size === 0) {
  console.error(`[dist] PyInstaller 未生成预期 sidecar: ${binary}`);
  process.exit(1);
}
console.log(`[dist] sidecar ready: ${binary}`);
