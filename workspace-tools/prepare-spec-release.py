"""Sync standard-layout metadata, icons and standalone recovery."""
import json
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
from version_catalog import load
from sync_versions import synchronize

def prepare():
    from sync_data_layout import synchronize as sync_layout
    sync_layout()
    synchronize()
    for plugin,version in load()['plugins'].items():
        base=ROOT/plugin
        assert not (base/'payload').exists(),plugin
        config=json.loads((base/'src/ui/config').read_text(encoding='utf-8'))
        info=json.loads((base/'INFO').read_text(encoding='utf-8'))
        if info['version']!=version:info['timestamp']=int(time.time())
        info.update(plugin=plugin,version=version,name=config['title'],desc=config['desc'],
                    changelog=('只迁移当前插件所需数据，新旧公共目录并存，最后一个使用者迁走后清理旧公共文件' if (base/'installer/data_layout.py').is_file() else {'mihomo':'独立用户服务托管，修复恢复进程退出时代理被停止',
                                'quickshare':'修复失效 PID 导致端口占用，独立托管快传服务',
                                'downloadcenter':'只读运行锁减少空闲索引写入，系统服务接管保活与异常退出恢复',
                                'taskcenter':'增加调度服务启动和停止入口，保留任务启用状态',
                                'sshrecovery':'修正 SSH 开机和应急恢复的数据路径，清理旧目录空锁残留'}.get(plugin,'按标准目录重构源码及安装包，更新安装、卸载和恢复入口')),type='standard',system=False,ext={})
        info['size']=sum(p.stat().st_size for p in (base/'src').rglob('*') if p.is_file())
        (base/'INFO').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
        target=base/'installer/recovery/recovery.py';target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((ROOT/'workspace-tools/plugin-recovery/recovery.py').read_bytes())
        subprocess.run([sys.executable,'-B',str(ROOT/'workspace-tools/icons'/(plugin+'.py')),str(base/'src/ui/icon.png')],check=True)
if __name__=='__main__':prepare()
