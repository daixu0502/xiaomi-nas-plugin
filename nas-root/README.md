# 小米 NAS root 工具

## 功能说明

独立使用的 NAS 管理工具，与七插件统一维护在 [xiaomi-nas-plugin 仓库](https://github.com/daixu0502/xiaomi-nas-plugin)。

| 工具 | 功能 |
| --- | --- |
| [SSH 开启](xiaomi-storage-open-ssh-wsl.sh) | 通过已登录小米客户端的证书和 WebDAV 流程尝试首次开启 SSH，验证后安装开机恢复。 |
| [SSH 开机恢复安装器](install-ssh-autostart-fix.sh) | 通过已有 root SSH 安装或更新与 SSH 恢复插件同源的开机恢复。 |
| [Docker bridge 修复](fix-docker-bridge.sh) | 检查网络规则，恢复 Docker 原生 IPv4 规则管理，提供限时确认和自动回退。 |
| [BitComet 部署](tools/bitcomet-docker/README.md) | 校验并导入 ARM64 镜像，部署容器及更新 WebUI 凭据。 |
| [bridge-net 辅助服务](tools/bridge-net-network/README.md) | 为已有 bridge-net 网络补充 IPv4 出站规则，供仍采用旧方案的设备使用。 |

## 如何使用

以下 SSH 命令在仓库根目录的 WSL / Linux 终端执行，将示例 IP 换为实际 NAS 地址：

```bash
# 尝试首次开启 SSH 并安装开机恢复
bash nas-root/xiaomi-storage-open-ssh-wsl.sh 192.168.31.100
# 已有 root SSH 时，只安装或更新开机恢复
bash nas-root/install-ssh-autostart-fix.sh 192.168.31.100
```

首次开启前，需登录小米 Windows 客户端并准备当前账户的证书。密钥、证书目录选项和兼容范围见 [SSH 说明](tools/ssh/README.md)。

将 fix-docker-bridge.sh 复制到 NAS，在 NAS 的真实 root 终端执行只读检查：

```sh
sh /path/to/fix-docker-bridge.sh --check
```

需要修复时按 [Docker bridge 说明](tools/docker-bridge/README.md) 执行 --apply，验证业务连通性后在期限内确认。其他工具按表中链接操作。

从仓库根目录重新生成单文件入口：

```bash
python3 nas-root/tools/ssh/build.py
python3 nas-root/tools/docker-bridge/build.py
```

## 注意事项

- SSH 开启入口只适用于自己的设备和已登录的管理员客户端。不同固件可能限制首次开启；上传或触发成功不等于已获得 root 命令执行能力。
- 当前首次开启流程缺少独立执行回执。设备原本已有可用 SSH 时，即使触发失败，后续验证仍可能通过；成功提示不能证明是本次首次开启成功。
- SSH worker 位于 /data/ssh-autostart-fix/restore-ssh.sh；公共权限组件的 SSH 状态和操作锁位于 /data/.minas-privileged/modules/sshrecovery/var/。安装开机恢复不主动重启设备，也不覆盖已有登录授权。
- Docker 网络修复的 --apply 可能短暂停止容器并重启 Docker，未及时确认会回退。请在维护窗口执行，按说明验证端口、DNS 和应用连通性。
- 原生 Docker 规则方案与 bridge-net 辅助服务需按设备现状选择；原生修复工具会处理旧辅助规则，操作前阅读对应说明。
- 测试、依赖、设备专属容器重建记录、凭据和离线应急材料仅保存在本地；安装包存放在 dist/。不要公开设备事务记录或恢复材料。
- [原厂插件管理脚本](docs/firmware-reference/README.md) 仅供参考，不能覆盖系统文件。隔离测试不能代替真实首次开启、固件升级和重启验证。
