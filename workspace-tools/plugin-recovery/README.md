# 插件事件恢复服务

## 功能说明

恢复程序为 `recovery.py`，当前版本 1.3.0，由七插件安装器分发并安装到 `/data/.minas-plugin-recovery/`。

- `minas-plugin-recovery-watch.service` 使用 inotify 监听注册表的原子替换，以及插件源码、入口、恢复登记和缓存目录本身的删除或重建。不监听配置、持久状态和缓存目录内部。
- 收到变化后等待 5 秒安静期，连续变化最多合并 30 秒；取得安装器互斥锁，并等待原厂 plugincenter 命令结束后处理。原厂操作仍活跃时继续等待。
- 注册表、图标和入口事件只核对状态；源码事件、开机检查或实际恢复时执行源码摘要及原厂校验。只恢复已登记插件，保留明确停用和页面停止意图。
- 启动时先建立监听再补查；开机前 90 秒等待原厂初始化。存储池未挂载时暂停对应记录，收到内核挂载通知后补查，忽略普通容器挂载变化。
- 源码目录重建、恢复登记更新及事件队列溢出会重建监听。异常失败后最多再重试两次，此后等待新事件或手动修复。
- Mihomo、文件快传和下载中心由独立用户服务托管，异常退出交给 systemd 重启。用户或卸载流程通过 systemctl 正常停止时不会触发自动重启。
- 旧的 30 秒定时器已移除。Cron 每分钟仅查询 systemd 中监听服务的存活状态，失败时才启动服务；单元丢失时再尝试 bootstrap。健康时不启动恢复程序。

## 如何使用

安装或更新任一插件时会登记服务。以下命令在 NAS 的 root 终端执行：

```sh
systemctl status minas-plugin-recovery-watch.service
cat /run/minas-plugin-recovery/watch-status.json
python3 -B /data/.minas-plugin-recovery/recovery.py status
```

手动完整检查和恢复：

```sh
python3 -B /data/.minas-plugin-recovery/recovery.py reconcile
```

启动入口被固件清理但程序仍保留时：

```sh
python3 -B /data/.minas-plugin-recovery/recovery.py bootstrap
```

查看错误：

```sh
journalctl -u minas-plugin-recovery-watch.service --no-pager -n 50
```

## 注意事项

- `/run/minas-plugin-recovery/` 仅保存可重建监听状态；恢复快照和持久启停意图继续位于受保护的 `/data/.minas-plugin-recovery/records/`，插件持久数据不迁入内存目录。
- 监听空闲时等待内核通知，不定时读取插件源码。其他程序仍可能访问硬盘，不能保证整机休眠；实际恢复和手动完整检查会访问存储池。
- 不直接覆盖冲突源码。出现不同版本、源码改动、损坏注册表或缺失持久数据时保留现场，需处理错误后重新检查。
- 明确卸载请使用 `manage.sh uninstall`。应用市场清理后会自动补回已登记插件；原厂策略仍然存在。
- 若固件同时清除所有启动入口，需要手动 bootstrap。尚不能保证适配任意固件升级；本服务不自动执行任务或 SSH 恢复动作。
