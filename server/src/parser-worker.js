'use strict';

const fs = require('node:fs');
const Module = require('node:module');

// Parser workers are intentionally unable to create outbound sockets or child processes.
const originalLoad = Module._load;
Module._load = function restrictedLoad(request, parent, isMain) {
  if (['node:net','node:dgram','node:http','node:https','node:tls','node:child_process','node:cluster','node:worker_threads'].includes(request)) {
    throw new Error(`parser worker module blocked: ${request}`);
  }
  return originalLoad.call(this, request, parent, isMain);
};
const { parseDocument } = require('./parsers');

async function main() {
  const inputPath = process.argv[2];
  const outputPath = process.argv[3];
  const maxOutputBytes = Number(process.argv[4] || 100 * 1024 * 1024);
  if (!inputPath || !outputPath) throw new Error('parser worker requires input and output paths');
  const input = fs.readFileSync(inputPath);
  const result = await parseDocument(input, process.argv[5] || 'file');
  const serialized = JSON.stringify(result);
  if (Buffer.byteLength(serialized) > maxOutputBytes) throw new Error('parser output exceeds configured budget');
  const temporary = `${outputPath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, serialized, { flag: 'wx', mode: 0o600 });
  fs.renameSync(temporary, outputPath);
}

main().then(() => process.exit(0)).catch(error => {
  process.stderr.write(JSON.stringify({ code: error.code || 'PARSER_WORKER_FAILED', message: error.message }) + '\n');
  process.exit(1);
});
