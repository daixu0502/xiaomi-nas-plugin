# 插件进阶安装与故障排查

[返回插件与工具导航](../README.md)

## 手动指定 NAS 和账号

使用完整的插件文件夹，保留目录内容完整。NAS 存储池需已挂载，目标用户已在小米客户端完成初始化。

在 WSL / Linux 中，通过 root SSH 连接 NAS。以下命令在仓库根目录或包含 `dockermanager/` 的目录运行：

```bash
bash dockermanager/manage.sh install --ip 192.168.31.100 --users u123456789
```

在 NAS 本机以 root 运行时，省略 `--ip`：

```bash
bash dockermanager/manage.sh install --users u123456789
```

将 `dockermanager` 换成表中对应的插件目录，将示例 IP 和用户 ID 换成实际值。重复运行安装命令即可更新；直接运行 `bash dockermanager/manage.sh` 可进入交互菜单。

新安装包会在安装前自动将旧自定义目录迁入 `/data/minas/`，保留配置、任务、密钥和原启停状态；只安装部分插件也可迁移。已完成迁移时重复安装不会再次搬迁。目录冲突时停止并保留两边数据，迁移失败时自动恢复原布局，详情见[目录说明](../nas-root/docs/data-layout.md)。

| 选项 | 用途 |
| --- | --- |
| `--users u123456789,u987654321` | 选择一个或多个 NAS 用户。 |
| `--all-users` | 选择全部符合条件的用户。 |
| `--list-users` | 只查看用户及安装状态。 |
| `--dry-run` | 检查安装 / 卸载计划，不执行实际操作。 |
| `--help` | 查看当前操作的完整参数。 |

### 打开与使用

安装完成后，在小米客户端应用列表中打开对应插件，按各插件 README 配置。列表或页面没有更新时，完全退出客户端后重新打开。需要运行后台服务的插件，在其页面中检查并启动服务；定时任务还需分别启用调度和具体任务。

### NAS root 工具

在仓库根目录的 WSL / Linux 终端执行以下命令，为已有 root SSH 安装或更新开机恢复：

```bash
bash nas-root/install-ssh-autostart-fix.sh 192.168.31.100
```

首次开启 SSH、Docker 网络修复和其他工具的运行环境与参数见 [NAS root 工具说明](../nas-root/README.md)。

### 卸载

使用对应插件的卸载入口，例如：

```bash
bash dockermanager/manage.sh uninstall --ip 192.168.31.100 --users u123456789
```

NAS 本机省略 `--ip`。核对范围后按提示确认；非交互卸载需添加 `--yes`。

### 查看或手动恢复插件

在 NAS root 终端执行：

```bash
python3 -B /data/minas/plugins/recovery/recovery.py status
python3 -B /data/minas/plugins/recovery/recovery.py reconcile
```

查看事件监听状态：

```bash
systemctl status minas-plugin-recovery-watch.service
cat /run/minas-plugin-recovery/watch-status.json
```

若升级后恢复服务的启动入口丢失，但恢复程序仍在，可手动重新登记：

```bash
python3 -B /data/minas/plugins/recovery/recovery.py bootstrap
```

## 版本维护

每个插件的发布版本只维护在该插件根目录 `INFO` 文件的 `version` 字段，例如 [Mihomo 的 INFO](../mihomo/INFO) 和 [下载中心的 INFO](../downloadcenter/INFO)。修改该字段后，运行构建会自动同步安装器、界面和接口版本，无需逐个文件修改。

每个安装包都包含自身的 `INFO` 与已同步的源码。在 WSL 中进入所需插件的文件夹，运行 `manage.sh` 即可安装，无需仓库根目录文件或其他插件。

```sh
python3 workspace-tools/build_plugin_packages.py
# 也可只同步源码中的版本，或检查是否一致：
python3 workspace-tools/sync_versions.py
python3 workspace-tools/sync_versions.py --check
```

其他文件中的发布版本由同步过程生成，无需逐个修改。上游内核版本、协议版本及历史说明中的版本号保持各自含义。

## 注意事项

- 适用于支持当前安装脚本和依赖的 ARM64 小米 NAS；远程安装需要 root SSH。Mihomo 和下载中心安装时需联网获取并校验内核，不是完整离线包。
- 插件安装按 NAS 用户区分；Docker、系统服务、SSH 和整机重启操作会影响整台设备，管理权限只授予可信用户。
- 以安装脚本最终成功 / 失败汇总为准。有用户安装失败时，处理错误后重试对应用户；更新后检查实际启用状态和服务状态。
- 配置和持久数据通过插件的 `etc/var` 入口访问，实际存放于 `/data/minas/plugins/data/<用户>/<插件>/`；恢复程序和副本位于 `/data/minas/plugins/recovery/`。这些目录包含正在使用的数据，请勿当作临时缓存删除。
- 正式卸载使用 `manage.sh uninstall`，它会取消自动恢复登记。仅在应用市场中删除插件时，恢复服务可能将它补回；保留数据及卸载影响以各插件说明和执行提示为准。
- 恢复需要 `/data` 中的程序、数据及所需系统依赖仍存在。如果固件同时清除所有启动入口，需要手动恢复；不能保证适配任意固件升级。
- 监听器不监控配置、下载进度、普通日志和 PID 内容，等待事件不会反复访问存储池。其他插件、容器和系统服务仍可能访问硬盘，不能据此保证整机休眠。冲突源码不会被静默覆盖；连续失败后需修正问题并手动检查或等待新事件。
- 订阅地址、接口密钥、登录私钥和离线应急材料应妥善保管；文件删除、系统维护及容器升级前备份重要数据。
