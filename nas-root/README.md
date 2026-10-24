# 小米 NAS root 工具

## 功能说明

独立使用的 NAS 管理工具，与七插件统一维护在 [xiaomi-nas-plugin 仓库](https://github.com/daixu0502/xiaomi-nas-plugin)。

| 工具 | 功能 |
| --- | --- |
| [SSH 开启：固件 1.0.9.462](xiaomi-storage-open-ssh-wsl-1.0.9.462.sh) | 用已登录客户端证书，经新固件 UCI/SMB 入口和一次性 Cron 尝试开启 SSH，验证后询问是否安装开机恢复。 |
| [SSH 开启：固件 1.0.8.425](xiaomi-storage-open-ssh-wsl-1.0.8.425.sh) | 保留的旧固件入口，不适用于 1.0.9.462；成功后自动安装开机恢复。 |
| [SSH 开机恢复安装器](install-ssh-autostart-fix.sh) | 通过已有 root SSH 安装或更新与 SSH 恢复插件同源的开机恢复。 |
| [Docker bridge 修复](fix-docker-bridge.sh) | 恢复原生 IPv4 规则；可选启用默认 bridge IPv6、NAT66 与开机恢复，提供限时确认和自动回退。 |
| [Docker 29.9.2 安装与回退](upgrade-docker-29.9.2.sh) | 独立运行时、一致备份；默认回退保留当前本地卷，支持显式完整数据恢复。 |
| [自定义目录迁移](migrate-minas-data.sh) | 使用校验过的迁移包将旧目录归入 `/data/minas/`，保留持久事务及失败恢复。 |

## 如何使用

自定义组件的持久布局见[目录说明](docs/data-layout.md)。新插件安装包和独立 SSH、Docker 工具检测到旧目录时，会自动迁移实际存在的组件，再继续安装；无需另行准备迁移包。涉及旧自定义 Docker 运行目录时会短暂停止 Docker 和容器，原厂 Docker 数据根保持原位。独立迁移命令仍可使用匹配的迁移包执行维护和失败恢复。

以下 SSH 命令在仓库根目录的 WSL / Linux 终端执行，将示例 IP 换为实际 NAS 地址：

```bash
# 尝试首次开启 SSH，成功后按提示选择是否安装开机恢复
bash nas-root/xiaomi-storage-open-ssh-wsl-1.0.9.462.sh 192.168.31.100
# 已有 root SSH 时，只安装或更新开机恢复
bash nas-root/install-ssh-autostart-fix.sh 192.168.31.100
```

首次开启前，需登录小米 Windows 客户端并准备当前账户的证书。密钥、证书目录选项和兼容范围见 [SSH 说明](tools/ssh/README.md)。

将 fix-docker-bridge.sh 复制到 NAS，在 NAS 的真实 root 终端执行只读检查：

```sh
sh /path/to/fix-docker-bridge.sh --check
```

需要修复时按 [Docker bridge 说明](docs/docker-bridge.md) 执行 --apply，验证业务连通性后在期限内确认。IPv6 维护采用纯事件驱动，无周期检查；已有旧版 IPv6 维护可用 `--install-ipv6-events` 升级，无需重启 Docker。

Docker 跨版本更新先阅读 [Docker 29.9.2 安装与回退说明](docs/docker-upgrade-29.9.2.md)。将单文件脚本复制到 NAS 后，默认只读检查：

```sh
sh /path/to/upgrade-docker-29.9.2.sh --check
```

正式安装需要显式 `--install`、一致备份目录及外部挂载备份确认；会停止容器，失败尝试回退。`--rollback-check ID` 只读检查已有备份和回退条件；`--rollback ID` 默认保留当前本地卷数据，镜像、容器层和元数据恢复升级前状态。只有显式添加 `--restore-all-data` 才将卷数据一起倒回。

从仓库根目录重新生成单文件入口：

```bash
python3 nas-root/tools/ssh/build.py
python3 nas-root/tools/docker-bridge/build.py
python3 nas-root/tools/docker-upgrade/build.py
```

## 注意事项

- SSH 开启入口只适用于自己的设备和已登录的管理员客户端。不同固件可能限制首次开启；上传或触发成功不等于已获得 root 命令执行能力。
- 首次开启要求本次随机执行回执和真实 UID 0 命令，不能仅凭原有 SSH 或端口开放报告成功。改写已经隔离测试，尚未在纯原厂设备完成首次开启及重启验收；[新固件入口分析](tools/ssh/NEW-FIRMWARE.md) 记录了验证边界。
- SSH worker 位于 /data/minas/ssh/restore-ssh.sh；公共权限组件的 SSH 状态和操作锁位于 /data/minas/privileged/modules/sshrecovery/var/。安装开机恢复不主动重启设备，也不覆盖已有登录授权。
- Docker 网络修复的 --apply 可能短暂停止容器并重启 Docker，未及时确认会回退。请在维护窗口执行，按说明验证端口、DNS 和应用连通性。
- 原生 Docker 修复工具会处理旧 bridge-net 辅助规则，操作前阅读对应说明。
- 测试、依赖、设备专属容器重建记录、凭据和离线应急材料仅保存在本地；安装包存放在 dist/。不要公开设备事务记录或恢复材料。
- [原厂插件管理脚本](docs/firmware-reference/README.md) 仅供参考，不能覆盖系统文件。隔离测试不能代替真实首次开启、固件升级和重启验证。
