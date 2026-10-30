# 构建与维护工具

所有命令从项目根目录执行，使用本 Windows 工作区对应的 WSL/Linux 路径。

## 分类

| 目录 | 用途 |
| --- | --- |
| `build/` | 完整离线套件、单插件包和签名发布清单；GitHub 发布工具 |
| `sync/` | 从 INFO、公共运行库、界面模板同步生成文件 |
| `checks/` | 签名套件审计、源码检查、本地回归入口、Git 测试排除规则 |
| `icons/` | 插件图标生成源码 |
| `nas/` | SSH、Docker 网络、Docker 升级、清理工具源码与构建器 |
| `tests/` | 仅本地保留的回归测试及夹具，不加入 Git 或安装包 |
| `private/` | 本地签名、缓存、备份、历史资料、工作树，不加入 Git 或安装包 |

公共安装、权限、恢复、更新和共享 UI 的维护源在 [plugins/common/](../plugins/common/README.md)。成品独立脚本在 [scripts/](../scripts/README.md)。

## 同步源码

插件版本只在 `plugins/<插件>/INFO` 中修改；同步和重复构建不会自动递增版本。组件有实际代码变更时，分别修改对应 VERSION 常量。不要替换内核版本、协议版本或历史版本记录。

```sh
python3 -B workspace-tools/sync/sync-plugin-runtime.py
python3 -B workspace-tools/sync/sync_data_layout.py
python3 -B workspace-tools/sync/sync-plugin-updater.py
python3 -B workspace-tools/sync/sync-ui-controls.py
python3 -B workspace-tools/sync/sync-privileged-component.py
python3 -B workspace-tools/sync/sync_versions.py --check
```

`prepare-spec-release.py` 用于初始化标准元数据与图标，不是普通版本更新的必需步骤。`sync-plugin-updater.py --check` 和 `sync-ui-controls.py --check` 检查共享副本是否一致。

## 构建与检查

```sh
python3 -B workspace-tools/build/build_suite.py --release v2026.10.30.3
python3 -B workspace-tools/checks/audit-suite.py
sh workspace-tools/checks/run-source-integrity-regressions.sh
```

构建默认读取 `workspace-tools/private/signing/private.pem`。已有公钥必须能验证该私钥的签名；不会自动创建或更换密钥。固定内核缓存位于 `workspace-tools/private/cache/offline-cores/`，缺失时按源码固定地址下载并校验摘要；缓存齐全后可在隔离网络中构建。构建暂存也在该私有缓存目录。

产物在根 `dist/`，插件清单只生成根 `package-manifest.json`；套件和组件信息分别在 `dist/suite-artifacts.json`、`suite-manifest.json`。发布时只保留本次清单列出的最新安装包，禁止把旧包混进附件。

本地回归在隔离的 root Linux/WSL 环境执行，不连接 NAS；测试只操作临时目录。签名套件审计验证八插件、组件依赖、文件归属，以及在线更新与离线安装内容一致。源码审计工具 `checks/audit-plugin-integrity.py` 供 NAS root 只读使用。真机验收范围见 [记录](../plugins/common/manager/VALIDATION.md)。

## 独立脚本构建

```sh
python3 -B workspace-tools/nas/ssh/build.py
python3 -B workspace-tools/nas/docker-bridge/build.py
python3 -B workspace-tools/nas/docker-upgrade/build.py
python3 -B workspace-tools/nas/data-layout/build.py
```

输出覆盖 `scripts/` 中对应成品；固件 1.0.8.425 的旧开启脚本仍保留独立实现。SSH worker 版本源是 SSH 插件私有模块里的 `SSH_RECOVERY_VERSION`，`--boot-installer-only` 只同步 worker 并构建开机修复安装器。

## 发布

先同步源码、完成本地检查，在独立工作树提交并推送本次源码，然后指定确切提交和未使用的标签：

```sh
python3 -B workspace-tools/build/publish-plugin-release.py \
  --tag v2026.10.30.3 --target 完整源码提交SHA \
  --signing-key workspace-tools/private/signing/private.pem --publish
```

不加 `--publish` 只构建和签名。发布前逐文件核对附件和提交的 Git blob；先上传草稿，确认附件大小和摘要后公开。凭据通过既有 Git 授权或环境变量读取，不写入源码。旧更新器不支持新包格式，须先通过根安装器切换；已切换的 2.0.0 更新器可按依赖下载本次更新。

## 本地资料

- `private/signing/`：已有发布签名材料。
- `private/cache/`：通过固定摘要核验的离线内核。
- `private/backups/manager-acceptance-20261030/`：真机切换前完整备份及验收证据，包含受保护材料。
- `private/backups/unified-installer-baseline-20261030.tar.gz`：旧更新器兼容测试基线。
- `private/history/`：压缩归档的历史维护记录，不是部署入口。
- `private/worktrees/`：保留未提交内容的旧工作树。

不把密钥、设备配置、完整备份、测试、缓存或工作树打入安装包，不重新创建根 local/。
