import os
import platform
from pathlib import Path


def resolve_config(overrides=None):
    overrides = overrides or {}
    project_root = Path(__file__).resolve().parent.parent.parent
    data_root = Path(overrides.get('dataRoot') or os.environ.get('ORDO_DATA_DIR') or (project_root / '.ordo-data')).resolve()
    return {
        'projectRoot': project_root,
        'webRoot': Path(overrides.get('webRoot') or (project_root / 'web')).resolve(),
        'dataRoot': data_root,
        'dbPath': data_root / 'metadata' / 'ordo.sqlite3',
        'blobRoot': data_root / 'blobs',
        'artifactRoot': data_root / 'artifacts',
        'backupRoot': data_root / 'backups',
        'taskRoot': data_root / 'tasks',
        'runtimeRoot': data_root / 'runtime',
        'logRoot': data_root / 'logs',
        'keyPath': data_root / 'runtime' / 'master.key',
        'host': overrides.get('host') or os.environ.get('ORDO_HOST') or '127.0.0.1',
        'port': int(overrides.get('port') or os.environ.get('ORDO_PORT') or 8790),
        'allowRemote': bool(overrides.get('allowRemote', None) if overrides.get('allowRemote', None) is not None else os.environ.get('ORDO_ALLOW_REMOTE') == 'true'),
        'tlsTerminated': bool(overrides.get('tlsTerminated', None) if overrides.get('tlsTerminated', None) is not None else os.environ.get('ORDO_TLS_TERMINATED') == 'true'),
        'remoteAdminToken': overrides.get('remoteAdminToken') or os.environ.get('ORDO_REMOTE_ADMIN_TOKEN') or '',
        'allowLocalModelEndpoints': bool(overrides.get('allowLocalModelEndpoints', None) if overrides.get('allowLocalModelEndpoints', None) is not None else os.environ.get('ORDO_ALLOW_LOCAL_MODEL_ENDPOINTS') == 'true'),
        'allowLocalDatabaseHosts': bool(overrides.get('allowLocalDatabaseHosts', None) if overrides.get('allowLocalDatabaseHosts', None) is not None else os.environ.get('ORDO_ALLOW_LOCAL_DATABASE_HOSTS') == 'true'),
        'bodyLimit': int(overrides.get('bodyLimit') or os.environ.get('ORDO_BODY_LIMIT') or 64 * 1024 * 1024),
        'maxFileBytes': int(overrides.get('maxFileBytes') or os.environ.get('ORDO_MAX_FILE_BYTES') or 50 * 1024 * 1024),
        'maxArchiveFiles': int(overrides.get('maxArchiveFiles') or os.environ.get('ORDO_MAX_ARCHIVE_FILES') or 250),
        'maxArchiveBytes': int(overrides.get('maxArchiveBytes') or os.environ.get('ORDO_MAX_ARCHIVE_BYTES') or 200 * 1024 * 1024),
        'maxArchiveCompressionRatio': int(overrides.get('maxArchiveCompressionRatio') or os.environ.get('ORDO_MAX_ARCHIVE_RATIO') or 100),
        'parserTimeoutMs': int(overrides.get('parserTimeoutMs') or os.environ.get('ORDO_PARSER_TIMEOUT_MS') or 120_000),
        'maxParserOutputBytes': int(overrides.get('maxParserOutputBytes') or os.environ.get('ORDO_MAX_PARSER_OUTPUT_BYTES') or 100 * 1024 * 1024),
        'backupEncryptionVersion': 'ordo-backup-aes256gcm-v1',
        'localOwnerId': 'usr_local_owner',
        'localWorkspaceId': 'ws_local',
        'deploymentProfile': 'web-single-node',
        'appVersion': '1.0.0',
        'schemaVersion': 5,
        'platform': f'{platform.system().lower()}-{platform.machine().lower()}',
    }
