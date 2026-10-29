"""Run user-requested console commands with bounded output and a process-group timeout.

The working directory is validated, but this is not an OS filesystem sandbox.
Commands execute with the bot user's normal OS permissions.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import os
import selectors
import signal
import subprocess
import time
import uuid

from config import COMMAND_TIMEOUT, COMMAND_MAX_TIMEOUT, COMMAND_MAX_OUTPUT_BYTES, COMMAND_SHELL
from file_tools import resolve_path

_actions = ContextVar('command_actions', default=None)
_observer = ContextVar('command_observer', default=None)


@contextmanager
def capture_command_actions(observer=None):
    actions = []
    token = _actions.set(actions)
    observer_token = _observer.set(observer)
    try:
        yield actions
    finally:
        _actions.reset(token)
        _observer.reset(observer_token)


def _record(action):
    if _observer.get() is not None:
        _observer.get()(dict(action))


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_command(command: str, cwd: str = '.', timeout: int = COMMAND_TIMEOUT) -> dict:
    """Execute a console command using zsh in an allowed working directory. Returns exit_code, stdout, stderr and timed_out. Commands have normal user permissions, not a filesystem sandbox."""
    try:
        if not isinstance(command, str) or not command.strip() or len(command) > 8000 or '\x00' in command:
            raise ValueError('Provide a nonempty command up to 8000 characters')
        directory = resolve_path(cwd)
        if not directory.is_dir():
            raise ValueError('Working directory does not exist')
        timeout = max(1, min(COMMAND_MAX_TIMEOUT, int(timeout)))
    except (OSError, ValueError, TypeError) as error:
        return {'error': str(error)}
    action = {'id': uuid.uuid4().hex, 'command': command, 'cwd': str(directory),
              'timeout': timeout, 'started_at': datetime.now(timezone.utc).isoformat(), 'started': False}
    if _actions.get() is not None:
        _actions.get().append(action)
    # Persist the attempt before starting: a restart must not replay an ambiguous command.
    _record(action)
    environment = dict(os.environ)
    for key in ('MAX_BOT_TOKEN', 'LM_API_KEY', 'DEEPSEEK_API_KEY', 'LOCAL_LM_API_KEY'):
        environment.pop(key, None)
    environment['PWD'] = str(directory)
    process = None
    selector = selectors.DefaultSelector()
    streams = {'stdout': bytearray(), 'stderr': bytearray()}
    truncated = False
    timed_out = False
    started = time.monotonic()
    try:
        process = subprocess.Popen([COMMAND_SHELL, '-f', '-c', command], cwd=str(directory),
                                   env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        action.update(started=True, pid=process.pid)
        _record(action)
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        deadline = started + timeout
        while selector.get_map() or process.poll() is None:
            if time.monotonic() >= deadline:
                if timed_out:
                    break
                timed_out = True
                _kill_group(process)
                deadline = time.monotonic() + 2
            for key, _ in selector.select(timeout=0.1):
                data = os.read(key.fileobj.fileno(), 16384)
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffer = streams[key.data]
                room = max(0, COMMAND_MAX_OUTPUT_BYTES // 2 - len(buffer))
                buffer.extend(data[:room])
                truncated = truncated or len(data) > room
        if process.poll() is None:
            _kill_group(process)
        exit_code = process.wait(timeout=3)
        action.update(exit_code=exit_code, timed_out=timed_out,
                      duration_seconds=round(time.monotonic() - started, 3))
        _record(action)
        return {**action, 'stdout': streams['stdout'].decode('utf-8', errors='replace'),
                'stderr': streams['stderr'].decode('utf-8', errors='replace'),
                'output_truncated': truncated}
    except (OSError, subprocess.SubprocessError) as error:
        action['error'] = type(error).__name__
        _record(action)
        return {'error': 'Console command failed to start or finish', 'cwd': str(directory)}
    finally:
        if process is not None:
            if process.poll() is None:
                _kill_group(process)
                process.wait(timeout=3)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        selector.close()


TOOL_SCHEMA = {'type': 'function', 'function': {'name': 'run_command', 'description': run_command.__doc__,
    'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'},
                   'cwd': {'type': 'string'}, 'timeout': {'type': 'integer'}}, 'required': ['command']}}}
