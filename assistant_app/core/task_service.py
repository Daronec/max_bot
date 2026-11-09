"""Channel-independent task registration and lifecycle metadata.

Persistence and locking are supplied by the host. This keeps the service usable
without Flask, MAX, or a running model and preserves the existing storage format.
"""
import logging
import uuid
from datetime import datetime, timezone


logger = logging.getLogger(__name__)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def record_lifecycle(task):
    """Add metadata to new and legacy records; never copy message content to events."""
    task.setdefault('run_id', uuid.uuid4().hex)
    events = task.setdefault('events', [])
    status = task['status']
    if events and events[-1]['status'] == status:
        return
    event = dict(sequence=len(events) + 1, type='run.status_changed',
                 run_id=task['run_id'], status=status, timestamp=timestamp())
    events.append(event)
    logger.info('run.status_changed run_id=%s status=%s sequence=%s',
                task['run_id'], status, event['sequence'])


class TaskConflict(RuntimeError):
    """Another writer changed the task; reload before retrying."""


class TaskService:
    def __init__(self, tasks, lock, save, lookup=None):
        self.tasks = tasks
        self.lock = lock
        self.save = save
        self.lookup = lookup

    def submit(self, task_id, message, chat_id, source, status='queued', **metadata):
        """Register once. Returns (record, created); callers choose duplicate policy."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError('Требуется непустой текст')
        with self.lock:
            stored = self.lookup(task_id) if self.lookup else None
            if stored is not None:
                self.tasks[task_id] = stored
            if task_id in self.tasks:
                return self.tasks[task_id], False
            task = dict(metadata, task_id=task_id, message=message, chat_id=chat_id,
                        source=source, status=status, timestamp=timestamp(), result=None)
            record_lifecycle(task)
            try:
                self.save(task)
            except TaskConflict:
                stored = self.lookup(task_id) if self.lookup else None
                if stored is None:
                    raise
                self.tasks[task_id] = stored
                return stored, False
            self.tasks[task_id] = task
            return task, True
