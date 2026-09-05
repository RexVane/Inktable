"""Authenticated streaming backups, compatible with ORDOENC1 Node archives."""
import asyncio
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .core import AppError, gen_id, now, stable_json
from .storage import assert_within


def digest(file):
    with Path(file).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def key_for(service, ws, backup_id):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=ws.encode(), info=('ordo-backup:' + backup_id).encode()).derive(service.secret_store.key)


def crypt_file(source, target, key, decrypt=False):
    with Path(source).open('rb') as src, Path(target).open('xb') as dst:
        if decrypt:
            if src.read(8) != b'ORDOENC1' or Path(source).stat().st_size < 36:
                raise AppError(422, 'BACKUP_ENVELOPE_INVALID', '备份加密封装无效')
            iv = src.read(12)
            src.seek(-16, 2)
            tag = src.read(16)
            remaining = src.tell() - 36
            src.seek(20)
            cipher = Cipher(algorithms.AES(key), modes.GCM(iv, tag)).decryptor()
        else:
            iv, remaining = os.urandom(12), Path(source).stat().st_size
            dst.write(b'ORDOENC1' + iv)
            cipher = Cipher(algorithms.AES(key), modes.GCM(iv)).encryptor()
        while remaining:
            block = src.read(min(1024 * 1024, remaining))
            if not block:
                raise AppError(422, 'BACKUP_TRUNCATED', '备份被截断')
            dst.write(cipher.update(block))
            remaining -= len(block)
        try:
            dst.write(cipher.finalize())
        except Exception as error:
            raise AppError(422, 'BACKUP_DECRYPT_FAILED', '备份解密或认证失败') from error
        if not decrypt:
            dst.write(cipher.tag)
        dst.flush()
        os.fsync(dst.fileno())


def validate_target(value, current):
    target, current = Path(value).resolve(), Path(current).resolve()
    if target == Path(target.anchor) or target == current or current in target.parents or target in current.parents or target.exists():
        raise AppError(400, 'RESTORE_TARGET_INVALID', '恢复目标必须是尚不存在的新数据目录')
    if not target.parent.is_dir():
        raise AppError(400, 'RESTORE_TARGET_PARENT_INVALID', '恢复目标的父目录必须已存在')
    return target


async def create_backup(service, context):
    ws, checkpoint = context['workspaceId'], context['checkpoint']
    backup_id, timestamp = gen_id('backup'), now()
    archive = service.config['backupRoot'] / (backup_id + '.tar.gz.enc')
    manifest = {'schemaVersion': service.schema_version(), 'appVersion': service.config['appVersion'],
                'backupId': backup_id, 'workspaceId': ws, 'createdAt': timestamp, 'label': context['input'].get('label'),
                'encryption': service.config['backupEncryptionVersion'], 'files': [], 'objectCounts': service.dashboard(ws)['counts']}
    with tempfile.TemporaryDirectory(prefix='backup-', dir=service.config['runtimeRoot']) as temp:
        root = Path(temp)
        await checkpoint(10, '创建一致性数据库快照')
        snapshot = root / 'snapshot.sqlite3'
        def copy_database():
            with closing(sqlite3.connect(service.config['dbPath'])) as src, closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
        await asyncio.to_thread(copy_database)
        files = [('metadata/ordo.sqlite3', snapshot), ('runtime/master.key', service.config['keyPath'])]
        for prefix, directory in [('blobs', service.config['blobRoot']), ('artifacts', service.config['artifactRoot'])]:
            files.extend((prefix + '/' + path.relative_to(directory).as_posix(), path)
                         for path in directory.rglob('*') if path.is_file() and not path.is_symlink())
        with tarfile.open(root / 'archive.tar.gz', 'w:gz') as pack:
            for index, (name, file) in enumerate(files):
                assert_within(service.config['dataRoot'], file)
                info = pack.gettarinfo(str(file), arcname=name)
                info.mode = 0o600
                with file.open('rb') as stream:
                    pack.addfile(info, stream)
                manifest['files'].append({'name': name, 'sizeBytes': info.size, 'sha256': digest(file)})
                if index % 25 == 0:
                    await checkpoint(20 + index / max(len(files), 1) * 60, '打包主数据', {'packed': index, 'total': len(files)})
            content = stable_json(manifest).encode()
            info = tarfile.TarInfo('backup-manifest.json')
            info.size, info.mode = len(content), 0o600
            pack.addfile(info, io.BytesIO(content))
        await asyncio.to_thread(crypt_file, root / 'archive.tar.gz', root / 'encrypted', key_for(service, ws, backup_id))
        checksum = digest(root / 'encrypted')
        await checkpoint(95, '校验备份包完整性')
        (root / 'encrypted').replace(archive)
        try:
            service.db.transaction(lambda: (
                service.db.run('INSERT INTO backup_manifests(id,workspace_id,status,storage_key,manifest_json,checksum,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?)', backup_id, ws, 'verified', archive.name, stable_json(manifest), checksum, timestamp, now()),
                service.audit.append(workspace_id=ws, action='backup.create', object_type='backup', object_id=backup_id, details={'files': len(files), 'checksum': checksum})))
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
    return {'backupId': backup_id, 'status': 'verified', 'storageKey': archive.name, 'checksum': checksum, 'fileCount': len(files)}


async def restore_backup(service, context):
    ws, input, checkpoint = context['workspaceId'], context['input'], context['checkpoint']
    backup = service.db.one('SELECT * FROM backup_manifests WHERE id=? AND workspace_id=?', input['backupId'], ws)
    if not backup:
        raise AppError(404, 'NOT_FOUND', '备份不存在')
    target = validate_target(input['targetRoot'], service.config['dataRoot'])
    archive = Path(assert_within(service.config['backupRoot'], service.config['backupRoot'] / backup['storage_key']))
    await checkpoint(5, '校验备份密文')
    if digest(archive) != backup['checksum']:
        raise AppError(422, 'BACKUP_CHECKSUM_INVALID', '备份校验和不匹配')
    manifest = backup['manifest']
    expected = {item['name']: item for item in manifest['files']}
    if manifest.get('workspaceId') != ws or manifest.get('schemaVersion', 0) > service.schema_version():
        raise AppError(422, 'BACKUP_INCOMPATIBLE', '备份工作区或数据库版本不兼容')
    # Stage under the target parent for an atomic rename on the same volume.
    with tempfile.TemporaryDirectory(prefix='.ordo-restore-', dir=target.parent) as temp:
        temp_root, seen = Path(temp).resolve(), set()
        stage = temp_root / 'data'
        stage.mkdir()
        plain = temp_root / 'archive.tar.gz'
        await asyncio.to_thread(crypt_file, archive, plain, key_for(service, ws, backup['id']), True)
        with tarfile.open(plain, 'r:gz') as pack:
            for member in pack:
                name = member.name
                logical = PurePosixPath(name)
                if '\\' in name or ':' in name or logical.is_absolute() or '..' in logical.parts or not member.isfile() or name in seen:
                    raise AppError(422, 'BACKUP_ENTRY_INVALID', '备份包含不安全条目')
                if name != 'backup-manifest.json' and name not in expected:
                    raise AppError(422, 'BACKUP_ENTRY_INVALID', '备份含清单之外的文件')
                budget = expected[name]['sizeBytes'] if name in expected else 16 * 1024 * 1024
                if member.size > budget or (name in expected and member.size != budget):
                    raise AppError(422, 'BACKUP_SIZE_INVALID', '备份文件大小不匹配')
                file = Path(assert_within(stage, stage / name))
                file.parent.mkdir(parents=True, exist_ok=True)
                with pack.extractfile(member) as source, file.open('xb') as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                seen.add(name)
                if name in expected and digest(file) != expected[name]['sha256']:
                    raise AppError(422, 'BACKUP_FILE_HASH_INVALID', '备份文件摘要不匹配')
                if len(seen) % 25 == 0:
                    await checkpoint(30 + len(seen) / max(len(expected) + 1, 1) * 50, '验证恢复文件')
        if seen != set(expected) | {'backup-manifest.json'}:
            raise AppError(422, 'BACKUP_FILES_MISSING', '备份缺少必要文件')
        if json.loads((stage / 'backup-manifest.json').read_text('utf-8')) != manifest:
            raise AppError(422, 'BACKUP_MANIFEST_INVALID', '备份内外清单不一致')
        with closing(sqlite3.connect(stage / 'metadata/ordo.sqlite3')) as restored:
            if restored.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or restored.execute('PRAGMA foreign_key_check').fetchone():
                raise AppError(422, 'BACKUP_DATABASE_INVALID', '恢复数据库完整性验证失败')
            for key, in restored.execute('SELECT storage_key FROM blobs'):
                if not Path(assert_within(stage, stage / 'blobs' / key)).is_file():
                    raise AppError(422, 'BACKUP_FILES_MISSING', '备份缺少引用的原始文件')
            for row in restored.execute('SELECT markdown_key,json_key,manifest_key,quality_key FROM parsed_artifacts'):
                if any(not Path(assert_within(stage, stage / 'artifacts' / key)).is_file() for key in row):
                    raise AppError(422, 'BACKUP_FILES_MISSING', '备份缺少引用的解析产物')
        await checkpoint(95, '完成恢复验证')
        validate_target(target, service.config['dataRoot'])
        # rename fails if a different process has created the target in the meantime.
        stage.rename(target)
    service.audit.append(workspace_id=ws, action='backup.restore', object_type='backup', object_id=backup['id'], details={'targetRoot': str(target)})
    return {'backupId': backup['id'], 'targetRoot': str(target), 'status': 'restored', 'verified': True, 'fileCount': len(expected)}
