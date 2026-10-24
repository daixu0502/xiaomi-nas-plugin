# NAS 脚本简单用法

**按需要选一个脚本，照着对应步骤做即可。**

| 想做什么 | 使用脚本 |
| --- | --- |
| 固件 1.0.9.462，首次开启 SSH | [xiaomi-storage-open-ssh-wsl-1.0.9.462.sh](xiaomi-storage-open-ssh-wsl-1.0.9.462.sh) |
| 固件 1.0.8.425，首次开启 SSH | [xiaomi-storage-open-ssh-wsl-1.0.8.425.sh](xiaomi-storage-open-ssh-wsl-1.0.8.425.sh) |
| SSH 已能登录，让开机后自动恢复 SSH | [install-ssh-autostart-fix.sh](install-ssh-autostart-fix.sh) |
| Docker 容器连不上网 | [fix-docker-bridge.sh](fix-docker-bridge.sh) |
| 升级 Docker 到 29.9.2，或退回旧版 | [upgrade-docker-29.9.2.sh](upgrade-docker-29.9.2.sh) |
| 手动处理旧目录迁移 | [migrate-minas-data.sh](migrate-minas-data.sh)，一般用不到，安装器会自动处理 |

## 先分清在哪里运行

- **SSH 开启、开机恢复安装器**：在电脑的 Ubuntu（WSL）或 Linux 终端运行。
- **Docker 网络修复、Docker 升级、手动迁移**：把脚本传到 NAS 的 `/home/rootx/`，再在 NAS 的 root SSH 终端运行。NAS 上需要 Python 3。

下面的电脑端命令都在存放脚本的文件夹中运行。例如文件在 Windows 的 `D:\NAS工具`，先在 WSL 输入 `cd "/mnt/d/NAS工具"`。示例中的 `192.168.31.100` 请换成自己的 NAS IP。

<details>
<summary>怎么把脚本传到 NAS？</summary>

以网络修复脚本为例，在电脑的 WSL 终端输入：

```bash
scp fix-docker-bridge.sh root@192.168.31.100:/home/rootx/
ssh root@192.168.31.100
```

第一条传文件，第二条登录 NAS。传其他脚本时，换成对应文件名。

</details>

## 开启 SSH

1. 先在小米 Windows 客户端登录自己的 NAS。
2. 下载符合固件版本的开启脚本，同时把 `install-ssh-autostart-fix.sh` 放在同一个文件夹。
3. 在电脑的 **Ubuntu（WSL）终端**运行：

```bash
# 固件 1.0.9.462
bash xiaomi-storage-open-ssh-wsl-1.0.9.462.sh
```

然后按提示输入 NAS IP。成功后会显示登录命令，照着登录即可；询问是否安装开机恢复时，需要就输入 `y`。

固件是 **1.0.8.425** 时，改用下面这条；旧版脚本成功后会自动安装开机恢复：

```bash
bash xiaomi-storage-open-ssh-wsl-1.0.8.425.sh 192.168.31.100
```

提示缺少电脑端命令时，在 WSL 安装依赖后重试：

```bash
sudo apt update
sudo apt install openssh-client curl jq openssl coreutils smbclient
```

首次开启功能尚未完成纯原厂设备的完整验收，以脚本最后的成功或失败提示为准。找不到客户端证书、需要指定私钥时，看[详细说明](docs/advanced-usage.md#1-首次开启-ssh固件-109462)。

若旧脚本一直停在“等待一次性任务执行”，请重新下载当前脚本（2.2.4）。已修复任务权限错误及临时共享无法自动清理的问题，见 [issue #3](https://github.com/daixu0502/xiaomi-nas-plugin/issues/3)。当前版本还会按绝对路径查找 Bash，并实际执行命令确认可用；找不到时会停止并提示。

## 让 SSH 开机自动恢复

已经能用 root SSH 登录 NAS 时，在**电脑的 WSL 终端**运行：

```bash
bash install-ssh-autostart-fix.sh 192.168.31.100
```

看到安装成功即可。这个操作不会重启 NAS；之后可在方便时重启一次，确认仍能登录。

## Docker 容器连不上网

把 `fix-docker-bridge.sh` 放到 NAS 的 `/home/rootx/`，在 **NAS root 终端**先检查：

```sh
sh /home/rootx/fix-docker-bridge.sh --check
```

需要修复时运行下面这条，按提示输入 `APPLY`。它会短暂停止容器并重启 Docker：

```sh
sh /home/rootx/fix-docker-bridge.sh --apply
```

修复后测试容器能否上网。在 **10 分钟内**执行确认命令，否则会自动退回修复前的设置。把最后的“事务编号”换成刚才脚本显示的那串编号：

```sh
sh /home/rootx/fix-docker-bridge.sh --confirm 事务编号
```

需要 IPv6 时，检查和修复命令都加上 `--ipv6`。IPv6 日常维护按网络变化触发，不需要每 30 秒运行一次脚本。更多操作见[网络修复详细说明](docs/docker-bridge.md)。

## 升级 Docker 到 29.9.2

先备份自己挂载给容器使用的文件夹；这些文件不包含在脚本自动备份里。升级和旧目录迁移可能暂时停止容器。

把 `upgrade-docker-29.9.2.sh` 放到 NAS 的 `/home/rootx/`，在 **NAS root 终端**依次执行：

```sh
# 检查能否升级
sh /home/rootx/upgrade-docker-29.9.2.sh --check

# 下载运行程序，并先处理已有的旧目录
sh /home/rootx/upgrade-docker-29.9.2.sh --prepare

# 准备升级前备份的保存位置
mkdir -p /data/minas/backups/docker
chmod 700 /data/minas/backups /data/minas/backups/docker

# 开始升级
sh /home/rootx/upgrade-docker-29.9.2.sh --install \
  --backup-dir /data/minas/backups/docker \
  --bind-backups-confirmed
```

**每一步成功后再做下一步。** 按提示输入 `INSTALL`，等待完成。备份需要足够空间；成功后检查原来的容器能否使用，保留升级备份。

### 需要退回旧版时

先查看升级记录，再把命令中的“事务编号”换成要回退的那一条编号：

```sh
sh /home/rootx/upgrade-docker-29.9.2.sh --status
sh /home/rootx/upgrade-docker-29.9.2.sh --rollback-check 事务编号
sh /home/rootx/upgrade-docker-29.9.2.sh --rollback 事务编号
```

第二条是检查，检查通过再执行第三条，按提示输入 `ROLLBACK`。

默认保留数据卷里的当前内容，但旧镜像、容器配置和容器自身的文件会恢复到升级前。需要连数据卷也恢复到升级前时，先看[完整回退说明](docs/docker-upgrade-29.9.2.md#回退)，再决定是否执行。

## 旧目录需要自己搬吗？

通常不需要。新版安装器只把自己需要的文件接到 `/data/minas/`，保留配置和任务，其他插件可继续使用旧目录。最后一个使用者迁走后，再清理已无用途的公共文件。首次开启 SSH 的两个脚本不搬迁已有组件。

`migrate-minas-data.sh` 是手动维护用的，需配套迁移资料才能执行；遇到迁移失败或目录冲突时，保留原数据和报错，按[详细说明](docs/advanced-usage.md#6-自定义目录迁移与失败恢复)处理。

## 还想了解更多

- [全部参数、检查命令和故障恢复](docs/advanced-usage.md)
- [Docker 升级备份与回退范围](docs/docker-upgrade-29.9.2.md)
- [配置、任务和备份保存在哪里](docs/data-layout.md)

保留正在使用的配置、密钥和恢复资料，尤其不要把 Docker 升级备份和回退记录当作缓存清理。
