# 插件事件恢复服务

## 功能说明

恢复程序为 `recovery.py`，当前版本 2.0.0，由统一离线套件分发并安装到 `/data/minas/plugins/recovery/`。持久配置位于 `/data/minas/plugins/data/<用户>/<插件>/`，插件原有 `etc/var` 链接指向这里；官方源码、缓存位置不变。发现非当前布局时停止并报告路径；不探测或搬移旧目录。

- `minas-plugin-recovery-v2-watch.service` 使用 inotify 监听注册表的原子替换，以及插件源码、入口、恢复登记和缓存目录本身的删除或重建。不监听配置、持久状态和缓存目录内部。
- 收到变化后等待 5 秒安静期，连续变化最多合并 30 秒；取得安装器互斥锁，并等待原厂 plugincenter 命令结束后处理。原厂操作仍活跃时继续等待。
- 注册表、图标和入口事件只核对状态；源码事件、开机检查或实际恢复时执行源码摘要及原厂校验。只恢复已登记插件，保留明确停用和页面停止意图。
- 原厂关机或卸载存储池时的批量 enable/disable 日志属于生命周期操作，不写成用户启停意图；同一进程下的批量成功记录全部排除。真正的单插件启停继续保留。恢复丢失文件或注册信息也不会强制启用原先禁用的插件。
- 启动时先建立监听再补查；开机前 90 秒等待原厂初始化。存储池未挂载时暂停对应记录，收到内核挂载通知后补查，忽略普通容器挂载变化。
- 源码目录重建、恢复登记更新及事件队列溢出会重建监听。异常失败后最多再重试两次，此后等待新事件或手动修复。
- Mihomo、文件快传和下载中心由独立用户服务托管，异常退出交给 systemd 重启。用户或卸载流程通过 systemctl 正常停止时不会触发自动重启。
- 旧的 30 秒定时器已移除。Cron 每分钟仅查询 systemd 中监听服务的存活状态，失败时才启动服务；单元丢失时再尝试 bootstrap。健康时不启动恢复程序。

## 如何使用

安装引擎负责登记、服务配置和启动；snapshot 仅保存已经验证的安装。恢复程序观察事件并调用公共引擎，写入和回退在同一事务内完成。以下命令在 NAS 的 root 终端执行：

```sh
systemctl status minas-plugin-recovery-v2-watch.service
cat /run/minas-plugin-recovery-v2/watch-status.json
python3 -B /data/minas/plugins/recovery/recovery.py status
```

手动完整检查和恢复：

```sh
python3 -B /data/minas/plugins/manager/cli.py repair --all-users --all-plugins
```

启动入口被固件清理但程序仍保留时：

```sh
python3 -B /data/minas/plugins/recovery/recovery.py bootstrap
```

查看错误：

```sh
journalctl -u minas-plugin-recovery-v2-watch.service --no-pager -n 50
```

## 注意事项

- 安装、在线更新和恢复共用 `verify()`：仅原厂校验子进程设置 `LC_ALL=C LC_COLLATE=C LANG=C`，并在 `runuser` 降权后再次设置，避免继承中文 locale 导致文件排序与 INFO 摘要不一致。不改变登录环境、插件语言或原厂校验器，不使用 `PLUG_STATUS=installing` 跳过检查。
- 可单独执行 `python3 -B /data/minas/plugins/recovery/recovery.py verify --user <用户> --plugin <插件>`；此命令不重建布局或启动服务。失败会输出原厂退出码、继承与生效的 locale、权限与临时目录检查、文件数及摘要对比；诊断也以插件用户执行，不输出源码内容。独立摘要一致不能替代原厂校验成功。

- `/run/minas-plugin-recovery-v2/` 仅保存可重建监听状态；恢复快照和持久启停意图继续位于受保护的 `/data/minas/plugins/recovery/records/`，插件持久数据不迁入内存目录。
- 监听空闲时等待内核通知，不定时读取插件源码。其他程序仍可能访问硬盘，不能保证整机休眠；实际恢复和手动完整检查会访问存储池。
- 不直接覆盖冲突源码。出现不同版本、源码改动、损坏注册表或缺失持久数据时保留现场，需处理错误后重新检查。
- 明确卸载请使用 `manage.sh uninstall`。应用市场清理后会自动补回已登记插件；原厂策略仍然存在。
- 若固件同时清除所有启动入口，需要手动 bootstrap。尚不能保证适配任意固件升级；本服务不自动执行任务或 SSH 恢复动作。

以下为 1.x 版本历史验收，不代表 2.0.0 已部署：2026-10-30 验证：45 项本地恢复测试通过，覆盖关机批量停用、真正的单插件停用、禁用插件文件恢复和独立服务意图。当前 NAS 两次整机重启后，Mihomo、下载中心、文件快传及定时任务均自动恢复，八个插件源码摘要与原厂校验通过；未替换现有任务、配置或 SSH 材料。

2.0.0 的本地与真机验收范围见 [统一安装器验证说明](../manager/VALIDATION.md)。普通卸载删除插件数据及恢复快照；仅保留数据重装保留原状态。
