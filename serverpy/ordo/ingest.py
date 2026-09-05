import gzip
import io
import os
import tarfile
import zipfile
from pathlib import Path

from .core import AppError, gen_id, hash_bytes, now, safe_name, stable_json
from .parsers import ALLOWED_EXTENSIONS, ARCHIVE_EXTENSIONS, extension_of


def validate_archive_path(name):
    normalized = str(name or '').replace('\\', '/')
    if not normalized or normalized.startswith('/') or re_drive(normalized) or '\x00' in normalized:
        raise AppError(422, 'ARCHIVE_UNSAFE_PATH', '压缩包包含绝对路径或设备路径')
    parts = [part for part in normalized.split('/') if part]
    if any(part in ('..', '.') for part in parts):
        raise AppError(422, 'ARCHIVE_PATH_TRAVERSAL', '压缩包包含路径穿越条目')
    if len(parts) > 32:
        raise AppError(422, 'ARCHIVE_PATH_TOO_DEEP', '压缩包路径层级超过安全预算')
    return '/'.join(safe_name(part) for part in parts)


def re_drive(value):
    import re
    return bool(re.match(r'^[A-Za-z]:', value))


def is_nested_archive(name):
    return extension_of(name) in ARCHIVE_EXTENSIONS


def _within(root, candidate):
    root = os.path.normcase(str(Path(root).resolve()))
    candidate = os.path.normcase(str(Path(candidate).resolve()))
    return candidate == root or candidate.startswith(root + os.sep)


def _same_path(left, right):
    return _within(left, right) and _within(right, left)


def extract_zip(buffer, limits):
    try:
        with zipfile.ZipFile(io.BytesIO(buffer)) as archive:
            infos = archive.infolist()
            if any(info.flag_bits & 0x1 for info in infos):
                raise AppError(422, 'NEEDS_PASSWORD', '压缩包需要密码或使用了不支持的加密方式')
            files = []
            seen_names = set()
            total = 0
            for info in infos:
                if info.is_dir():
                    continue
                if len(files) >= limits['maxFiles']:
                    raise AppError(413, 'ARCHIVE_FILE_LIMIT', '压缩包文件数量超过预算')
                normalized = validate_archive_path(info.filename)
                if normalized in seen_names:
                    raise AppError(422, 'ARCHIVE_DUPLICATE_PATH', '压缩包包含规范化后的重复路径')
                seen_names.add(normalized)
                if info.file_size > limits['maxFileBytes']:
                    raise AppError(413, 'ARCHIVE_SIZE_LIMIT', '压缩包展开大小超过预算')
                total += info.file_size
                if total > limits['maxBytes']:
                    raise AppError(413, 'ARCHIVE_SIZE_LIMIT', '压缩包展开大小超过预算')
                content = archive.read(info)
                files.append({'name': normalized, 'buffer': content})
            return files
    except AppError:
        raise
    except zipfile.BadZipFile:
        raise AppError(422, 'ARCHIVE_INVALID', 'ZIP 压缩包无效或已损坏')


def extract_tar(buffer, compressed, limits):
    try:
        if compressed:
            stream = gzip.GzipFile(fileobj=io.BytesIO(buffer))
            try:
                fileobj = tarfile.open(fileobj=stream, mode='r:')
            except (tarfile.TarError, OSError):
                raise AppError(422, 'ARCHIVE_INVALID', 'TAR 压缩包无效或已损坏')
        else:
            fileobj = tarfile.open(fileobj=io.BytesIO(buffer), mode='r:')
    except AppError:
        raise
    except (tarfile.TarError, OSError):
        raise AppError(422, 'ARCHIVE_INVALID', 'TAR 压缩包无效或已损坏')
    files = []
    seen_names = set()
    total = 0
    with fileobj:
        for member in fileobj.getmembers():
            if member.issym() or member.islnk() or member.ischr() or member.isblk() or member.isfifo():
                raise AppError(422, 'ARCHIVE_LINK_REJECTED', '压缩包包含链接或设备文件')
            if not member.isfile():
                continue
            normalized = validate_archive_path(member.name)
            if normalized in seen_names:
                raise AppError(422, 'ARCHIVE_DUPLICATE_PATH', '压缩包包含规范化后的重复路径')
            seen_names.add(normalized)
            if len(files) >= limits['maxFiles']:
                raise AppError(413, 'ARCHIVE_FILE_LIMIT', '压缩包文件数量超过预算')
            if member.size > limits['maxFileBytes']:
                raise AppError(413, 'ARCHIVE_SIZE_LIMIT', '压缩包展开大小超过预算')
            total += member.size
            if total > limits['maxBytes']:
                raise AppError(413, 'ARCHIVE_SIZE_LIMIT', '压缩包展开大小超过预算')
            extracted = fileobj.extractfile(member)
            content = extracted.read() if extracted else b''
            files.append({'name': normalized, 'buffer': content})
    return files


def extract_archive(buffer, filename, limits, via_import=True):
    extension = extension_of(filename)
    if extension == '.zip':
        files = extract_zip(buffer, limits)
    elif extension == '.tar':
        files = extract_tar(buffer, False, limits)
    elif extension in ('.tar.gz', '.tgz'):
        files = extract_tar(buffer, True, limits)
    else:
        raise AppError(415, 'UNSUPPORTED_FORMAT', '不是支持的压缩包格式')
    expanded_bytes = sum(len(item['buffer']) for item in files)
    if extension in ('.zip', '.tar.gz', '.tgz') and len(buffer) and expanded_bytes / len(buffer) > (limits.get('maxCompressionRatio') or 100):
        raise AppError(413, 'ARCHIVE_COMPRESSION_RATIO', '压缩包展开比例超过安全预算')
    return files


class IngestService:
    def __init__(self, db, knowledge, tasks, audit, config):
        self.db = db
        self.knowledge = knowledge
        self.tasks = tasks
        self.audit = audit
        self.config = config
        tasks.register('archive.import', self._archive_task)
        tasks.register('directory.import', self._directory_task)

    def _workspace_id(self):
        return self.config['localWorkspaceId']

    def archive_import(self, dataset_id, filename, buffer, input=None, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self._workspace_id()
        input = input or {}
        self.knowledge.ensure_dataset(dataset_id, workspace_id)
        if not isinstance(buffer, (bytes, bytearray)) or not buffer:
            raise AppError(400, 'EMPTY_FILE', '压缩包为空')
        buffer = bytes(buffer)
        if len(buffer) > self.config['maxFileBytes']:
            raise AppError(413, 'FILE_TOO_LARGE', '压缩包超过上传预算')
        blob = self.knowledge.blob_store.put(workspace_id, buffer, 'application/octet-stream')
        idempotency_key = input.get('idempotencyKey') or f"archive:{dataset_id}:{blob['sha256']}"
        existing = self.db.one('SELECT * FROM tasks WHERE workspace_id=? AND idempotency_key=?', workspace_id, idempotency_key)
        if existing:
            source = self.db.one('SELECT * FROM sources WHERE id=? AND workspace_id=?', existing['object_id'], workspace_id)
            task = self.tasks.create(workspace_id=workspace_id, task_type='archive.import', object_type='source',
                                     object_id=existing['object_id'], idempotency_key=idempotency_key, input=existing['input'] or {})
            return {'duplicate': True, 'source': source, 'task': task}
        source = self.knowledge.create_source(dataset_id, {'type': 'archive', 'name': filename,
                                                           'config': {'originalBlobId': blob['id']}}, workspace_id, request_id)
        task = self.tasks.create(workspace_id=workspace_id, task_type='archive.import', object_type='source', object_id=source['id'],
                                 idempotency_key=idempotency_key,
                                 input={'datasetId': dataset_id, 'sourceId': source['id'], 'filename': filename, 'blobId': blob['id']})
        return {'duplicate': False, 'source': source, 'task': task}

    def _directory_scan(self, directory, rules=None):
        rules = rules or {}
        root = Path(directory).resolve()
        if not root.exists() or not root.is_dir():
            raise AppError(400, 'DIRECTORY_INVALID', '授权目录不存在或不是目录')
        excluded = [str(item).lower() for item in (rules.get('exclude') or [])]
        max_files = min(int(rules.get('maxFiles') or self.config['maxArchiveFiles']), self.config['maxArchiveFiles'])
        candidates = []

        def walk(current):
            for entry in sorted(current.iterdir()):
                if len(candidates) >= max_files:
                    return
                relative = entry.relative_to(root).as_posix()
                if any(pattern in relative.lower() for pattern in excluded):
                    continue
                if entry.is_symlink():
                    raise AppError(400, 'DIRECTORY_SYMLINK_REJECTED', '目录导入不允许符号链接')
                if entry.is_dir():
                    walk(entry)
                elif entry.is_file():
                    extension = extension_of(entry.name)
                    candidates.append({
                        'relativePath': relative, 'sizeBytes': entry.stat().st_size,
                        'supported': extension in ALLOWED_EXTENSIONS and extension not in ARCHIVE_EXTENSIONS,
                        'extension': extension,
                    })
        walk(root)
        total_bytes = sum(item['sizeBytes'] for item in candidates)
        return {'root': str(root), 'count': len(candidates), 'totalBytes': total_bytes,
                'truncated': len(candidates) >= max_files, 'candidates': candidates}

    def directory_preview(self, directory, rules=None):
        scan = self._directory_scan(directory, rules)
        return {'root': scan['root'], 'count': scan['count'], 'totalBytes': scan['totalBytes'],
                'truncated': scan['truncated'], 'candidates': scan['candidates']}

    def directory_import(self, dataset_id, input, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self._workspace_id()
        input = input or {}
        self.knowledge.ensure_dataset(dataset_id, workspace_id)
        scan = self._directory_scan(input.get('directory'), input.get('rules') or {})
        preview = {'root': scan['root'], 'count': scan['count'], 'totalBytes': scan['totalBytes'],
                   'truncated': scan['truncated'], 'candidates': scan['candidates']}
        rules = input.get('rules') or {}
        idempotency_key = input.get('idempotencyKey') or f"directory:{dataset_id}:{hash_bytes(stable_json({'root': preview['root'], 'rules': rules}))}"
        existing = self.db.one('SELECT * FROM tasks WHERE workspace_id=? AND idempotency_key=?', workspace_id, idempotency_key)
        if existing:
            source = self.db.one('SELECT * FROM sources WHERE id=? AND workspace_id=?', existing['object_id'], workspace_id)
            task = self.tasks.create(workspace_id=workspace_id, task_type='directory.import', object_type='source',
                                     object_id=existing['object_id'], idempotency_key=idempotency_key, input=existing['input'] or {})
            return {'duplicate': True, 'source': source, 'preview': preview, 'task': task}
        source = self.knowledge.create_source(dataset_id, {'type': 'directory', 'name': os.path.basename(preview['root']),
                                                           'locationHint': preview['root'],
                                                           'config': {'rules': rules, 'authorizedAt': now()}},
                                              workspace_id, request_id)
        task = self.tasks.create(workspace_id=workspace_id, task_type='directory.import', object_type='source', object_id=source['id'],
                                 idempotency_key=idempotency_key,
                                 input={'datasetId': dataset_id, 'sourceId': source['id'], 'root': preview['root'],
                                        'candidates': scan['candidates'], 'rules': rules})
        return {'duplicate': False, 'source': source, 'preview': preview, 'task': task}

    async def _archive_task(self, context):
        input = context['input']
        workspace_id = context['workspaceId']
        checkpoint = context['checkpoint']
        await checkpoint(5, '验证压缩包安全预算')
        blob = self.knowledge.blob_store.get(input['blobId'], workspace_id)
        files = extract_archive(blob['buffer'], input['filename'], {
            'maxFiles': self.config['maxArchiveFiles'], 'maxBytes': self.config['maxArchiveBytes'],
            'maxFileBytes': self.config['maxFileBytes'], 'maxCompressionRatio': self.config['maxArchiveCompressionRatio']})
        manifest = []
        for index, file in enumerate(files):
            extension = extension_of(file['name'])
            if is_nested_archive(file['name']):
                manifest.append({'path': file['name'], 'sizeBytes': len(file['buffer']), 'status': 'nested_archive_not_expanded'})
            elif extension not in ALLOWED_EXTENSIONS:
                manifest.append({'path': file['name'], 'sizeBytes': len(file['buffer']), 'status': 'unsupported'})
            else:
                try:
                    registered = self.knowledge.register_upload(input['datasetId'], input['sourceId'], file['name'],
                                                                      file['buffer'], None, workspace_id)
                    manifest.append({'path': file['name'], 'sizeBytes': len(file['buffer']),
                                     'status': 'duplicate' if registered['duplicate'] else 'queued',
                                     'documentId': registered['document']['id'], 'taskId': (registered.get('task') or {}).get('id')})
                except AppError as error:
                    manifest.append({'path': file['name'], 'sizeBytes': len(file['buffer']), 'status': 'failed',
                                     'code': error.code or 'IMPORT_FAILED', 'message': error.message})
            await checkpoint(10 + (index + 1) / max(len(files), 1) * 85, '登记压缩包文件', {'processed': index + 1, 'total': len(files)})
        failures = [item for item in manifest if item['status'] in ('failed', 'unsupported', 'nested_archive_not_expanded')]
        self.db.run("UPDATE sources SET status=?,config_json=?,updated_at=? WHERE id=? AND workspace_id=?",
                    'partial' if failures else 'queued', stable_json({'archiveManifest': manifest}), now(), input['sourceId'], workspace_id)
        self.audit.append(workspace_id=workspace_id, action='archive.import', object_type='source', object_id=input['sourceId'],
                          details={'files': len(files), 'failures': len(failures)})
        return {'status': 'partial' if failures else 'succeeded', 'files': len(files), 'failures': len(failures), 'manifest': manifest}

    async def _directory_task(self, context):
        input = context['input']
        workspace_id = context['workspaceId']
        checkpoint = context['checkpoint']
        candidates = input.get('candidates') if isinstance(input.get('candidates'), list) else []
        manifest = []
        for index, item in enumerate(candidates):
            if not item.get('supported'):
                manifest.append({'path': item['relativePath'], 'status': 'unsupported', 'sizeBytes': item['sizeBytes']})
            elif item['sizeBytes'] > self.config['maxFileBytes']:
                manifest.append({'path': item['relativePath'], 'status': 'resource_limit', 'sizeBytes': item['sizeBytes']})
            else:
                try:
                    path = Path(input['root']) / item['relativePath']
                    buffer = path.read_bytes()
                    registered = self.knowledge.register_upload(input['datasetId'], input['sourceId'], item['relativePath'],
                                                                      buffer, None, workspace_id)
                    manifest.append({'path': item['relativePath'], 'status': 'duplicate' if registered['duplicate'] else 'queued',
                                     'documentId': registered['document']['id'], 'taskId': (registered.get('task') or {}).get('id')})
                except AppError as error:
                    manifest.append({'path': item['relativePath'], 'status': 'failed', 'code': error.code or 'IMPORT_FAILED', 'message': error.message})
            await checkpoint(5 + (index + 1) / max(len(candidates), 1) * 90, '导入授权目录', {'processed': index + 1, 'total': len(candidates)})
        failures = [item for item in manifest if item['status'] in ('failed', 'unsupported', 'resource_limit')]
        self.db.run("UPDATE sources SET status=?,config_json=?,updated_at=? WHERE id=? AND workspace_id=?",
                    'partial' if failures else 'queued',
                    stable_json({'root': input.get('root'), 'rules': input.get('rules') or {}, 'manifest': manifest}),
                    now(), input['sourceId'], workspace_id)
        self.audit.append(workspace_id=workspace_id, action='directory.import', object_type='source', object_id=input['sourceId'],
                          details={'files': len(manifest), 'failures': len(failures)})
        return {'status': 'partial' if failures else 'succeeded', 'files': len(manifest), 'failures': len(failures), 'manifest': manifest}
