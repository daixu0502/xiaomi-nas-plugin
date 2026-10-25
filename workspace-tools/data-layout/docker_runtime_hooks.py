"""Docker runtime handover only; image and volume directories are untouched."""

def _layout_lock_paths():
    roots=('/data/.minas-privileged','/data/minas/privileged')
    paths=[Path(root)/name for root in roots for name in ('component.lock','docker.lock')]
    paths += [Path(root)/'lock' for root in ('/data/docker-bridge-repair','/data/minas/docker/network')]
    paths += [Path(root)/'upgrade.lock' for root in ('/data/.minas-docker-runtime','/data/minas/docker/runtime')]
    return [Path('/data/plugin/.local-plugin-installer.lock'),*paths]


def _layout_journal_paths():
    return [Path(DATA_LAYOUT_ROOT)/'migration/docker-runtime/maintenance',
            *[UNIT_ROOT/(unit+'.d/98-minas-runtime-layout.conf') for unit in ('docker.service','docker.socket')]]


def _layout_runtime_capture(moves):
    old='/data/.minas-docker-runtime'
    if old not in moves:return None
    if any(json.loads(p.read_text()).get('phase') not in ('installed','rolled-back') for p in Path(old,'transactions').glob('*/state.json')):
        raise ValueError('Docker 有未完成的升级事务，请先处理后再迁移目录')
    if not DROPIN.is_file() or old not in DROPIN.read_text():return None
    service=active('docker.service');sock=active('docker.socket')
    if service!=sock:raise ValueError('Docker 服务与 socket 状态不一致，请先检查')
    if not service:return None
    info=json.loads(docker('info','--format','{{json .}}').stdout)
    if info.get('LiveRestoreEnabled') or info.get('Swarm',{}).get('LocalNodeState','inactive')!='inactive':raise ValueError('不支持迁移 live-restore 或 swarm 运行时')
    before=[dict(id=c['Id'],running=bool(c['State']['Running']),restart=c['HostConfig']['RestartPolicy']) for c in containers()]
    return dict(before=dict(containers=before),version=info['ServerVersion'])


def _layout_runtime_pause(state):
    paths=_layout_journal_paths();gate=paths[0]
    _layout_atomic(gate,b'maintenance\n')
    for path in paths[1:]:
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
        _layout_atomic(path,'[Unit]\nConditionPathExists=!'+str(gate)+'\n',0o644)
    _layout_run(['systemctl','daemon-reload'])
    for item in state['before']['containers']:docker('update','--restart=no',item['id'])
    ids=[c['id'] for c in state['before']['containers'] if c['running']]
    if ids:docker('stop','--time','30',*ids,timeout=180)
    _layout_run(['systemctl','stop','docker.socket','docker.service'])
    for proc in Path('/proc').glob('[0-9]*/exe'):
        try:exe=os.readlink(proc)
        except OSError:continue
        if exe.startswith('/data/.minas-docker-runtime/') and Path(exe).name in ('dockerd','containerd','containerd-shim-runc-v2','runc'):
            raise ValueError('旧 Docker 运行时尚未退出，未移动目录')


def _layout_runtime_restart(state,migrated):
    for path in _layout_journal_paths():path.unlink(missing_ok=True)
    _layout_run(['systemctl','daemon-reload'])
    _layout_run(['systemctl','start','docker.socket','docker.service'])
    deadline=time.monotonic()+60
    while True:
        result=docker('info','--format','{{json .}}',check=False,timeout=10)
        if result.returncode==0:break
        if time.monotonic()>=deadline:raise ValueError('迁移后 Docker 未能就绪')
        time.sleep(1)
    if json.loads(result.stdout)['ServerVersion']!=state['version']:raise ValueError('迁移前后 Docker 版本不一致')
    restore_running(state)
