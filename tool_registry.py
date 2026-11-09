"""Shared tool contracts and permission checks, independent of model intent labels."""
from dataclasses import dataclass
import time
import uuid

from execution_context import check_execution, current_execution


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict
    permission: str
    effect: str
    timeout_seconds: float
    retry: str
    cancellation: str


def catalogue():
    import app_tools,command_tools,file_tools,web_tools
    from config import COMMAND_MAX_TIMEOUT,WEB_TIMEOUT
    result={}
    for schemas,permission in ((web_tools.TOOL_SCHEMAS,'web'),(file_tools.TOOL_SCHEMAS,'files'),([command_tools.TOOL_SCHEMA],'commands'),(app_tools.TOOL_SCHEMAS,'commands')):
        for schema in schemas:
            name=schema['function']['name']
            effect='write' if name in ('write_file','edit_file','create_directory','create_contact','create_reminder') else 'process' if permission=='commands' else 'read'
            result[name]=ToolSpec(name,schema,permission,effect,COMMAND_MAX_TIMEOUT if permission=='commands' else WEB_TIMEOUT if permission=='web' else 30,
                                  'never_automatic' if effect!='read' else 'bounded',
                                  'process_group' if name=='run_command' else 'cooperative_between_calls')
    return result


def validate(arguments,schema):
    if not isinstance(arguments,dict):raise ValueError('Аргументы инструмента должны быть объектом')
    properties=schema.get('properties',{})
    if set(arguments)-set(properties):raise ValueError('Неизвестные аргументы инструмента')
    if set(schema.get('required',[]))-set(arguments):raise ValueError('Отсутствуют обязательные аргументы инструмента')
    for name,value in arguments.items():
        rule=properties[name];kind=rule.get('type')
        if isinstance(kind,list):
            if value is None and 'null' in kind:continue
            kind=next((k for k in kind if k!='null'),None)
        valid={'string':isinstance(value,str),'integer':isinstance(value,int) and not isinstance(value,bool),
               'number':isinstance(value,(float,int)) and not isinstance(value,bool),'boolean':isinstance(value,bool),
               'array':isinstance(value,list),'object':isinstance(value,dict),'null':value is None}
        if kind in valid and not valid[kind]:raise ValueError('Некорректный тип аргумента '+name)
        if 'enum' in rule and value not in rule['enum']:raise ValueError('Недопустимое значение '+name)
        if isinstance(value,str) and len(value)>100000:raise ValueError('Аргумент инструмента слишком длинный')
        if isinstance(value,(int,float)) and not isinstance(value,bool):
            import math
            if not math.isfinite(value) or value<rule.get('minimum',float('-inf')) or value>rule.get('maximum',float('inf')):
                raise ValueError('Аргумент вне допустимого диапазона')


def invoke(name,arguments,*,granted_names,allow_files=False,allow_commands=False,web_enabled=False):
    specs=catalogue();spec=specs.get(name)
    if not spec or name not in granted_names or not {'files':allow_files,'commands':allow_commands,'web':web_enabled}.get(spec.permission):
        return {'error':'This tool is not authorized for this request'}
    validate(arguments,spec.schema['function']['parameters'])
    check_execution()
    observer=current_execution().tool_observer
    ident=uuid.uuid4().hex;started=time.monotonic()
    if observer:observer(dict(id=ident,name=name,effect=spec.effect,status='started',retry=spec.retry))
    result=None
    try:
        import app_tools,command_tools,file_tools,web_tools
        if current_execution().local_agent and spec.permission in ('files','commands'):
            from local_agent import invoke as invoke_local
            result=invoke_local(spec,arguments,dict(files=allow_files,commands=allow_commands))
        elif name in app_tools.TOOLS:result=app_tools.TOOLS[name](**arguments)
        elif name=='run_command':result=command_tools.run_command(**arguments)
        elif name in file_tools.TOOLS:result=file_tools.execute_tool(name,arguments)
        else:result=web_tools.execute_tool(name,arguments)
        return result
    finally:
        if observer:observer(dict(id=ident,name=name,effect=spec.effect,status='failed' if result is None or result.get('error') else 'completed',
                                  duration_seconds=round(time.monotonic()-started,3),retry=spec.retry))


def public_catalogue():
    return [dict(name=s.name,schema=s.schema,permission=s.permission,effect=s.effect,timeout_seconds=s.timeout_seconds,
                 retry=s.retry,cancellation=s.cancellation) for s in catalogue().values()]
