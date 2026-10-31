"""Version-checked text file operations inside explicitly configured directories."""
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from config import BASE_DIR, FS_ROOTS, FS_SEARCH_ROOTS, FS_MAX_BYTES, FS_MAX_READ_CHARS

_lock = threading.RLock()
_actions = ContextVar('file_actions', default=None)
_action_observer = ContextVar('file_action_observer', default=None)
BACKUP_DIR = BASE_DIR / '.bot_file_backups'
PROTECTED_PATHS = frozenset((BASE_DIR / name).resolve() for name in (
    'voice_clone.py', 'setup_voice_clone.py', 'requirements-tts.txt', 'command_menu.py', 'project_changes.py', 'project_agent.py', 'code_deploy.py', 'tool_protocol.py',
    'personal_memory.py', 'memory_agent.py', 'assistant_identity.py',
    'config.py', 'file_tools.py', 'task_processor.py', 'server.py', 'bot_client.py',
    'bot_client_v2.py', 'web_tools.py', 'runtime.py', 'run.py', 'run_simple.py',
    'notifications', 'mood', 'mood_checkin.py', 'agenda_queries.py', 'contacts_tools.py', 'deepseek_balance.py', 'contacts_bridge.py', 'contacts_requests', 'setup_contacts.sh', 'summaries', '.models', 'helpers', 'voice_tools.py', 'morning_summary.py', 'setup_agenda.sh', 'launch_server.py', 'Запустить MAX бот.command', 'requirements.txt', 'setup_max_tls.py', 'command_tools.py', 'lm_connection.py', 'app_tools.py', 'notification_forwarder.py',
))
PROTECTED_PARTS = frozenset(('.secrets', '.git', '.venv', '.ssh', '.aws', '.codex', '.agents',
                             '.bot_file_backups', '.voices', '.venv-tts', '.code_changes', '__pycache__'))


def digest(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def capture_file_actions(observer=None):
    actions = []
    token = _actions.set(actions)
    observer_token = _action_observer.set(observer)
    try:
        yield actions
    finally:
        _actions.reset(token)
        _action_observer.reset(observer_token)


def resolve_path(path, write=False, search=False):
    if not isinstance(path, str) or not path.strip():
        raise ValueError('Provide a file or directory path')
    roots = tuple(Path(root).expanduser().resolve() for root in
                  ((*FS_ROOTS, *FS_SEARCH_ROOTS) if search else FS_ROOTS))
    if not roots:
        raise PermissionError('No filesystem directories are configured')
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    candidate = candidate.resolve()
    root = next((root for root in roots if candidate == root or root in candidate.parents), None)
    if root is None:
        raise PermissionError('Path is outside FS_ROOTS')
    relative = candidate.relative_to(root)
    if (any(part in PROTECTED_PARTS or part == '.env' or part.startswith('.env.')
            for part in relative.parts)
            or candidate == (BASE_DIR / 'config.py').resolve()
            or (write and candidate in PROTECTED_PATHS)
            or (write and any(candidate == BASE_DIR / name or BASE_DIR / name in candidate.parents
                              for name in ('tasks', 'logs', 'certs')))
            or (BASE_DIR / 'certs') in candidate.parents
            or candidate.suffix.lower() in ('.key', '.p12', '.pfx', '.keystore')
            or candidate.name in ('id_rsa', 'id_ed25519', 'credentials.json')):
        raise PermissionError('This credential or bot control path is protected')
    if candidate.is_file() and candidate.stat().st_nlink > 1:
        raise PermissionError('Hard-linked files are not supported')
    return candidate


def read_bytes(path):
    if not path.is_file():
        raise FileNotFoundError('File not found')
    with path.open('rb') as stream:
        data = stream.read(FS_MAX_BYTES + 1)
    if len(data) > FS_MAX_BYTES:
        raise ValueError('File exceeds FS_MAX_BYTES')
    if b'\x00' in data:
        raise ValueError('Only UTF-8 text files are supported')
    data.decode('utf-8')
    return data


def _error(error):
    if isinstance(error, FileNotFoundError):
        return {'error': 'File or directory not found'}
    if isinstance(error, (PermissionError, ValueError)):
        return {'error': str(error)}
    return {'error': 'Filesystem operation failed'}


def list_directory(path: str = '.', offset: int = 0, limit: int = 100) -> dict:
    """List a directory inside FS_ROOTS. Protected entries are omitted. Supports pagination."""
    try:
        directory = resolve_path(path)
        if not directory.is_dir():
            raise FileNotFoundError()
        offset, limit = max(0, int(offset)), max(1, min(200, int(limit)))
        entries = []
        for child in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
            try:
                resolved = resolve_path(str(child))
                info = resolved.stat()
                entries.append({'name': child.name, 'path': str(resolved),
                                'type': 'directory' if resolved.is_dir() else 'file',
                                'size': info.st_size})
            except (OSError, ValueError):
                continue
        return {'path': str(directory), 'entries': entries[offset:offset + limit],
                'total': len(entries), 'has_more': offset + limit < len(entries)}
    except (OSError, ValueError, TypeError) as error:
        return _error(error)


def read_file(path: str, start_line: int = 1, max_lines: int = 200) -> dict:
    """Read a UTF-8 file. Returns its full-file SHA256 required for subsequent modifications."""
    try:
        target = resolve_path(path)
        data = read_bytes(target)
        lines = data.decode('utf-8').splitlines(keepends=True)
        start_line, max_lines = max(1, int(start_line)), max(1, min(1000, int(max_lines)))
        selected = ''.join(lines[start_line - 1:start_line - 1 + max_lines])
        return {'path': str(target), 'content': selected[:FS_MAX_READ_CHARS], 'sha256': digest(data),
                'start_line': start_line, 'total_lines': len(lines),
                'truncated': len(selected) > FS_MAX_READ_CHARS or start_line - 1 + max_lines < len(lines)}
    except (OSError, ValueError, TypeError) as error:
        return _error(error)


def _save(target, content, expected_sha256):
    if not isinstance(content, str):
        raise ValueError('content must be text')
    new = content.encode('utf-8')
    if len(new) > FS_MAX_BYTES or b'\x00' in new:
        raise ValueError('Only UTF-8 text up to FS_MAX_BYTES is supported')
    if not target.parent.is_dir():
        raise FileNotFoundError('Create the parent directory first')
    existed = target.exists()
    old = read_bytes(target) if existed else None
    if old == new:
        return {'path': str(target), 'changed': False, 'sha256': digest(new)}
    if existed and (not expected_sha256 or digest(old) != expected_sha256):
        raise ValueError('File changed or expected_sha256 missing. Read the file again before editing.')
    if not existed and expected_sha256:
        raise ValueError('File no longer exists. Read or list the directory again.')
    backup_id = None
    if existed:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_id = uuid.uuid4().hex
        backup = BACKUP_DIR / (backup_id + '.txt')
        backup.write_bytes(old)
        backup.chmod(0o600)
        metadata = BACKUP_DIR / (backup_id + '.json')
        metadata.write_text(json.dumps({'path': str(target), 'sha256': digest(old)}, ensure_ascii=False), encoding='utf-8')
        metadata.chmod(0o600)
    descriptor, temporary = tempfile.mkstemp(prefix='.maxbot-', dir=str(target.parent))
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        if existed:
            os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
        # Recheck the version just before replacing the file.
        if target.exists() != existed or (existed and digest(read_bytes(target)) != digest(old)):
            raise ValueError('File changed during editing. Read it again.')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    receipt = {'path': str(target), 'changed': True, 'created': not existed,
               'sha256': digest(new), 'backup_id': backup_id}
    if _actions.get() is not None:
        _actions.get().append(receipt)
    if _action_observer.get() is not None:
        _action_observer.get()(receipt)
    return receipt


def write_file(path: str, content: str, expected_sha256: str = '') -> dict:
    """Create a UTF-8 file or replace its entire content. Existing files require SHA256 from read_file."""
    try:
        with _lock:
            return _save(resolve_path(path, write=True), content, expected_sha256)
    except (OSError, ValueError, TypeError) as error:
        return _error(error)


def edit_file(path: str, old_text: str, new_text: str, expected_sha256: str, replace_all: bool = False) -> dict:
    """Replace an exact text fragment after read_file. Ambiguous matches require replace_all=true."""
    try:
        if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
            raise ValueError('Provide nonempty old_text and a text replacement')
        with _lock:
            target = resolve_path(path, write=True)
            data = read_bytes(target)
            if digest(data) != expected_sha256:
                raise ValueError('File changed. Read it again before editing.')
            content = data.decode('utf-8')
            count = content.count(old_text)
            if count == 0:
                raise ValueError('old_text was not found; no changes made')
            if count > 1 and not replace_all:
                raise ValueError('Multiple matches; provide a larger exact fragment or replace_all=true')
            return _save(target, content.replace(old_text, new_text, -1 if replace_all else 1), expected_sha256)
    except (OSError, ValueError, TypeError) as error:
        return _error(error)


def create_directory(path: str) -> dict:
    """Create a directory inside FS_ROOTS, including missing parent directories."""
    try:
        with _lock:
            target = resolve_path(path, write=True)
            existed = target.is_dir()
            target.mkdir(parents=True, exist_ok=True)
            return {'path': str(target), 'created': not existed}
    except (OSError, ValueError, TypeError) as error:
        return _error(error)


def search_files(query: str, path: str = '.', max_results: int = 50) -> dict:
    """Find project/file paths by name across FS_ROOTS and metadata-only FS_SEARCH_ROOTS. Omit path to search all roots. Does not read file contents or follow symlinks."""
    try:
        if not isinstance(query, str) or not query.strip():
            raise ValueError('Provide a filename or filename pattern')
        configured = list(dict.fromkeys(Path(root).expanduser().resolve() for root in (*FS_ROOTS, *FS_SEARCH_ROOTS)))
        if path in ('.', ''):
            roots = configured
        else:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = Path(FS_ROOTS[0]) / candidate
            candidate = candidate.resolve()
            if any(candidate == root or root in candidate.parents for root in configured):
                roots = [resolve_path(str(candidate), search=True)]
            else:
                # A broad request such as '/' searches only configured project locations.
                roots = [root for root in configured if candidate in root.parents]
                if not roots:
                    raise PermissionError('Поиск доступен только в настроенных каталогах FS_ROOTS и FS_SEARCH_ROOTS')
        limit = max(1, min(100, int(max_results)))
        matches = []
        scanned = 0
        seen = set()
        def matches_query(name):
            return query.casefold() in name.casefold() or fnmatch.fnmatch(name.casefold(), query.casefold())
        for root in roots:
            if not root.is_dir():
                continue
            try:
                resolve_path(str(root), search=True)
            except (OSError, ValueError):
                continue
            if matches_query(root.name) and str(root) not in seen:
                matches.append({'path': str(root), 'type': 'directory'})
                seen.add(str(root))
            if len(matches) >= limit:
                return {'matches': matches, 'truncated': True}
            for directory, folders, files in os.walk(root, followlinks=False):
                allowed = []
                for name in folders:
                    try:
                        resolve_path(str(Path(directory) / name), search=True)
                        allowed.append(name)
                    except (OSError, ValueError):
                        pass
                folders[:] = allowed
                for name in sorted(folders + files):
                    scanned += 1
                    if scanned > 10000:
                        return {'matches': matches, 'truncated': True}
                    if not matches_query(name):
                        continue
                    try:
                        item = resolve_path(str(Path(directory) / name), search=True)
                        if str(item) in seen:
                            continue
                        seen.add(str(item))
                        matches.append({'path': str(item), 'type': 'directory' if item.is_dir() else 'file'})
                    except (OSError, ValueError):
                        continue
                    if len(matches) >= limit:
                        return {'matches': matches, 'truncated': True}
        return {'matches': matches, 'truncated': False, 'searched_roots': [str(root) for root in roots]}
    except (OSError, ValueError, TypeError) as error:
        return _error(error)


def schema(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': list(required)}}}


TEXT = {'type': 'string'}
INTEGER = {'type': 'integer'}
TOOL_SCHEMAS = [
    schema('list_directory', list_directory.__doc__, {'path': TEXT, 'offset': INTEGER, 'limit': INTEGER}),
    schema('read_file', read_file.__doc__, {'path': TEXT, 'start_line': INTEGER, 'max_lines': INTEGER}, ['path']),
    schema('search_files', search_files.__doc__, {'query': TEXT, 'path': TEXT, 'max_results': INTEGER}, ['query']),
    schema('write_file', write_file.__doc__, {'path': TEXT, 'content': TEXT, 'expected_sha256': TEXT}, ['path', 'content']),
    schema('edit_file', edit_file.__doc__, {'path': TEXT, 'old_text': TEXT, 'new_text': TEXT,
           'expected_sha256': TEXT, 'replace_all': {'type': 'boolean'}}, ['path', 'old_text', 'new_text', 'expected_sha256']),
    schema('create_directory', create_directory.__doc__, {'path': TEXT}, ['path']),
]
TOOLS = {tool['function']['name']: globals()[tool['function']['name']] for tool in TOOL_SCHEMAS}


def execute_tool(name, arguments):
    if name not in TOOLS or not isinstance(arguments, dict):
        return {'error': 'Unknown filesystem tool or invalid arguments'}
    try:
        return TOOLS[name](**arguments)
    except (ValueError, TypeError):
        return {'error': 'Invalid filesystem tool arguments'}
