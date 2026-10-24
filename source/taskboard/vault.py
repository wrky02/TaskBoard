"""Optional at-rest workspace encryption; no decrypted database/temp attachments.

SQLCipher protects SQLite (including journals). AES-256-GCM protects managed
objects; the workspace master key is independently wrapped by a password and a
random recovery key. Password changes rewrap the key, never rewrite all objects.
Existing backup copies keep the password/recovery wrapper they were made with.

This module does not promise secure erasure of SSDs, the Windows pagefile or
copies exported by the user. Migration removes the application's old plaintext
files after the encrypted generation is committed and recoverably journaled.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import io
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import struct
import tempfile
import threading
import uuid
import zipfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

META = 'vault.json'
JOURNAL = '.vault-migration.json'
FILE_MAGIC = b'TBFILE01'
ARCHIVE_MAGIC = b'TBARC001'
MAX_ARCHIVE = 2 * 1024 * 1024 * 1024
OBJECT_DIRS = ('attachments', 'history-objects', 'staging')
_sessions: dict[Path, 'Vault'] = {}
_guard = threading.RLock()


class VaultLocked(ValueError):
    pass


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode('ascii')


def _unb64(value: str, size: int | None = None) -> bytes:
    try:
        result = base64.b64decode(value, validate=True)
        if size is not None and len(result) != size:
            raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise ValueError('加密工作区元数据无效') from exc


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')


def _derive(master: bytes, purpose: str) -> bytes:
    return HKDF(algorithm=SHA256(), length=32, salt=None,
                info=('TaskBoard/vault/1/' + purpose).encode('ascii')).derive(master)


def _password_key(password: str, salt: bytes) -> bytes:
    # Parameters are fixed by the versioned format, not accepted from a file.
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password.encode('utf8'))


def _recovery_bytes(key: str) -> bytes:
    value = re.sub(r'[\s-]', '', str(key)).upper()
    if value.startswith('TB4'):
        value = value[3:]
    if not re.fullmatch(r'[A-Z2-7]{52}', value):
        raise ValueError('恢复密钥格式无效')
    try:
        return base64.b32decode(value + '=' * 4)
    except ValueError as exc:
        raise ValueError('恢复密钥格式无效') from exc


def _new_recovery_key() -> str:
    text = base64.b32encode(secrets.token_bytes(32)).decode('ascii').rstrip('=')
    return 'TB4-' + '-'.join(text[i:i+4] for i in range(0, len(text), 4))


def _aad(metadata: dict, purpose: str) -> bytes:
    return ('TaskBoard/vault/1/' + metadata['id'] + '/' + purpose).encode('utf8')


def _wrap(master: bytes, key: bytes, metadata: dict, purpose: str) -> dict:
    nonce = secrets.token_bytes(12)
    return {'nonce': _b64(nonce), 'key': _b64(AESGCM(key).encrypt(nonce, master, _aad(metadata, purpose)))}


def _sign_metadata(metadata: dict, master: bytes) -> None:
    unsigned = {key: value for key, value in metadata.items() if key != 'auth'}
    metadata['auth'] = hmac.new(_derive(master, 'metadata'), _json_bytes(unsigned), hashlib.sha256).hexdigest()


def _authenticate_metadata(metadata: dict, master: bytes) -> None:
    unsigned = {key: value for key, value in metadata.items() if key != 'auth'}
    expected = hmac.new(_derive(master, 'metadata'), _json_bytes(unsigned), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, metadata.get('auth', '')):
        raise ValueError('加密工作区密钥元数据校验失败')


def _check_metadata(metadata: dict) -> dict:
    if (not isinstance(metadata, dict) or metadata.get('version') != 1 or
            metadata.get('cipher') != 'SQLCipher4+AES256GCM' or
            not re.fullmatch(r'[a-f0-9]{32}', str(metadata.get('id', ''))) or
            metadata.get('kdf') != 'scrypt-N32768-r8-p1' or
            not re.fullmatch(r'[a-f0-9]{64}', str(metadata.get('auth', '')))):
        raise ValueError('不支持或已损坏的加密工作区格式')
    _unb64(metadata.get('salt'), 16)
    for purpose in ('password', 'recovery'):
        entry = metadata.get(purpose, {})
        _unb64(entry.get('nonce'), 12)
        _unb64(entry.get('key'), 48)
    return metadata


def _read_metadata(path: Path) -> dict:
    if path.stat().st_size > 16384:
        raise ValueError('加密工作区元数据过大')
    try:
        return _check_metadata(json.loads(path.read_text(encoding='utf8')))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('加密工作区元数据无效') from exc


def _unwrap(metadata: dict, password: str | None = None, recovery_key: str | None = None) -> bytes:
    _check_metadata(metadata)
    if recovery_key is not None:
        purpose = 'recovery'
        key = _derive(_recovery_bytes(recovery_key), 'recovery-wrapper')
    elif password is not None:
        purpose = 'password'
        key = _password_key(password, _unb64(metadata['salt'], 16))
    else:
        raise VaultLocked('工作区已加密，请输入密码或恢复密钥')
    entry = metadata[purpose]
    try:
        master = AESGCM(key).decrypt(_unb64(entry['nonce'], 12), _unb64(entry['key'], 48), _aad(metadata, purpose))
        _authenticate_metadata(metadata, master)
        return master
    except InvalidTag as exc:
        raise ValueError('密码或恢复密钥错误，或加密元数据已损坏') from exc


def _fsync_dir(path: Path) -> None:
    # Windows does not permit opening directories through os.open.
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class Vault:
    root: Path
    metadata: dict
    master: bytes

    @property
    def database_key(self) -> bytes:
        return _derive(self.master, 'database')

    def seal(self, content: bytes, relative: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return FILE_MAGIC + nonce + AESGCM(_derive(self.master, 'objects')).encrypt(
            nonce, content, _aad(self.metadata, 'object/' + relative))

    def unseal(self, content: bytes, relative: str) -> bytes:
        if len(content) < 36 or not content.startswith(FILE_MAGIC):
            raise ValueError('加密工作区发现未加密或已损坏的文件')
        try:
            return AESGCM(_derive(self.master, 'objects')).decrypt(
                content[8:20], content[20:], _aad(self.metadata, 'object/' + relative))
        except InvalidTag as exc:
            raise ValueError('加密文件校验失败，文件可能已损坏或被替换') from exc


def is_enabled(root) -> bool:
    return (Path(root) / META).is_file()


def is_unlocked(root) -> bool:
    with _guard:
        return Path(root).resolve() in _sessions


def current(root) -> Vault | None:
    with _guard:
        result = _sessions.get(Path(root).resolve())
    if result is None and is_enabled(root):
        raise VaultLocked('加密工作区已锁定')
    return result


def unlock(root, password=None, recovery_key=None) -> Vault:
    root = Path(root).resolve()
    recover_migration(root)
    metadata = _read_metadata(root / META)
    instance = Vault(root, metadata, _unwrap(metadata, password, recovery_key))
    # Verify the database before registering a usable session.
    if (root / 'tasks.sqlite3').is_file():
        check = _connect_cipher(root / 'tasks.sqlite3', instance, readonly=True)
        check.close()
    with _guard:
        _sessions[root] = instance
    return instance


def lock(root) -> None:
    """Caller must first close all SQLite connections and background workers."""
    with _guard:
        _sessions.pop(Path(root).resolve(), None)


def _connect_cipher(path: Path, instance: Vault, readonly=False):
    import sqlcipher3
    target = path.resolve().as_uri() + '?mode=ro' if readonly else str(path)
    db = sqlcipher3.connect(target, uri=readonly)
    try:
        # Key is generated bytes, never user-controlled SQL.
        db.execute('PRAGMA key="x\'' + instance.database_key.hex() + '\'"')
        if not db.execute('PRAGMA cipher_version').fetchone():
            raise RuntimeError('SQLCipher 加密运行库不可用')
        db.execute('PRAGMA cipher_memory_security=ON')
        db.execute('PRAGMA temp_store=MEMORY')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('SELECT count(*) FROM sqlite_master').fetchone()
        db.row_factory = sqlcipher3.Row
        return db
    except Exception:
        db.close()
        raise


def connection(root, path=None, readonly=False):
    root = Path(root).resolve()
    instance = current(root)
    path = Path(path) if path is not None else root / 'tasks.sqlite3'
    if instance is not None:
        return _connect_cipher(path, instance, readonly)
    target = path.resolve().as_uri() + '?mode=ro' if readonly else str(path)
    result = sqlite3.connect(target, uri=readonly)
    result.row_factory = sqlite3.Row
    return result


def open_snapshot(path, root, readonly=True):
    return connection(root, path=path, readonly=readonly)


def snapshot_database(db, destination, root=None) -> None:
    """Copy committed pages using the same SQLite backend and encryption key."""
    if db.in_transaction:
        raise ValueError('保存事务尚未结束，无法创建一致备份')
    if root is None:
        rows = db.execute('PRAGMA database_list').fetchall()
        main = next((row[2] for row in rows if row[1] == 'main'), '')
        if not main:
            raise ValueError('内存数据库快照必须指定工作区目录')
        root = Path(main).parent
    output = connection(root, destination)
    try:
        db.backup(output)
        if output.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('数据库快照完整性验证失败')
    finally:
        output.close()


def backup_metadata(root) -> dict | None:
    instance = current(root)
    return dict(instance.metadata) if instance is not None else None


def _instance_for_metadata(metadata, root=None, password=None, recovery_key=None) -> Vault:
    _check_metadata(metadata)
    instance = current(root) if root is not None and is_unlocked(root) else None
    if instance is not None and instance.metadata['id'] == metadata['id']:
        _authenticate_metadata(metadata, instance.master)
        return Vault(Path(root).resolve(), metadata, instance.master)
    return Vault(Path(root or '.').resolve(), metadata, _unwrap(metadata, password, recovery_key))


def validate_backup_database(db_path, metadata=None, root=None, password=None, recovery_key=None):
    """Return an authenticated read-only connection, without changing sessions."""
    if metadata is None:
        result = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
        result.row_factory = sqlite3.Row
    else:
        result = _connect_cipher(Path(db_path), _instance_for_metadata(metadata, root, password, recovery_key), True)
    try:
        if result.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('备份数据库完整性验证失败')
        if result.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('备份数据库关联验证失败')
        return result
    except Exception:
        result.close()
        raise


@contextmanager
def validate_backup_bytes(content: bytes, metadata=None, root=None, password=None, recovery_key=None):
    """Authenticate/validate without ever writing a plaintext preview database."""
    if metadata is None:
        db = sqlite3.connect(':memory:')
        try:
            db.deserialize(content)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('备份数据库完整性验证失败')
            yield db
        finally:
            db.close()
    else:
        # This temporary file contains authenticated SQLCipher pages only.
        with tempfile.TemporaryDirectory(prefix='taskboard-cipher-check-', dir=root) as directory:
            path = Path(directory) / 'tasks.sqlite3'
            path.write_bytes(content)
            db = validate_backup_database(path, metadata, root, password, recovery_key)
            try:
                yield db
            finally:
                db.close()


@dataclass
class RestoreContext:
    instance: Vault | None


def _encrypt_database_bytes(content: bytes, destination: Path, instance: Vault) -> None:
    """Convert a legacy plain SQLite image entirely in RAM into SQLCipher."""
    source = sqlite3.connect(':memory:')
    target = _connect_cipher(destination, instance)
    try:
        source.deserialize(content)
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('备份数据库完整性验证失败')
        target.execute('PRAGMA foreign_keys=OFF')
        target.executescript('\n'.join(source.iterdump()))
        target.execute('PRAGMA user_version=' + str(int(source.execute('PRAGMA user_version').fetchone()[0])))
        target.execute('PRAGMA foreign_keys=ON')
        target.commit()
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or target.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('加密恢复数据库验证失败')
    finally:
        target.close()
        source.close()


def stage_backup_archive(source, staged, root, password=None, recovery_key=None) -> RestoreContext:
    """Prepare a backup for atomic restore, preserving an encrypted target.

    Plain source SQLite is deserialized in RAM; encrypted sources stay encrypted.
    User-supplied encrypted backups switch to their authenticated workspace key.
    This function only stages data; the current session stays unchanged.
    """
    root, staged = Path(root).resolve(), Path(staged)
    staged.mkdir(parents=True, exist_ok=True)
    with open_archive(source, root, password, recovery_key) as stream, zipfile.ZipFile(stream) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or 'tasks.sqlite3' not in names:
            raise ValueError('备份缺少数据库或包含重复路径')
        source_metadata = json.loads(archive.read(META)) if META in names else None
        instance = (_instance_for_metadata(source_metadata, root, password, recovery_key)
                    if source_metadata is not None else getattr(stream, 'vault_context', None) or current(root))
        for dirname in OBJECT_DIRS:
            (staged / dirname).mkdir(exist_ok=True)
        for member in archive.infolist():
            relative = member.filename
            if relative in (META, 'manifest.json'):
                continue
            if not (relative == 'tasks.sqlite3' or re.fullmatch(
                    r'(?:attachments/[a-f0-9]{32}(?:\.[a-zA-Z0-9]{1,11})?|history-objects/[a-f0-9]{64}|staging/[a-f0-9]{32}/object)', relative)):
                raise ValueError('备份包含无效路径')
            if member.file_size > MAX_ARCHIVE:
                raise ValueError('备份文件过大')
            content = archive.read(member)
            destination = staged / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative == 'tasks.sqlite3':
                if source_metadata is None and instance is not None:
                    _encrypt_database_bytes(content, destination, instance)
                else:
                    _atomic_write(destination, content)
            elif source_metadata is not None:
                instance.unseal(content, relative)  # Authenticate every object before promotion.
                _atomic_write(destination, content)
            else:
                _atomic_write(destination, instance.seal(content, relative) if instance is not None else content)
        if instance is not None:
            _atomic_write(staged / META, _json_bytes(instance.metadata))
        check = validate_backup_database(staged / 'tasks.sqlite3', instance.metadata if instance else None,
                                         root, password, recovery_key)
        check.close()
        return RestoreContext(instance)


def prepare_restore(root, staged, password=None, recovery_key=None) -> RestoreContext:
    staged = Path(staged)
    metadata = _read_metadata(staged / META) if is_enabled(staged) else None
    if metadata is None:
        if is_enabled(root):
            raise ValueError('加密工作区不能直接恢复明文暂存内容，请使用安全恢复入口')
        instance = None
    else:
        instance = _instance_for_metadata(metadata, root, password, recovery_key)
    check = validate_backup_database(staged / 'tasks.sqlite3', metadata, root, password, recovery_key)
    check.close()
    return RestoreContext(instance)


def activate_restored(root, token: RestoreContext) -> None:
    """Call only after the generation transaction commits, never on rollback."""
    root = Path(root).resolve()
    with _guard:
        if token.instance is None:
            if is_enabled(root):
                raise ValueError('恢复后的加密状态与暂存记录不一致')
            _sessions.pop(root, None)
        else:
            metadata = _read_metadata(root / META)
            _authenticate_metadata(metadata, token.instance.master)
            _sessions[root] = Vault(root, metadata, token.instance.master)


def _path_instance(path) -> tuple[Path, Vault | None, str | None]:
    nominal = Path(path).absolute()
    path = nominal.resolve()
    with _guard:
        roots = sorted(_sessions, key=lambda x: len(x.parts), reverse=True)
        for root in roots:
            if nominal.is_relative_to(root) and not path.is_relative_to(root):
                raise ValueError('加密工作区文件不能通过链接指向工作区之外')
            if path.is_relative_to(root):
                return path, _sessions[root], path.relative_to(root).as_posix()
    for parent in path.parents:
        if is_enabled(parent):
            raise VaultLocked('加密工作区已锁定')
    return path, None, None


def read_bytes(path) -> bytes:
    path, instance, relative = _path_instance(path)
    content = path.read_bytes()
    return instance.unseal(content, relative) if instance is not None else content


def write_bytes(path, data: bytes) -> None:
    path, instance, relative = _path_instance(path)
    _atomic_write(path, instance.seal(data, relative) if instance is not None else data)


def file_size(path) -> int:
    path, instance, _ = _path_instance(path)
    size = path.stat().st_size
    if instance is not None:
        with path.open('rb') as source:
            if source.read(8) != FILE_MAGIC or size < 36:
                raise ValueError('加密文件格式无效')
        size -= 36
    return size


def _protect_archive(source: Path, destination: Path, instance: Vault) -> None:
    """Stream old ZIP backups into an authenticated envelope with its own keys."""
    nonce = secrets.token_bytes(12)
    header = _json_bytes({'vault': instance.metadata, 'nonce': _b64(nonce)})
    prefix = ARCHIVE_MAGIC + struct.pack('>I', len(header)) + header
    encryptor = Cipher(algorithms.AES(_derive(instance.master, 'archives')), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(prefix)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as inp, destination.open('xb') as output:
        output.write(prefix)
        while chunk := inp.read(1024 * 1024):
            output.write(encryptor.update(chunk))
        output.write(encryptor.finalize())
        output.write(encryptor.tag)
        output.flush()
        os.fsync(output.fileno())


def is_protected_archive(path) -> bool:
    with Path(path).open('rb') as source:
        return source.read(8) == ARCHIVE_MAGIC


@contextmanager
def open_archive(source, root=None, password=None, recovery_key=None):
    """Yield a ZIP-readable stream. Decrypted legacy archives stay in RAM only.

    Newly created backups contain encrypted SQLite/objects directly and need no
    whole-archive RAM buffering. This envelope is used for pre-encryption ZIPs.
    """
    with Path(source).open('rb') as inp:
        if inp.read(8) != ARCHIVE_MAGIC:
            inp.seek(0)
            yield inp
            return
        length_bytes = inp.read(4)
        if len(length_bytes) != 4:
            raise ValueError('加密备份文件已损坏')
        length = struct.unpack('>I', length_bytes)[0]
        if not 0 < length <= 16384:
            raise ValueError('加密备份头无效')
        header = inp.read(length)
        try:
            info = json.loads(header)
            instance = _instance_for_metadata(info['vault'], root, password, recovery_key)
            nonce = _unb64(info['nonce'], 12)
        except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('加密备份元数据无效') from exc
        offset = inp.tell()
        inp.seek(0, os.SEEK_END)
        body_size = inp.tell() - offset - 16
        if not 0 <= body_size <= MAX_ARCHIVE:
            raise ValueError('加密备份大小无效或超过 2 GB')
        inp.seek(-16, os.SEEK_END)
        tag = inp.read(16)
        inp.seek(offset)
        decryptor = Cipher(algorithms.AES(_derive(instance.master, 'archives')), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(ARCHIVE_MAGIC + length_bytes + header)
        result = io.BytesIO()
        try:
            remaining = body_size
            while remaining:
                chunk = inp.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('加密备份文件不完整')
                result.write(decryptor.update(chunk))
                remaining -= len(chunk)
            try:
                result.write(decryptor.finalize())
            except InvalidTag as exc:
                raise ValueError('加密备份校验失败') from exc
            result.seek(0)
            result.vault_context = instance
            yield result
        finally:
            result.close()


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def recover_migration(root) -> str | None:
    """Resolve an interrupted encryption switch before opening any database."""
    root = Path(root).resolve()
    journal = root / JOURNAL
    if not journal.is_file():
        return None
    if journal.stat().st_size > 65536:
        raise ValueError('加密迁移记录损坏，请保留原文件')
    state = json.loads(journal.read_text(encoding='utf8'))
    if (not re.fullmatch(r'\.vault-stage-[a-f0-9]{32}', state.get('stage', '')) or
            not re.fullmatch(r'\.vault-original-[a-f0-9]{32}', state.get('original', '')) or
            not isinstance(state.get('components'), list)):
        raise ValueError('加密迁移路径无效，请保留原文件')
    names = state['components']
    if any(not isinstance(name, str) or Path(name).name != name or name in ('.', '..', '')
           or '/' in name or '\\' in name for name in names):
        raise ValueError('加密迁移组件无效')
    stage, original = root / state['stage'], root / state['original']
    committed = is_enabled(root) and _read_metadata(root / META)['id'] == state['id']
    if not committed:
        for name in reversed(names):
            previous = original / name
            if previous.exists():
                _remove(root / name)
                os.replace(previous, root / name)
            elif name not in state['existed']:
                _remove(root / name)
    _remove(stage)
    _remove(original)
    journal.unlink()
    _fsync_dir(root)
    return 'committed' if committed else 'rolled_back'


def _encrypt_database(source: Path, destination: Path, instance: Vault) -> None:
    import sqlcipher3
    db = sqlcipher3.connect(str(source))
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('原数据库完整性校验失败，未开启加密')
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.execute('PRAGMA temp_store=MEMORY')
        db.execute('ATTACH DATABASE ? AS vault_target KEY "x\'' + instance.database_key.hex() + '\'"', (str(destination),))
        db.execute("SELECT sqlcipher_export('vault_target')")
        version = int(db.execute('PRAGMA user_version').fetchone()[0])
        db.execute('PRAGMA vault_target.user_version=' + str(version))
        db.execute('DETACH DATABASE vault_target')
    finally:
        db.close()
    check = _connect_cipher(destination, instance, True)
    try:
        if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or check.execute('PRAGMA cipher_integrity_check').fetchall():
            raise ValueError('加密数据库校验失败')
    finally:
        check.close()


def enable(root, password: str) -> dict:
    """Enable while all database/background connections are closed.

    The caller verifies the existing login password first. The returned recovery
    key is shown once; it is never written to disk in plaintext by this module.
    """
    root = Path(root).resolve()
    recover_migration(root)
    if is_enabled(root):
        raise ValueError('此工作区已开启加密')
    if (root / '.recovery-operation.json').exists():
        raise ValueError('请先完成上次数据恢复，再开启加密')
    if len(password) < 8:
        raise ValueError('密码至少 8 位')
    if not (root / 'tasks.sqlite3').is_file():
        raise ValueError('工作区数据库不存在')
    if (root / 'tasks.sqlite3').is_symlink():
        raise ValueError('数据库不能是符号链接')
    recovery = _new_recovery_key()
    metadata = {'version': 1, 'cipher': 'SQLCipher4+AES256GCM', 'id': uuid.uuid4().hex,
                'kdf': 'scrypt-N32768-r8-p1', 'salt': _b64(secrets.token_bytes(16))}
    master = secrets.token_bytes(32)
    metadata['password'] = _wrap(master, _password_key(password, _unb64(metadata['salt'], 16)), metadata, 'password')
    metadata['recovery'] = _wrap(master, _derive(_recovery_bytes(recovery), 'recovery-wrapper'), metadata, 'recovery')
    _sign_metadata(metadata, master)
    instance = Vault(root, metadata, master)
    stage = root / ('.vault-stage-' + uuid.uuid4().hex)
    original = root / ('.vault-original-' + uuid.uuid4().hex)
    stage.mkdir()
    original.mkdir()
    names = ['tasks.sqlite3']
    try:
        _encrypt_database(root / 'tasks.sqlite3', stage / 'tasks.sqlite3', instance)
        for dirname in OBJECT_DIRS:
            source = root / dirname
            if not source.exists():
                continue
            names.append(dirname)
            (stage / dirname).mkdir()
            for path in source.rglob('*'):
                if path.is_symlink():
                    raise ValueError('工作区包含符号链接，请先移除链接后再开启加密')
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    _atomic_write(stage / relative, instance.seal(path.read_bytes(), relative))
        # All backups the application keeps under its own data root are covered.
        # User exports on other drives are deliberately outside this scope.
        for source in root.iterdir():
            if source.name.startswith('.vault-') or source.name in names or source.name == META:
                continue
            if source.is_file() and source.suffix.lower() == '.zip':
                names.append(source.name)
                _protect_archive(source, stage / source.name, instance)
            elif source.is_dir() and source.name in ('backups', 'auto-backups', 'recovery-backups'):
                names.append(source.name)
                (stage / source.name).mkdir()
                for path in source.rglob('*'):
                    if path.is_symlink():
                        raise ValueError('备份目录包含符号链接')
                    if path.is_file():
                        relative = path.relative_to(root)
                        _protect_archive(path, stage / relative, instance)
            elif source.is_dir() and re.fullmatch(r'(?:previous|interrupted)-generation-[a-f0-9]{32}', source.name):
                names.append(source.name)
                destination = stage / source.name
                if is_enabled(source):
                    # A previously encrypted generation has its own keys. Keep it
                    # byte-for-byte rather than silently changing its password.
                    shutil.copytree(source, destination)
                    continue
                destination.mkdir()
                if (source / 'tasks.sqlite3').exists():
                    _encrypt_database(source / 'tasks.sqlite3', destination / 'tasks.sqlite3', instance)
                for dirname in OBJECT_DIRS:
                    if not (source / dirname).exists():
                        continue
                    (destination / dirname).mkdir()
                    for path in (source / dirname).rglob('*'):
                        if path.is_symlink():
                            raise ValueError('历史恢复副本包含符号链接')
                        if path.is_file():
                            logical = path.relative_to(source).as_posix()
                            _atomic_write(destination / logical, instance.seal(path.read_bytes(), logical))
                _atomic_write(destination / META, _json_bytes(metadata))
        _atomic_write(stage / META, _json_bytes(metadata))
        names.append(META)  # Commit marker must be last.
        state = {'version': 1, 'id': metadata['id'], 'stage': stage.name, 'original': original.name,
                 'components': names, 'existed': [name for name in names if (root / name).exists()]}
        _atomic_write(root / JOURNAL, _json_bytes(state))
        for name in names:
            if (root / name).exists():
                os.replace(root / name, original / name)
            os.replace(stage / name, root / name)
        _fsync_dir(root)
        # Journaled cleanup also completes safely after an interruption.
        recover_migration(root)
        with _guard:
            _sessions[root] = instance
        return {'enabled': True, 'recovery_key': recovery, 'workspace_id': metadata['id']}
    except BaseException:
        if (root / JOURNAL).is_file():
            recover_migration(root)
        else:
            _remove(stage)
            _remove(original)
        raise


def change_password(root, old_password: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise ValueError('密码至少 8 位')
    root = Path(root).resolve()
    metadata = _read_metadata(root / META)
    master = _unwrap(metadata, password=old_password)
    metadata['salt'] = _b64(secrets.token_bytes(16))
    metadata['password'] = _wrap(master, _password_key(new_password, _unb64(metadata['salt'], 16)), metadata, 'password')
    _sign_metadata(metadata, master)
    _atomic_write(root / META, _json_bytes(metadata))
    with _guard:
        if root in _sessions:
            _sessions[root].metadata = metadata


def recover_password(root, recovery_key: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise ValueError('密码至少 8 位')
    root = Path(root).resolve()
    metadata = _read_metadata(root / META)
    master = _unwrap(metadata, recovery_key=recovery_key)
    metadata['salt'] = _b64(secrets.token_bytes(16))
    metadata['password'] = _wrap(master, _password_key(new_password, _unb64(metadata['salt'], 16)), metadata, 'password')
    _sign_metadata(metadata, master)
    _atomic_write(root / META, _json_bytes(metadata))
    with _guard:
        if root in _sessions:
            _sessions[root].metadata = metadata


def new_recovery_key(root, password: str) -> str:
    root = Path(root).resolve()
    metadata = _read_metadata(root / META)
    master = _unwrap(metadata, password=password)
    recovery = _new_recovery_key()
    metadata['recovery'] = _wrap(master, _derive(_recovery_bytes(recovery), 'recovery-wrapper'), metadata, 'recovery')
    _sign_metadata(metadata, master)
    _atomic_write(root / META, _json_bytes(metadata))
    with _guard:
        if root in _sessions:
            _sessions[root].metadata = metadata
    return recovery
