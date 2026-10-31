# 小米 NAS 插件与工具

八个插件共用根目录的 `manage.sh` 安装、更新和卸载；另提供独立的 SSH、Docker 维护脚本。

## 选择插件

| 插件 | 可以做什么 | 插件 ID |
| --- | --- | --- |
| [Docker 管理](plugins/dockermanager/README.md) | 管理容器、镜像、网络和日志 | `dockermanager` |
| [Mihomo](plugins/mihomo/README.md) | 导入订阅、切换节点、管理代理 | `mihomo` |
| [定时任务](plugins/taskcenter/README.md) | 定时开关容器、执行脚本和开机任务 | `taskcenter` |
| [控制中心](plugins/nascenter/README.md) | 查看 CPU、内存、网络和硬盘状态 | `nascenter` |
| [SSH 恢复](plugins/sshrecovery/README.md) | 恢复 SSH、配置开机恢复、准备应急材料 | `sshrecovery` |
| [文件快传](plugins/quickshare/README.md) | 分享下载链接、接收上传 | `quickshare` |
| [下载中心](plugins/downloadcenter/README.md) | 下载直链、磁链和种子 | `downloadcenter` |
| [影视编辑](plugins/mediaeditor/README.md) | 修正影视资料、系列和季集信息 | `mediaeditor` |

## 安装前准备

1. 在小米客户端登录 NAS，完成账号和存储池初始化。
2. 确认电脑可以用 **root SSH** 登录 NAS。尚未开启 SSH 时，先看[独立脚本说明](scripts/README.md)，选择匹配固件的开启脚本。
3. 准备完整离线套件 `xiaomi-nas-suite-发布标识-arm64.tar.gz`。本地构建产物在 `dist/`；已发布版本从[项目发布页](https://github.com/daixu0502/xiaomi-nas-plugin/releases)下载。首次安装请选择完整套件，单插件包和组件包用于在线更新。
4. Windows 用户在 **Ubuntu / WSL 终端**运行以下命令，Linux 用户使用终端。电脑需要 Python 3.9+、OpenSSH 客户端、tar 和 OpenSSL；NAS 需要 Python 3.9+、systemd 及原厂插件环境。安装器不会自动升级系统依赖。

完整套件包含八插件、公共组件和校验过的 ARM64 默认内核，安装及默认首次启动可离线完成。导入网络订阅、下载文件等功能仍需要相应网络。

## 安装：推荐使用中文菜单

假设下载文件位于 Windows 的 `D:\NAS工具`，在 WSL 输入：

```sh
cd /mnt/d/NAS工具
mkdir -p nas-suite
# 将下面的文件名换成实际下载的完整套件文件名
tar -xzf xiaomi-nas-suite-发布标识-arm64.tar.gz -C nas-suite
cd nas-suite
sh manage.sh
```

选择“安装插件”，输入 NAS IP，按提示选择账号和插件。多选时输入逗号分隔的序号，输入 `all` 表示全选；连接 NAS 时可能需要输入 root 密码。等待终端报告安装成功，再到小米客户端打开插件。

全新安装启用插件入口，并启动所选的 Mihomo、下载中心、文件快传和空任务调度器。SSH 登录及开机恢复需在 SSH 恢复页面另行配置。

### 直接输入命令

将 `192.168.31.100` 换成自己的 NAS IP。先查看状态，找到实际账号 ID（例如 `u123456`），它不是小米账号昵称。

```sh
sh manage.sh status --ip 192.168.31.100

# 只检查能否安装，不修改设备配置
sh manage.sh install --ip 192.168.31.100 --users u123456 --plugins mihomo,quickshare --dry-run

# 为该账号安装这两个插件
sh manage.sh install --ip 192.168.31.100 --users u123456 --plugins mihomo,quickshare

# 为该账号安装全部八插件
sh manage.sh install --ip 192.168.31.100 --users u123456 --all-plugins
```

多个账号写成 `--users u123456,u654321`。新安装遇到多个存储池时，加上实际池名，例如 `--pool pool1`，不会默认强制使用 pool0。

## 已装旧版：保留数据切换

在新完整套件目录中运行：

```sh
sh manage.sh reinstall --ip 192.168.31.100 --dry-run
sh manage.sh reinstall --ip 192.168.31.100
```

默认处理**所有账号已有的八类插件**，保留配置、任务、SSH 材料、已更新内核和原启停状态，不额外安装原来没有的插件。第二条会暂停相关服务并要求确认，请在没有下载、传输或关键任务的维护时间运行。切换前另行保存重要配置和应急材料。

`reinstall` 是保留数据重装。**不要先卸载再安装来代替它，普通卸载会删除插件数据。**

## 更新插件

打开插件，点击电脑端左下角或手机端底部的版本号，选择“检查更新”，再确认“立即更新”。关闭页面不会取消已提交的更新；完成后关闭插件窗口再打开。更新保留配置与启停状态。

在线更新包不携带 Mihomo、aria2 或 qBittorrent 内核，也不下载或替换现有内核；只有完整离线套件携带默认内核。现有内核缺失时，使用完整套件保留数据重装。

公共安装引擎、恢复程序、更新器及权限共有代码会随插件按需在线升级，无需分别安装。**从更新器 2.0.0 首次升级时，先在线更新 Docker 管理或文件快传等非内核插件一次，再更新 Mihomo 和下载中心**；这一步会将共有引擎和更新器升级到支持精简包的版本。旧于 2.0.0 或只安装了内核插件的设备，可用最新完整套件执行下面的 `update`，同样保留数据与启停状态。

“更新代理”按 NAS 账号在八插件间共用，仅用于检查和下载安装包。需要时填写 HTTP/HTTPS 代理和认证信息；启用代理后连接失败不会自动改为直连。旧更新器必须先按上一节切换。

也可解压新完整套件，离线更新已安装插件：

```sh
sh manage.sh update --ip 192.168.31.100 --users u123456 --all-plugins
```

## 卸载：删除插件及其数据

**普通卸载彻底删除所选账号的插件配置、任务、断点、专属恢复材料和快照，成功后不保留回退备份。需要的内容请先自行导出。** 用户下载和媒体文件、Docker 容器与卷、独立 SSH 及 Docker 维护工具不属于插件数据。

运行 `sh manage.sh`，选择“彻底卸载插件及数据”，再选择账号和插件；也可执行：

```sh
# 先看将卸载哪些内容
sh manage.sh uninstall --ip 192.168.31.100 --users u123456 --plugins quickshare --dry-run

# 卸载文件快传，按提示输入 YES 确认
sh manage.sh uninstall --ip 192.168.31.100 --users u123456 --plugins quickshare

# 卸载所有账号安装的全部自定义插件，仍需确认
sh manage.sh uninstall --ip 192.168.31.100 --all-users --all-plugins
```

安装器撤销所选账号授权，删除插件服务和数据，按剩余引用清理共用组件；安全锁等受保护控制文件依契约保留。明确卸载的插件不会自动恢复。仅在原厂应用市场删除入口可能触发自动补回，完整卸载请使用 `manage.sh`。

INFO、源码、控制脚本或部分数据缺失时，仍可直接卸载。异常链接只移除链接，外部目标会列明保留；存储池不明确时清理各已挂载池中该账号、该插件的规范目录。已知但未挂载的池保留精确待清理记录，挂载后继续清理；记录完成前不允许重新安装，以免误删新数据。

## NAS 本机运行、状态和修复

把完整套件传到 NAS 上有足够空间的目录并解压，在 **NAS root SSH 终端**进入解压目录，执行同样的命令，省略 `--ip`：

```sh
sh manage.sh
sh manage.sh status
sh manage.sh repair --users u123456 --plugins quickshare
```

`repair` 用于已切换到统一安装器的插件。存储池未挂载、持久数据缺失或源码冲突时会停止并报告路径；请先解决原因，不要创建空数据目录顶替。

源码仓库的 `manage.sh` 会自动读取 `dist/suite-artifacts.json` 指向的最新完整套件，直接运行菜单即可。也可加 `--bundle /绝对路径/套件.tar.gz` 或指定解压后的目录。`--dry-run` 做预检；自动化加 `--yes` 可跳过卸载和重装的人工确认。所有参数见 `sh manage.sh --help`。

安装中断后先运行 `sh manage.sh recover --ip NAS地址`，再重试原操作；NAS 本机省略 `--ip`。恢复失败会保留事务材料并显示路径，不要手动删除。若提示 `/etc/config/plugin` 的挂载句柄失效，先恢复事务并重启 NAS，再使用最新套件；不要直接编辑 `/data/etc/upper` 下的文件。

## 项目结构与文档

```text
xiaomi-nas-plugin/
├── dist/                 最新安装包与校验清单（本地生成）
├── docs/                 使用、开发和目录规范
├── plugins/
│   ├── common/           公共组件与共享源码
│   └── 八个插件目录/
├── scripts/              NAS 独立成品脚本
├── workspace-tools/      构建、同步、检查及独立脚本源码
├── manage.sh             唯一插件管理入口
├── README.md
└── AGENTS.md
```

- [文档索引](docs/README.md)：数据布局、开发规范和验收记录。
- [独立脚本](scripts/README.md)：首次开启 SSH、开机恢复、Docker 网络及升级。
- [公共组件](plugins/common/README.md)：组件职责和安装边界。
- [构建与检查](workspace-tools/README.md)：维护源码、离线打包和本地验证。
