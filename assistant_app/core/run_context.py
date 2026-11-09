"""Assemble the private context and execute a conversational turn."""
import time

from assistant_app.config import LOCAL_AGENT_ENABLED
from assistant_app.knowledge.rag import append_references, prepare_sources
from assistant_app.models.registry import ModelRegistry
from assistant_app.memory.personal import PERSONAL, SECRET
from assistant_app.projects.project_context import ProjectContext
from assistant_app.core.task_service import timestamp

from .context_builder import ContextBuilder
from .execution_context import ExecutionContext, check_execution, execution_session


def run_conversation(task, text, permissions, *, respond, store, settings,
                     knowledge_search, memory_context, persist, lock):
    """Run with bounded history and record model, tool and progress events."""
    deadline = time.monotonic() + settings['run_timeout']
    history = []
    sources = []
    private_conversation = False
    conversation_id = task.get('conversation_id')
    projects = ProjectContext(store)
    selected_project = projects.conversation_project(conversation_id) if conversation_id else None
    if conversation_id:
        built = ContextBuilder(store).build(conversation_id, task['user_id'])
        history = built['history']
        if built['summary_version'] is not None:
            task['context_summary_version'] = built['summary_version']
            persist(task)
        if task.get('use_knowledge', True):
            sources = knowledge_search(projects.scope(selected_project), text)

    def observed_route(decision):
        with lock:
            task['routing'] = dict(target=decision.target, reason=decision.reason,
                                   label=decision.label, score=decision.score)
            persist(task)

    project_notes = projects.context(selected_project)
    shared_memory = task.get('source') == 'desktop' or bool(task.get('memory_link'))
    owner_context = memory_context(text) if shared_memory else ''
    personal_context = '\n\n'.join(filter(None, (project_notes, owner_context)))
    if conversation_id:
        inputs = [text, *(item['content'] for item in history)]
        private_input = any(PERSONAL.search(value) or SECRET.search(value) for value in inputs)
        private_conversation = store.conversation_privacy(
            conversation_id, task['user_id'],
            mark_private=bool(permissions or sources or personal_context or private_input),
        )
    knowledge, references = prepare_sources(sources)
    if references:
        task['sources'] = references
        persist(task)

    def observed_model(details):
        with lock:
            task.setdefault('model_calls', []).append(dict(details, timestamp=timestamp()))
            persist(task)

    def observed_tool(details):
        with lock:
            events = task.setdefault('tool_executions', [])
            events.append(dict(details, timestamp=timestamp()))
            persist(task)

    def observed_progress(content):
        with lock:
            task['preview'] = content[:24000]
            persist(task)

    model_target = task.get('model_target', 'auto')
    source_channel = task.get('source')
    private_context = bool(permissions or private_conversation or personal_context or sources)
    local_only = (private_context or bool(task.get('memory_link')) or source_channel == 'telegram'
                  or (source_channel == 'desktop' and model_target in ('auto', 'qwen')))
    context = ExecutionContext(
        history=history, knowledge=knowledge, personal_context=personal_context,
        model_target=model_target, local_only=local_only,
        route_observer=observed_route, model_observer=observed_model, deadline=deadline,
        tool_observer=observed_tool,
        progress_observer=observed_progress, local_agent=LOCAL_AGENT_ENABLED,
        remote_enabled=settings['remote_enabled'], model_overrides=settings,
        web_enabled=settings['web_enabled'] and not private_context,
        cancel_check=lambda: bool((store.runtime('cancel_' + task['run_id']) or {}).get('requested')),
    )
    registry = ModelRegistry(store)
    context.budget_reserver = registry.reserve

    def backend_failed(backend):
        registry.unavailable(backend)
        with lock:
            task.setdefault('routing', {}).update(fallback_reason='registered_model_error', fallback_target='qwen')
            persist(task)

    context.backend_failure = backend_failed
    context.backend_selector = lambda label, force_local: registry.route(label,
        private=context.local_only or force_local, remote_enabled=context.remote_enabled, needs_tools=bool(permissions))
    if context.model_target.startswith('model:'):
        backend = registry.backend(context.model_target[6:])
        if backend['local'] or not context.local_only and context.remote_enabled:
            context.selected_backend = backend
        else:
            context.local_only = True
    with execution_session(context):
        check_execution()
        answer = respond()
        check_execution()
    return append_references(answer, references)
