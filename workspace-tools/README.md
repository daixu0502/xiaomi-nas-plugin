# 统一源码仓库构建工具

从 `xiaomi-nas-plugin/` 根目录执行。构建不再依赖父工作区或七个旧仓库。

```sh
python workspace-tools/prepare-spec-release.py
python workspace-tools/sync-plugin-runtime.py
python workspace-tools/sync-privileged-component.py
python workspace-tools/build_plugin_packages.py
# 单个插件：
python workspace-tools/build_plugin_packages.py mihomo
```

各插件版本唯一维护在各自根目录 `INFO` 的 `version` 字段；构建自动运行 `sync_versions.py`，同步安装器、界面和接口版本。公共组件版本取自对应维护源的 VERSION 常量，同步副本及摘要。不存在仓库级版本配置；安装包包含自身 INFO 与生成结果，可独立复制安装。单独运行 `python3 workspace-tools/sync_versions.py --check` 可只检查生成文件。目录名必须与插件 ID 一致。`prepare-spec-release.py` 用于标准布局元数据和图标准备，普通版本更新无需运行它。普通工具与共有代码只同步自本仓库维护源，私有模块保持在插件所属位置。

构建输出根 `dist/`，并同步 `package-manifest.json`、本目录的同名清单和 `dist/SHA256SUMS`。构建检查元数据、恢复代码同步、公共组件摘要、缓存工具与包内容，不打包测试、依赖、缓存和运行数据。

SSH 开机修复脚本版本独立维护在 `sshrecovery/installer/common/modules/sshrecovery/restore-ssh.sh` 的 `SSH_RECOVERY_VERSION`，不随插件 INFO 版本变化。运行 `python -X utf8 nas-root/tools/ssh/build.py --boot-installer-only` 可同步独立 worker 并生成开机安装器，其标题、帮助与内嵌脚本使用同一个版本源。

Linux/WSL 本地检查：

```sh
python3 -B -m unittest discover -s workspace-tools/tests -p test_install_packages.py
python3 -B -m unittest discover -s workspace-tools/tests -p test_cgi_descriptor.py
sh workspace-tools/run-source-integrity-regressions.sh
```

恢复测试与权限组件测试要求隔离的 root 测试环境；仅操作测试创建的临时目录，不连接 NAS：

```sh
python3 -B -m unittest discover -s workspace-tools/tests -p test_plugin_recovery.py
python3 -B -m unittest discover -s workspace-tools/tests -p test_privileged_component.py
```

`audit-common-component-packages.py` 检查七包权限组件边界；`audit-plugin-integrity.py` 是 NAS 上的只读源码审计。`source_policy.py` 检查未来仓库索引是否混入测试。测试和固件参考测试夹具保持本地，不上传 GitHub。

历史定向部署、旧 GitHub 多仓库发布脚本、NAS/WSL 实例信息和旧仓库元数据留在父工作区，不属于新仓库的部署入口。恢复服务和真机验收边界见 [根说明](../README.md)。

## 在线更新维护

八插件的代理设置、签名发布、中断恢复和维护命令见 [在线更新组件](plugin-updater/README.md)。构建前运行 `python -B workspace-tools/sync-plugin-updater.py` 和 `python -B workspace-tools/sync-ui-controls.py`，再执行标准打包。Release 附件使用根 `dist/` 中的标准包，构建产物不加入 Git 索引。
