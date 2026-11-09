"""Per-run context shared by the harness and model gateway, isolated by ContextVar."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time


@dataclass
class ExecutionContext:
    history: list = field(default_factory=list)
    model_target: str = 'auto'
    local_only: bool = False
    knowledge: str = ''
    personal_context: str = ''
    route_observer: object = None
    model_observer: object = None
    cancel_check: object = None
    deadline: float | None = None
    remote_enabled: bool = True
    model_overrides: dict = field(default_factory=dict)
    selected_backend: dict | None = None
    budget_reserver: object = None
    tool_observer: object = None
    local_agent: bool = False
    progress_observer: object = None
    backend_selector: object = None
    backend_failure: object = None
    web_enabled: bool | None = None


class ExecutionStopped(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('Выполнение отменено пользователем' if reason == 'cancelled' else 'Превышен лимит времени задачи')


def check_execution():
    context = current_execution()
    if context.cancel_check and context.cancel_check():
        raise ExecutionStopped('cancelled')
    if context.deadline is not None and time.monotonic() >= context.deadline:
        raise ExecutionStopped('deadline')


_current = ContextVar('execution_context', default=None)


def current_execution():
    return _current.get() or ExecutionContext()


@contextmanager
def execution_session(context):
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)
