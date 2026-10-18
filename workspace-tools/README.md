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

`prepare-spec-release.py` 是本次规范发布的幂等准备工具；版本唯一维护在 `plugins.json`，目录名必须与插件 ID 一致。普通工具与共有代码只同步自本仓库维护源，私有模块保持在插件所属位置。

构建输出根 `dist/`，并同步 `package-manifest.json`、本目录的同名清单和 `dist/SHA256SUMS`。构建检查元数据、恢复代码同步、公共组件摘要、缓存工具与包内容，不打包测试、依赖、缓存和运行数据。

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
