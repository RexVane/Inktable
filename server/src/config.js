'use strict';

const path = require('node:path');
const os = require('node:os');

function resolveConfig(overrides = {}) {
  const projectRoot = path.resolve(__dirname, '..', '..');
  const dataRoot = path.resolve(overrides.dataRoot || process.env.ORDO_DATA_DIR || path.join(projectRoot, '.ordo-data'));
  return {
    projectRoot,
    webRoot: path.resolve(overrides.webRoot || path.join(projectRoot, 'web')),
    dataRoot,
    dbPath: path.join(dataRoot, 'metadata', 'ordo.sqlite3'),
    blobRoot: path.join(dataRoot, 'blobs'),
    artifactRoot: path.join(dataRoot, 'artifacts'),
    backupRoot: path.join(dataRoot, 'backups'),
    taskRoot: path.join(dataRoot, 'tasks'),
    runtimeRoot: path.join(dataRoot, 'runtime'),
    logRoot: path.join(dataRoot, 'logs'),
    keyPath: path.join(dataRoot, 'runtime', 'master.key'),
    host: overrides.host || process.env.ORDO_HOST || '127.0.0.1',
    port: Number(overrides.port || process.env.ORDO_PORT || 8790),
    allowRemote: overrides.allowRemote ?? process.env.ORDO_ALLOW_REMOTE === 'true',
    tlsTerminated: overrides.tlsTerminated ?? process.env.ORDO_TLS_TERMINATED === 'true',
    remoteAdminToken: overrides.remoteAdminToken || process.env.ORDO_REMOTE_ADMIN_TOKEN || '',
    allowLocalModelEndpoints: overrides.allowLocalModelEndpoints ?? process.env.ORDO_ALLOW_LOCAL_MODEL_ENDPOINTS === 'true',
    allowLocalDatabaseHosts: overrides.allowLocalDatabaseHosts ?? process.env.ORDO_ALLOW_LOCAL_DATABASE_HOSTS === 'true',
    bodyLimit: Number(overrides.bodyLimit || process.env.ORDO_BODY_LIMIT || 64 * 1024 * 1024),
    maxFileBytes: Number(overrides.maxFileBytes || process.env.ORDO_MAX_FILE_BYTES || 50 * 1024 * 1024),
    maxArchiveFiles: Number(overrides.maxArchiveFiles || process.env.ORDO_MAX_ARCHIVE_FILES || 250),
    maxArchiveBytes: Number(overrides.maxArchiveBytes || process.env.ORDO_MAX_ARCHIVE_BYTES || 200 * 1024 * 1024),
    maxArchiveCompressionRatio: Number(overrides.maxArchiveCompressionRatio || process.env.ORDO_MAX_ARCHIVE_RATIO || 100),
    parserTimeoutMs: Number(overrides.parserTimeoutMs || process.env.ORDO_PARSER_TIMEOUT_MS || 120_000),
    maxParserOutputBytes: Number(overrides.maxParserOutputBytes || process.env.ORDO_MAX_PARSER_OUTPUT_BYTES || 100 * 1024 * 1024),
    backupEncryptionVersion: 'ordo-backup-aes256gcm-v1',
    localOwnerId: 'usr_local_owner',
    localWorkspaceId: 'ws_local',
    deploymentProfile: 'web-single-node',
    appVersion: '1.0.0',
    schemaVersion: 2,
    platform: `${os.platform()}-${os.arch()}`
  };
}

module.exports = { resolveConfig };
