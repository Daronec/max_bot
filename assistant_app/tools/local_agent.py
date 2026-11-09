"""One-request local executor using authenticated-by-inheritance stdio, never a listener.

The parent confirms that each action intent is durably saved before execution can
continue. Process separation is not an operating-system filesystem sandbox.
"""
import contextlib
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time

from assistant_app.core.execution_context import ExecutionContext, ExecutionStopped, check_execution, current_execution, execution_session
from assistant_app.paths import PROJECT_ROOT


def invoke(spec,arguments,grants):
    from assistant_app.tools.file_tools import capture_file_actions, _observe_file
    from assistant_app.tools.command_tools import capture_command_actions, _record
    from assistant_app.tools import file_tools
    from assistant_app.tools import command_tools
    check_execution()
    context=current_execution()
    process=subprocess.Popen([sys.executable,'-m','assistant_app.tools.local_agent','serve'],cwd=PROJECT_ROOT,stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1,start_new_session=True)
    actions={}
    selector=selectors.DefaultSelector()
    try:
        process.stdin.write(json.dumps(dict(name=spec.name,arguments=arguments,grants=grants))+'\n');process.stdin.flush()
        selector.register(process.stdout,selectors.EVENT_READ)
        deadline=time.monotonic()+max(35,spec.timeout_seconds+15)
        while True:
            check_execution()
            if time.monotonic()>deadline:raise ExecutionStopped('deadline')
            ready=selector.select(.1)
            if not ready:
                if process.poll() is not None:raise RuntimeError('Локальный агент завершился без результата')
                continue
            line=process.stdout.readline(2*1024*1024)
            if not line:raise RuntimeError('Локальный агент прервал протокол')
            event=json.loads(line)
            if event['type']=='result':return event['value']
            if event['type']=='stopped':raise ExecutionStopped(event.get('reason','cancelled'))
            if event['type']=='error':raise RuntimeError('Локальное действие не подтверждено')
            if event['type'] not in ('file','command'):raise RuntimeError('Некорректный протокол агента')
            action=event['value'];key=(event['type'],action['id'])
            if key not in actions:
                actions[key]=dict(action)
                collection=file_tools._actions.get() if event['type']=='file' else command_tools._actions.get()
                if collection is not None:collection.append(actions[key])
            else:actions[key].update(action)
            if event['type']=='file':_observe_file(actions[key])
            else:_record(actions[key])
            # The child cannot start its mutation/command without this acknowledgement.
            process.stdin.write('{"ack":true}\n');process.stdin.flush()
    finally:
        selector.close()
        if process.poll() is None:
            if process.stdin and not process.stdin.closed:process.stdin.close()
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
        for pipe in (process.stdin,process.stdout):
            if pipe:pipe.close()


def serve():
    from assistant_app.tools.tool_registry import catalogue,validate
    from assistant_app.tools import app_tools
    from assistant_app.tools import command_tools
    from assistant_app.tools import file_tools
    parent=os.getppid();stop=threading.Event()
    def shutdown(*_):stop.set()
    signal.signal(signal.SIGTERM,shutdown)
    def watch_parent():
        while not stop.wait(.1):
            if os.getppid()!=parent:stop.set()
    threading.Thread(target=watch_parent,daemon=True).start()
    def emit(kind,value):
        sys.__stdout__.write(json.dumps(dict(type=kind,value=value),ensure_ascii=False)+'\n');sys.__stdout__.flush()
        if kind in ('file','command'):
            if json.loads(sys.stdin.readline()).get('ack') is not True:raise ExecutionStopped('cancelled')
    try:
        request=json.loads(sys.stdin.readline(1024*1024))
        spec=catalogue().get(request['name']);grants=request['grants']
        if not spec or spec.permission not in ('files','commands') or grants.get(spec.permission) is not True:
            raise ValueError('Unauthorized local action')
        validate(request['arguments'],spec.schema['function']['parameters'])
        with execution_session(ExecutionContext(local_only=True,cancel_check=stop.is_set)),contextlib.redirect_stdout(sys.stderr),file_tools.capture_file_actions(lambda action:emit('file',action)),command_tools.capture_command_actions(lambda action:emit('command',action)):
            check_execution()
            if spec.name in app_tools.TOOLS:result=app_tools.TOOLS[spec.name](**request['arguments'])
            elif spec.name=='run_command':result=command_tools.run_command(**request['arguments'])
            else:result=file_tools.execute_tool(spec.name,request['arguments'])
        emit('result',result)
    except ExecutionStopped as error:
        sys.__stdout__.write(json.dumps(dict(type='stopped',reason=error.reason))+'\n');sys.__stdout__.flush()
    except Exception:emit('error',{})
    finally:stop.set()


if __name__=='__main__':serve()
