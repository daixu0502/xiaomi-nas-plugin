"""Event-driven task deadlines and temporary SSH key expiry. Root-only service."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
import privileged_runtime as runtime
from event_wait import Events

UNIT = 'minas-maintenance-events.service'
MANAGER = Path('/data/minas/plugins/manager')


class MaintenancePending(Exception):
    pass


def require_available():
    # Called while holding component_use(), paired with the installer's gate.
    if (MANAGER / 'maintenance.json').exists():
        raise MaintenancePending()


def module(name, relative):
    path = runtime.BASE / relative
    runtime.secure_directory(path.parent)
    sys.path.insert(0,str(path.parent))
    loader=importlib.machinery.SourceFileLoader(name,str(path))
    spec=importlib.util.spec_from_loader(name,loader)
    result=importlib.util.module_from_spec(spec);loader.exec_module(result)
    return result


def next_task_deadline(tasks, state, now):
    if not state.get('scheduler'): return None
    due=[]; uptime=tasks.uptime_seconds(); boot=tasks.boot_id()
    for task in state.get('tasks',[]):
        pending=tasks.retry_pending(state,task['id'])
        if pending: due.append(max(now+1,pending['retryAt']));continue
        if not task.get('enabled'): continue
        kind=task['schedule']['type']
        if kind=='boot':
            delay=task['schedule']['delay']
            if task.get('lastSlot')!='boot:'+boot:
                due.append(now+max(1,delay-uptime) if uptime<delay else now+60)
        elif kind!='after_task':
            due.extend(tasks.schedules.upcoming(task,now,1))
    for run in state.get('runs',[]):
        if run.get('state')=='running':
            task=next((t for t in state['tasks'] if t['id']==run['taskId']),{})
            due.append(max(now+1,run['started']+task.get('timeout',300)+10))
    return min(due) if due else None


def run():
    if os.geteuid()!=0: raise PermissionError('root service only')
    stopped=False
    with Events() as events:
        def stop(*args):
            nonlocal stopped
            stopped=True;events.wake()
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        deadlines={}; failures={}; changed={('startup','')}; tasks=None; ssh=None
        while not stopped:
            events.watch(MANAGER, {'maintenance.json'})
            now=time.time()
            blocked=False
            reload=any(kind not in ('deadline','wake') for kind,name in changed)
            if reload:
                # No lifetime component lock: installers must be able to replace code.
                with runtime.component_use():
                    registrations=runtime.registry().get('users',{})
                    events.watch(runtime.BASE);events.watch(runtime.BASE/'users')
                    tasks=module('event_tasks','modules/taskcenter/task-center-helper') if any('taskcenter' in v for v in registrations.values()) else None
                    ssh=module('event_ssh','modules/sshrecovery/ssh-recovery-helper') if any('sshrecovery' in v for v in registrations.values()) else None
                deadlines={};failures={}
            if tasks:
                for user,grants in registrations.items():
                    if 'taskcenter' not in grants: continue
                    directory=tasks.home(user);events.watch(directory)
                    if not reload and deadlines.get(user,float('inf'))>now: continue
                    try:
                        with runtime.component_use():
                            require_available()
                            tasks.tick_user(user)
                        state=json.loads((directory/'state.json').read_text())
                        next_due=next_task_deadline(tasks,state,time.time())
                        failures.pop(user,None)
                        deadlines.pop(user,None)
                        if next_due is not None:deadlines[user]=next_due
                    except MaintenancePending:
                        blocked=True
                        break
                    except Exception as exc:
                        # Real failed work retries with a bound, not idle polling.
                        print('Task event failed: '+type(exc).__name__,flush=True)
                        deadlines.pop(user,None)
                        failures[user]=failures.get(user,0)+1
                        if failures[user]<=3:deadlines[user]=time.time()+min(60,15*failures[user])
            if blocked:
                changed=events.wait(None)
                continue
            if ssh and (reload or deadlines.get('ssh',float('inf'))<=now):
                deadlines.pop('ssh',None)
                events.watch(ssh.STATE);events.watch(ssh.STATE/'tmp')
                try:
                    with runtime.component_use():
                        require_available()
                        ssh.prune()
                except MaintenancePending:
                    changed=events.wait(None)
                    continue
                for folder in (ssh.STATE/'tmp').glob('generated-u*'):
                    if not __import__('re').fullmatch(r'generated-u[0-9]+',folder.name):continue
                    events.watch(folder)
                    try:
                        expires=float(json.loads((folder/'meta.json').read_text())['expires'])
                        deadlines['ssh']=min(deadlines.get('ssh',float('inf')),max(time.time()+1,expires))
                    except (OSError,ValueError,KeyError):pass
            changed=events.wait(min(deadlines.values()) if deadlines else None)
            changed={(kind,name) for kind,name in changed if kind in ('mount','overflow','clock','deadline','wake') or name in ('registry.json','state.json','meta.json','maintenance.json') or name.startswith(('generated-','u'))}


if __name__=='__main__':
    os.umask(0o077)
    run()
