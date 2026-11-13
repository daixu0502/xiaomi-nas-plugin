# 标准源码与安装包目录

插件 ID：`sshrecovery`。插件根目录直接包含 `INFO`、`src/files/`、`src/ui/`、`scripts/control` 和 `manage.sh`；默认配置按需放 `etc/`。安装辅助与提权实现放 `installer/`，不放入参与原厂校验的 `src/`。

包内顶层目录同样为 `sshrecovery/`，直接运行 `bash plugins/sshrecovery/manage.sh install`，远程安装加 `--ip` 和 `--users`。安装器保持这一相对布局传输，按原有固件映射安装到 `/home/<用户>/plugin/sshrecovery/`；www 链接统一指向该根目录的 `src/ui`。不迁移或清空现有配置、任务、断点、内核、SSH 材料或禁用状态。

`var/`、`tmp/` 为 NAS 运行时目录，不在源码包预置运行数据。历史 `payload/` 目录已移除；图标生成代码仅在公共构建工具中保留，安装只使用 `src/ui/icon.png`。
