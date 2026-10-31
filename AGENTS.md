# 八插件统一仓库协作约定

## 源码与协作

- 本目录是八插件及独立 NAS 工具的唯一开发、构建和 GitHub 提交来源；不用旧 WSL 副本覆盖它，不建立插件嵌套仓库。
- 保留已有未提交改动。提交时在本 Windows 工作区创建独立工作树并核对远端；提交、推送、发布和部署各自遵守当前授权，不因本地整理自动执行。
- 统一远端为 `https://github.com/daixu0502/xiaomi-nas-plugin`，不混入父工作区其他项目。

## 目录归属

| 目录 | 内容与维护规则 |
| --- | --- |
| `plugins/<插件ID>/` | 八插件：根 INFO、src/files、src/ui、scripts/control、按需 etc；差异描述在 installer/ |
| `plugins/common/manager/` | 统一安装引擎，唯一 Shell 入口是根 manage.sh |
| `plugins/common/privileged/` | 权限共有代码；六插件私有模块只维护在各自 installer/common/modules/<插件>/ |
| `plugins/common/recovery/`、`updater/` | 恢复观察程序、更新后台，不在插件中维护副本 |
| `plugins/common/runtime/`、`data-layout/`、`ui/` | 共享维护源，通过同步工具生成所属副本 |
| `scripts/` | 独立成品脚本；生成文件修改维护源后重新构建 |
| `workspace-tools/nas/` | 独立工具源码、模板和构建器 |
| `workspace-tools/build/`、`sync/`、`checks/`、`icons/` | 打包发布、同步、检查、图标准备 |
| `docs/` | 通用文档；独立脚本文档在 docs/scripts/ |
| `dist/` | 最新套件、插件包、组件包与校验清单；不保留旧版本包，不创建 releases/ |
| `workspace-tools/private/` | 本地签名、缓存、备份、历史资料和工作树；全部忽略，不入 Git 或安装包 |

根 `package-manifest.json` 是构建生成的唯一插件包清单；`dist/suite-artifacts.json` 和 `suite-manifest.json` 记录套件与组件。不要恢复旧目录或重复清单。

## 版本与构建

- 插件版本只维护在 `plugins/<插件ID>/INFO` 的 version 字段，组件版本维护在各自 VERSION 常量。不手工修改生成的页面缓存键和接口版本，不把内核版本当插件版本替换。
- `python3 -B workspace-tools/sync/sync_versions.py --check` 只检查；去掉 --check 执行同步。其他命令见 [workspace-tools/README.md](workspace-tools/README.md)。
- 共享源码修改后同步副本，再构建、检查签名、摘要和文件归属。仓库位置调整不得改变 NAS 安装路径或包内路径。
- SSH worker 版本源为 `plugins/sshrecovery/installer/common/modules/sshrecovery/restore-ssh.sh` 的 SSH_RECOVERY_VERSION，经 workspace-tools/nas/ssh/build.py 同步构建。
- 测试代码只保留本地。tests/、__tests__/ 和独立测试文件不得加入 Git 索引或安装包；已跟踪测试只取消跟踪，不删除本地文件。Markdown 验收文档可保留，不用 git add -f 绕过规则。
- 密钥、凭据、缓存、下载内核、设备配置及应急导出不入源码或安装包。签名私钥在 workspace-tools/private/signing/；固定摘要内核缓存在 workspace-tools/private/cache/offline-cores/。

## 安装与数据

- 根 manage.sh 是唯一插件安装入口；不恢复各插件 manage.sh，不重建 payload/ 包装层。
- 更新、旧版切换和 reinstall 保留配置、任务、禁用入口、服务停止意图、SSH 材料及已更新内核。普通 uninstall 彻底删除所选插件数据，提交后不留回退备份。独立 SSH、Docker 工具和用户下载、媒体、容器、卷遵守各自归属。
- 源码不写运行数据。用户数据在 `/data/minas/plugins/data/<用户>/<插件>/`，通过 etc/var 链接访问；源码路径使用实际存储池，不强制 pool0。
- 权限数据归 `/data/minas/privileged/`；管理、恢复、更新分别归 `/data/minas/plugins/{manager,recovery,updater}/`。保护 SSH 模块材料和影视 protected/ 数据，不跨组件覆盖。
- 普通缓存、日志、PID 使用官方 tmp → plugintmp/<插件>/；root 先降权访问用户缓存。配置、任务、断点、内核、授权、锁及恢复材料不得放入可清空缓存。
- 卸载不依赖插件文件完整或其控制脚本可执行。对异常链接只移除链接，不追删外部目标；按账号、插件枚举当前规范布局的已挂载池。已知未挂载池的清理持久登记，完成前阻止重新安装；不把挂载点空目录当成已清理。
- `/data/minas` 保持 root 所有的真实目录、0755；敏感目录 0700。保留固定锁 inode，最终原子暂存与目标同目录。安装器不探测、搬移或清理旧布局。
- 真机源码更新必须同步 INFO 摘要及注册信息，原厂校验和业务检查通过后才报告部署成功；本地构建和测试不能替代真机验收。

## 文档

- 根 README.md 集中提供通俗的安装、更新、保留数据重装和彻底卸载教程。
- 各插件 README.md 只说明自身功能、使用、权限、数据和注意事项；通用安装教程链接回根 README，不复制八份。
- scripts/README.md 说明独立脚本用法，workspace-tools/README.md 说明维护流程。目录变动同步修改路径、文档链接、构建和忽略规则。
