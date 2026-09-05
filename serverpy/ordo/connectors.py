import asyncio
import json
import re
import socket
import time
from pathlib import Path

from .core import AppError, gen_id, hash_bytes, now, redact, required


_READ_ONLY_RE = re.compile(r'^(SELECT|WITH)\b', re.IGNORECASE)
_BANNED_RE = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|GRANT|REVOKE|COPY|CALL|EXECUTE|ATTACH|DETACH|PRAGMA|VACUUM|ANALYZE|REINDEX|LOAD_EXTENSION)\b',
    re.IGNORECASE,
)


def assert_read_only_sql(sql):
    text = str(sql or '').strip()
    if not _READ_ONLY_RE.match(text):
        raise AppError(400, 'QUERY_NOT_READ_ONLY', '查询模板只允许 SELECT 或 WITH')
    without_trailing = re.sub(r';\s*$', '', text)
    if ';' in without_trailing or re.search(r'--|/\*|\*/', without_trailing):
        raise AppError(400, 'QUERY_MULTIPLE_OR_COMMENT', '查询模板不允许多语句或 SQL 注释')
    if _BANNED_RE.search(without_trailing):
        raise AppError(400, 'QUERY_NOT_READ_ONLY', '查询模板包含禁止的数据库操作')
    return without_trailing


def _ipv4_number(address):
    parts = str(address).split('.')
    if len(parts) != 4 or any(not part.isdigit() or int(part) < 0 or int(part) > 255 for part in parts):
        return None
    return ((int(parts[0]) * 256 + int(parts[1])) * 256 + int(parts[2])) * 256 + int(parts[3])


def _blocked_private_ipv4(address):
    value = _ipv4_number(address)
    if value is None:
        return False
    return (value <= 0x00FFFFFF or (0x0A000000 <= value <= 0x0AFFFFFF)
            or (0x64400000 <= value <= 0x647FFFFF) or (0x7F000000 <= value <= 0x7FFFFFFF)
            or (0xA9FE0000 <= value <= 0xA9FEFFFF) or (0xAC100000 <= value <= 0xAC1FFFFF)
            or (0xC0A80000 <= value <= 0xC0A8FFFF) or value >= 0xE0000000)


def _dangerous_address(address):
    value = str(address or '').strip('[]').lower()
    mapped = re.match(r'^::ffff:(\d+\.\d+\.\d+\.\d+)$', value)
    ipv4 = mapped.group(1) if mapped else value
    if _blocked_private_ipv4(ipv4):
        return True
    try:
        socket.inet_pton(socket.AF_INET, value)
        is_ip = socket.AF_INET
    except OSError:
        try:
            socket.inet_pton(socket.AF_INET6, value)
            is_ip = socket.AF_INET6
        except OSError:
            return True
    if is_ip == socket.AF_INET:
        return False
    return (value == '::' or value == '::1' or value.startswith(('fc', 'fd'))
            or value.startswith(('fe8', 'fe9', 'fea', 'feb')) or value.startswith('ff')
            or value in ('169.254.169.254', '100.100.100.200'))


_METADATA_HOSTS = ('metadata.google.internal', 'metadata.azure.internal', 'metadata')
_METADATA_ADDRESSES = ('169.254.169.254', '100.100.100.200')


def resolve_database_host_sync(host, allow_local=False):
    value = required(host, 'host').lower()
    if value in _METADATA_HOSTS:
        raise AppError(400, 'HOST_BLOCKED', '禁止连接云元数据地址')
    try:
        infos = socket.getaddrinfo(value, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise AppError(400, 'HOST_RESOLUTION_FAILED', '数据库主机无法解析')
    records = []
    for info in infos:
        address = info[4][0]
        if address not in records:
            records.append(address)
    if not records or any(address in _METADATA_ADDRESSES for address in records):
        raise AppError(400, 'HOST_BLOCKED', '数据库主机解析到云元数据地址')
    if not allow_local and any(_dangerous_address(address) for address in records):
        raise AppError(400, 'HOST_BLOCKED', '数据库主机解析到本机或私网地址')
    return {'hostname': value, 'address': records[0]}


async def resolve_database_host(host, allow_local=False):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, resolve_database_host_sync, host, allow_local)


async def validate_database_host(host, allow_local=False):
    await resolve_database_host(host, allow_local)
    return required(host, 'host').lower()


class ConnectorService:
    def __init__(self, db, secret_store, artifact_store, knowledge, audit, config):
        self.db = db
        self.secret_store = secret_store
        self.artifact_store = artifact_store
        self.knowledge = knowledge
        self.audit = audit
        self.config = config

    def list(self, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        return self.db.all(
            """SELECT c.id,c.workspace_id,c.name,c.type,c.config_json,c.status,c.last_checked_at,c.last_error,
            c.created_at,c.updated_at,s.mask AS secret_mask
            FROM connectors c LEFT JOIN secrets s ON s.id=c.secret_ref WHERE c.workspace_id=? ORDER BY c.created_at""",
            workspace_id)

    def get(self, connector_id, workspace_id=None, include_secret=False):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        record = self.db.one('SELECT * FROM connectors WHERE id=? AND workspace_id=?', connector_id, workspace_id)
        if not record:
            raise AppError(404, 'NOT_FOUND', '数据库连接不存在或不可访问')
        if not include_secret:
            record.pop('secret_ref', None)
        return record

    async def create(self, input, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        connector_type = input.get('type')
        if connector_type not in ('sqlite', 'postgresql'):
            raise AppError(400, 'CONNECTOR_TYPE_INVALID', '仅支持 SQLite 或 PostgreSQL')
        connector_id = gen_id('conn')
        secret = None
        if connector_type == 'sqlite':
            file = Path(required(input.get('path'), 'path')).resolve()
            if not file.exists() or not file.is_file():
                raise AppError(400, 'SQLITE_FILE_INVALID', 'SQLite 文件不存在或不是普通文件')
            config = {'path': str(file), 'timeoutMs': min(int(input.get('timeoutMs') or 5000), 30000)}
        else:
            host = await validate_database_host(input.get('host'), self.config['allowLocalDatabaseHosts'])
            port = int(input.get('port') or 5432)
            if not 1 <= port <= 65535:
                raise AppError(400, 'VALIDATION_ERROR', 'port 无效')
            config = {'host': host, 'port': port, 'database': required(input.get('database'), 'database'),
                      'ssl': input.get('ssl') or False, 'timeoutMs': min(int(input.get('timeoutMs') or 10000), 30000)}
            if not input.get('username') or not input.get('password'):
                raise AppError(400, 'CREDENTIALS_REQUIRED', 'PostgreSQL 需要用户名和密码')
            secret = self.secret_store.create(workspace_id, f'connector:{connector_id}',
                                              json.dumps({'username': input['username'], 'password': input['password']}, ensure_ascii=False, separators=(',', ':')))
        timestamp = now()
        try:
            self.db.run('INSERT INTO connectors(id,workspace_id,name,type,config_json,secret_ref,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                        connector_id, workspace_id, required(input.get('name'), 'name'), connector_type,
                        json.dumps(config, ensure_ascii=False, separators=(',', ':')), secret['id'] if secret else None,
                        'unverified', timestamp, timestamp)
        except Exception as error:
            if 'UNIQUE constraint failed' in str(error):
                raise AppError(409, 'NAME_CONFLICT', '同名数据库连接已存在')
            raise
        self.audit.append(workspace_id=workspace_id, action='connector.create', object_type='connector',
                          object_id=connector_id, request_id=request_id,
                          details={'type': connector_type, 'hasSecret': bool(secret)})
        return self.get(connector_id, workspace_id)

    async def _with_connection(self, record, workspace_id, fn):
        config = record.get('config') or {}
        timeout_ms = min(int(config.get('timeoutMs') or 5000), 30000)
        if record['type'] == 'sqlite':
            import sqlite3
            try:
                connection = sqlite3.connect(Path(config['path']).as_uri() + '?mode=ro', uri=True, timeout=timeout_ms / 1000)
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                connection.execute('PRAGMA trusted_schema=OFF')
                connection.execute(f'PRAGMA busy_timeout={timeout_ms}')
                deadline = time.monotonic() + timeout_ms / 1000
                connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
            except Exception:
                raise AppError(502, 'CONNECTOR_UNREACHABLE', '无法以只读方式打开 SQLite 数据库')
            try:
                return await fn({'type': 'sqlite', 'raw': connection})
            finally:
                connection.close()
        # postgresql
        import psycopg
        credentials = record.get('_credentials') or json.loads(self.secret_store.resolve(record['secret_ref'], workspace_id))
        resolved = await resolve_database_host(config.get('host'), self.config['allowLocalDatabaseHosts'])
        try:
            connection = await psycopg.AsyncConnection.connect(
                host=resolved['address'], port=config.get('port'), dbname=config.get('database'),
                user=credentials['username'], password=credentials['password'],
                sslmode='require' if config.get('ssl') else 'disable',
                connect_timeout=max(1, timeout_ms // 1000))
        except Exception as error:
            raise AppError(502, 'CONNECTOR_QUERY_FAILED', '数据库连接或只读查询失败',
                           {'reason': redact(str(error))[:180]})

        try:
            await connection.execute('BEGIN READ ONLY')
            await connection.execute(f'SET LOCAL statement_timeout = {timeout_ms}')
            async with connection.cursor() as cursor:
                result = await fn({'type': 'postgresql', 'raw': cursor})
            await connection.execute('ROLLBACK')
            return result
        except AppError:
            await connection.close()
            raise
        except Exception as error:
            await connection.close()
            raise AppError(502, 'CONNECTOR_QUERY_FAILED', '数据库连接或只读查询失败',
                           {'reason': redact(str(error))[:180]})
        finally:
            await connection.close()

    async def test_config(self, input, workspace_id):
        connector_type = input.get('type')
        if connector_type not in ('sqlite', 'postgresql'):
            raise AppError(400, 'CONNECTOR_TYPE_INVALID', '仅支持 SQLite 或 PostgreSQL')
        config = dict(input.get('config') or input)
        if connector_type == 'sqlite':
            file = Path(required(config.get('path'), 'path')).resolve()
            if not file.is_file():
                raise AppError(400, 'SQLITE_FILE_INVALID', 'SQLite 文件不存在')
            config['path'] = str(file)
        else:
            config['host'] = await validate_database_host(config.get('host'), self.config['allowLocalDatabaseHosts'])
            config['database'] = required(config.get('database'), 'database')
            config['port'] = int(config.get('port') or 5432)
        record = {'type': connector_type, 'config': config, '_credentials': {'username': input.get('username'), 'password': input.get('password')}}
        async def probe(connection):
            if connector_type == 'sqlite':
                return connection['raw'].execute('SELECT sqlite_version()').fetchone()[0]
            await connection['raw'].execute('SELECT version()')
            return (await connection['raw'].fetchone())[0]
        started = time.monotonic()
        version = await self._with_connection(record, workspace_id, probe)
        return {'available': True, 'type': connector_type, 'version': version, 'latencyMs': round((time.monotonic()-started)*1000), 'persisted': False}

    async def test(self, connector_id, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        record = self.get(connector_id, workspace_id, include_secret=True)
        started = time.monotonic()
        config = record.get('config') or {}

        async def _probe(connection):
            if connection['type'] == 'sqlite':
                row = connection['raw'].execute('SELECT sqlite_version() AS version').fetchone()
                return {'version': row['version'], 'database': Path(config.get('path', '')).name}
            await connection['raw'].execute("SELECT current_setting('server_version') AS version, current_database() AS database")
            row = await connection['raw'].fetchone()
            return {'version': row[0], 'database': row[1]}

        try:
            result = await self._with_connection(record, workspace_id, _probe)
            response = {'available': True, 'type': record['type'],
                        'latencyMs': round((time.monotonic() - started) * 1000),
                        'version': result['version'], 'database': result['database']}
            self.db.run("UPDATE connectors SET status='available',last_checked_at=?,last_error=NULL,updated_at=? WHERE id=?",
                        now(), now(), connector_id)
            self.audit.append(workspace_id=workspace_id, action='connector.test', object_type='connector',
                              object_id=connector_id, request_id=request_id, details=response)
            return response
        except AppError as error:
            self.db.run("UPDATE connectors SET status='unavailable',last_checked_at=?,last_error=?,updated_at=? WHERE id=?",
                        now(), redact(error.message), now(), connector_id)
            raise

    async def schema(self, connector_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        record = self.get(connector_id, workspace_id, include_secret=True)

        async def _read_schema(connection):
            if connection['type'] == 'sqlite':
                objects = connection['raw'].execute(
                    "SELECT name,type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
                result = []
                for obj in objects:
                    name = obj['name']
                    columns = connection['raw'].execute(f'PRAGMA table_info("{name}")').fetchall() \
                        if obj['type'] == 'table' else connection['raw'].execute(f'PRAGMA table_xinfo("{name}")').fetchall()
                    result.append({'name': name, 'type': obj['type'], 'columns': [
                        {'name': column['name'], 'type': column['type'], 'nullable': not column['notnull'],
                         'primaryKey': bool(column['pk'])} for column in columns]})
                return result
            await connection['raw'].execute(
                """SELECT table_schema,table_name,table_type,column_name,data_type,is_nullable
                FROM information_schema.columns JOIN information_schema.tables USING(table_schema,table_name)
                WHERE table_schema NOT IN ('pg_catalog','information_schema')
                ORDER BY table_schema,table_name,ordinal_position""")
            rows = await connection['raw'].fetchall()
            mapping = {}
            for row in rows:
                key = f"{row[0]}.{row[1]}"
                if key not in mapping:
                    mapping[key] = {'schema': row[0], 'name': row[1], 'type': row[2], 'columns': []}
                mapping[key]['columns'].append({'name': row[3], 'type': row[4], 'nullable': row[5] == 'YES'})
            return list(mapping.values())

        return await self._with_connection(record, workspace_id, _read_schema)

    def create_template(self, connector_id, input, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        self.get(connector_id, workspace_id)
        template_id = gen_id('qtpl')
        timestamp = now()
        sql = assert_read_only_sql(required(input.get('sql'), 'sql'))
        row_limit = max(1, min(int(input.get('rowLimit') or 1000), 10000))
        timeout_ms = max(100, min(int(input.get('timeoutMs') or 10000), 30000))
        self.db.run('INSERT INTO database_query_templates(id,workspace_id,connector_id,name,sql_text,params_json,row_limit,timeout_ms,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    template_id, workspace_id, connector_id, required(input.get('name'), 'name'), sql,
                    json.dumps(input.get('params') or [], ensure_ascii=False), row_limit, timeout_ms, 'active', timestamp, timestamp)
        self.audit.append(workspace_id=workspace_id, action='database_template.create', object_type='database_query_template',
                          object_id=template_id, request_id=request_id, details={'connectorId': connector_id, 'rowLimit': row_limit, 'timeoutMs': timeout_ms})
        return self.get_template(template_id, workspace_id)

    def get_template(self, template_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        record = self.db.one('SELECT * FROM database_query_templates WHERE id=? AND workspace_id=?', template_id, workspace_id)
        if not record:
            raise AppError(404, 'NOT_FOUND', '查询模板不存在或不可访问')
        return record

    def list_templates(self, connector_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        return self.db.all('SELECT * FROM database_query_templates WHERE connector_id=? AND workspace_id=? ORDER BY created_at DESC', connector_id, workspace_id)

    async def execute_template(self, template_id, values=None, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        values = values or []
        template = self.get_template(template_id, workspace_id)
        record = self.get(template['connector_id'], workspace_id, include_secret=True)
        expected = parse_params(template)
        if not isinstance(values, list) or len(values) != len(expected):
            raise AppError(400, 'QUERY_PARAMS_INVALID', '查询参数数量与模板定义不一致')
        started = time.monotonic()

        async def _execute(connection):
            sql = assert_read_only_sql(template['sql_text'])
            if connection['type'] == 'sqlite':
                rows = connection['raw'].execute(f'SELECT * FROM ({sql}) AS ordo_query LIMIT ?', (*values, template['row_limit'])).fetchall()
                fields = list(rows[0].keys()) if rows else []
                return {'rows': [dict(row) for row in rows], 'fields': fields, 'truncated': len(rows) >= template['row_limit']}
            await connection['raw'].execute(f'SELECT * FROM ({sql}) AS ordo_query LIMIT {int(template["row_limit"])}', values)
            rows = await connection['raw'].fetchall()
            descriptions = connection['raw'].description or []
            return {'rows': [dict(zip([d[0] for d in descriptions], row)) for row in rows],
                    'fields': [d[0] for d in descriptions], 'truncated': len(rows) >= template['row_limit']}

        result = await self._with_connection(record, workspace_id, _execute)
        response = {**result, 'rowCount': len(result['rows']),
                    'elapsedMs': round((time.monotonic() - started) * 1000), 'templateId': template_id}
        self.audit.append(workspace_id=workspace_id, action='database_template.execute', object_type='database_query_template',
                          object_id=template_id, request_id=request_id,
                          details={'rowCount': response['rowCount'], 'truncated': response['truncated'], 'elapsedMs': response['elapsedMs']})
        return response

    async def snapshot(self, template_id, input=None, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        input = input or {}
        template = self.get_template(template_id, workspace_id)
        result = await self.execute_template(template_id, input.get('values') or [], workspace_id, request_id)
        dataset = self.knowledge.ensure_dataset(required(input.get('datasetId'), 'datasetId'), workspace_id)
        name = input.get('name') or template['name']
        source = self.knowledge.create_source(dataset['id'], {'type': 'database', 'name': name,
                                                              'config': {'connectorId': template['connector_id'], 'templateId': template_id}},
                                              workspace_id, request_id)
        columns = result['fields']
        rows_dict = result['rows']

        def _csv_cell(value):
            text = str(value if value is not None else '')
            return '"' + text.replace('"', '""') + '"' if re.search(r'[",\n]', text) else text

        csv_lines = [','.join(_csv_cell(column) for column in columns)]
        for row in rows_dict:
            csv_lines.append(','.join(_csv_cell(row.get(column)) for column in columns))
        csv_text = '\n'.join(csv_lines)
        artifact_id = gen_id('dbsnap')
        keys = self.artifact_store.write_document(workspace_id, artifact_id, {
            'snapshot.json': {'schemaVersion': 1, 'templateId': template_id,
                              'connectorId': template['connector_id'], 'fields': columns, 'rows': rows_dict, 'createdAt': now()}})
        registered = self.knowledge.register_upload(dataset['id'], source['id'], f'{name}.csv', csv_text.encode('utf-8'), 'text/csv', workspace_id, request_id)

        snapshot_id = gen_id('dbsnap')
        schema_hash = hash_bytes(json.dumps(columns, ensure_ascii=False, separators=(',', ':')))
        self.db.run('INSERT INTO database_snapshots(id,workspace_id,connector_id,template_id,dataset_id,source_id,row_count,schema_hash,watermark,artifact_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    snapshot_id, workspace_id, template['connector_id'], template_id, dataset['id'], source['id'],
                    result['rowCount'], schema_hash, input.get('watermark'), keys['snapshot.json'], now())
        self.audit.append(workspace_id=workspace_id, action='database_snapshot.create', object_type='database_snapshot',
                          object_id=snapshot_id, request_id=request_id,
                          details={'datasetId': dataset['id'], 'rowCount': result['rowCount'], 'documentId': registered['document']['id']})
        return {'id': snapshot_id, 'datasetId': dataset['id'], 'sourceId': source['id'], 'rowCount': result['rowCount'],
                'schemaHash': schema_hash, 'document': registered['document'], 'task': registered['task']}


def parse_params(template):
    value = template.get('params_json') if isinstance(template.get('params_json'), list) else template.get('params')
    if value is None:
        try:
            value = json.loads(template.get('params_json') or '[]')
        except (ValueError, TypeError):
            value = []
    return value if isinstance(value, list) else []
