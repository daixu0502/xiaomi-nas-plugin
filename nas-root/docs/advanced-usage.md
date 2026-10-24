# NAS root 脚本详细用法

[返回简单使用步骤](../README.md)

本目录提供首次开启 SSH、安装 SSH 开机恢复、修复 Docker 网络、升级与回退 Docker，以及迁移自定义数据目录的脚本，与七插件统一维护在 [xiaomi-nas-plugin 仓库](https://github.com/daixu0502/xiaomi-nas-plugin)。

## 选择脚本

| 你的情况 | 使用脚本 | 执行位置 |
| --- | --- | --- |
| 固件 **1.0.9.462**，首次开启 root SSH | [xiaomi-storage-open-ssh-wsl-1.0.9.462.sh](../xiaomi-storage-open-ssh-wsl-1.0.9.462.sh) | 电脑的 WSL / Linux |
| 固件 **1.0.8.425**，首次开启 root SSH | [xiaomi-storage-open-ssh-wsl-1.0.8.425.sh](../xiaomi-storage-open-ssh-wsl-1.0.8.425.sh) | 电脑的 WSL / Linux |
| 已有 root SSH，需要开机自动恢复 SSH | [install-ssh-autostart-fix.sh](../install-ssh-autostart-fix.sh) | 电脑的 WSL / Linux |
| 容器无法出网，或需要默认 bridge 的 IPv6 出口 | [fix-docker-bridge.sh](../fix-docker-bridge.sh) | NAS 的 root SSH 终端 |
| 升级 Docker 到 29.9.2，或回退已有升级事务 | [upgrade-docker-29.9.2.sh](../upgrade-docker-29.9.2.sh) | NAS 的 root SSH 终端 |
| 手动迁移旧目录，或恢复未完成的目录迁移 | [migrate-minas-data.sh](../migrate-minas-data.sh) | NAS 的 root SSH 终端 |

新版插件安装包和独立 SSH、Docker 工具已支持自动迁移旧目录。常规安装、更新直接使用相应入口即可，通常不需要手动运行迁移脚本。

## 执行环境与文件准备

### 电脑端

SSH 开启和开机恢复安装器使用 Bash，在 WSL / Linux 执行。电脑端示例均从**仓库根目录**执行；如果只下载了脚本，改为文件实际所在路径。

首次使用可在 WSL Ubuntu 安装依赖：

```bash
sudo apt update
sudo apt install openssh-client curl jq openssl coreutils smbclient
```

旧固件脚本的自动寻址功能另外需要 `dnsutils`、`iproute2`；直接提供 NAS IP 则无需自动寻址。

示例 IP `192.168.31.100` 请替换为自己的 NAS。SSH 私钥默认为 `~/.ssh/id_ed25519`，可用 `--identity` 指定其他路径；含空格的路径加双引号。

### NAS 端

Docker 网络、Docker 升级和手动目录迁移使用 NAS 的 `sh` 与 Python 3，相关系统命令由脚本预检。复制对应单文件入口即可；手动目录迁移还需要匹配的迁移包目录，见后文。

例如，在电脑的 WSL / Linux 中上传两个 Docker 工具，再登录 NAS：

```bash
NAS_IP=192.168.31.100
scp -i ~/.ssh/id_ed25519 nas-root/fix-docker-bridge.sh \
  nas-root/upgrade-docker-29.9.2.sh "root@$NAS_IP:/home/rootx/"
ssh -i ~/.ssh/id_ed25519 "root@$NAS_IP"
```

进入 NAS 后：

```sh
id -u
cd /home/rootx
```

`id -u` 应输出 `0`。下文的 NAS 命令假设脚本已放在 `/home/rootx/`，放在其他位置时修改路径。使用 `sh 脚本名` 不需要先添加可执行权限，脚本应保持 LF 换行。

## 1. 首次开启 SSH：固件 1.0.9.462

先在小米 Windows 客户端登录当前 NAS 管理账户，准备该账户的客户端 TLS 证书。证书目录通常为 `%LOCALAPPDATA%\minasCert`，应包含：

```text
ca_chain.pem
同一前缀_cert.pem
同一前缀_private_key.pem
```

目录中只保留本次账户的一对证书。客户端 TLS 证书由小米客户端提供；`--identity` 指定的是用于 SSH 登录的私钥，两者用途不同。

在电脑端执行：

```bash
bash nas-root/xiaomi-storage-open-ssh-wsl-1.0.9.462.sh 192.168.31.100
```

指定证书目录和 SSH 私钥：

```bash
bash nas-root/xiaomi-storage-open-ssh-wsl-1.0.9.462.sh 192.168.31.100 \
  --cert-dir "/mnt/c/Users/Windows用户名/AppData/Local/minasCert" \
  --identity "$HOME/.ssh/xiaomi_nas_ed25519" \
  -v
```

| 参数 | 说明 |
| --- | --- |
| NAS IPv4 | 不提供时会提示输入 |
| `--identity 路径` | 复用已有私钥；私钥及同名 `.pub` 都不存在时生成新密钥 |
| `--cert-dir 目录` | 指定已有客户端证书目录，使用 WSL / Linux 路径 |
| `-v` | 显示执行阶段和验证结果 |
| `--help` | 仅显示帮助 |

脚本通过客户端证书认证、临时 SMB 共享和一次性任务尝试开启 SSH，需要空闲的 SMB 账户槽位，目前只接受脚本已确认的单个 `/pool0/data` 内部存储池布局。成功需通过本次执行回执和真实 UID `0` 校验，并清理本次临时任务与共享。

成功后显示登录命令，并询问是否安装开机恢复：输入 `y` 才安装，直接回车跳过。需要安装时，将 `install-ssh-autostart-fix.sh` 放在开启脚本的**同一目录**。

首次开启流程尚未完成纯原厂设备上的端到端和重启验收。端口开放、上传成功或已有 SSH 可用，均不能单独视为本次开启成功。若提示临时状态未清理完成，按输出的 `recovery.txt` 核对撤销；保留的材料中可能含临时凭据。

### 一直等待任务或提示临时共享未清理

[issue #3](https://github.com/daixu0502/xiaomi-nas-plugin/issues/3) 对应的问题已在开启脚本 **2.2.2** 修复。请先重新下载脚本，再重试：

- 原厂 Samba 的强制创建权限会把任务文件变成 `0764`，Cron 会报 `BAD FILE MODE` 并拒绝运行。新版只在本次临时共享内覆盖强制权限，上传文件为 root 所有的 `0644`，保留其他共享设置。
- `smbclient allinfo` 对不存在的文件也可能返回退出码 `0`。新版根据明确的“文件不存在”状态确认删除，网络或认证错误仍按失败处理，避免误报清理成功。

旧版留下的恢复材料仍需核对撤销；若已有 root 终端，删除本次共享的原厂命令是 `nas call smb_mgr.sh del_account one <NAS账户> <槽位>`，随后执行 `nas call smb_mgr.sh reload`。账户和槽位以本次 `recovery.txt` 为准，勿删除其他共享。

已在现有 NAS 验证临时共享、上传权限、一次性 Cron、随机回执及清理；测试载荷只写回执，未重新配置 SSH。此验证不代表纯原厂设备首次开启及重启恢复已经验收。

## 2. 首次开启 SSH：固件 1.0.8.425

旧固件使用单独入口，证书与私钥的准备方式同上：

```bash
bash nas-root/xiaomi-storage-open-ssh-wsl-1.0.8.425.sh 192.168.31.100 \
  --cert-dir "/mnt/c/Users/Windows用户名/AppData/Local/minasCert" \
  --identity "$HOME/.ssh/id_ed25519"
```

此入口使用旧版证书 / WebDAV 流程，成功后自动安装开机恢复；`-v` 显示诊断，`--help` 显示帮助。它不适用于 1.0.9.462。已有可用 root SSH 时，直接使用下一节的开机恢复安装器。

## 3. 安装或更新 SSH 开机恢复

需要现有私钥能够通过 SSH 执行真实 root 命令。先在电脑端验证：

```bash
ssh -i ~/.ssh/id_ed25519 root@192.168.31.100 'id -u'
```

输出 `0` 后，在电脑端安装或更新：

```bash
bash nas-root/install-ssh-autostart-fix.sh 192.168.31.100 \
  --identity "$HOME/.ssh/id_ed25519"
```

该入口默认就是“仅安装开机恢复”，不新增登录公钥、不主动重启 NAS。它安装与 SSH 恢复插件同源的 worker、开机任务及必要的 systemd 配置。

安装后可在 NAS root 终端检查：

```sh
sh /data/minas/ssh/restore-ssh.sh --check
systemctl is-active dropbear.socket
cat /etc/cron.d/keep-ssh
journalctl -t ssh-autostart-fix --no-pager -n 30
```

`--check` 只检查开机检查是否完成及 SSH socket 是否活动；`dropbear.socket` 应显示 `active`，Cron 应指向 `/data/minas/ssh/restore-ssh.sh`。开机阶段进行有限次复查，正常运行后没有全天循环检查。安装验证通过后，仍应在合适时机自行验证一次重启后的登录。

## 4. 修复 Docker 容器网络与 IPv6 出口

以下命令在 **NAS root 终端**执行。

### 检查与修复

```sh
# 只读检查 IPv4
sh /home/rootx/fix-docker-bridge.sh --check

# 只读检查 IPv4 和 IPv6
sh /home/rootx/fix-docker-bridge.sh --check --ipv6
```

检查正常时退出码为 `0`，发现需要调整时为 `2`。需要修复时，按目标选择下面一条：

```sh
# 恢复 IPv4 容器出口
sh /home/rootx/fix-docker-bridge.sh --apply

# 恢复 IPv4，并为默认 bridge 启用 IPv6 出口
sh /home/rootx/fix-docker-bridge.sh --apply --ipv6
```

需要修改网络时，按提示输入 `APPLY`，操作会短暂停止容器并重启 Docker。脚本输出事务编号，并设置 **10 分钟自动回退**；网络已正常时会直接返回。

验证容器的 IPv4/IPv6 出口、DNS、端口和业务连接后，在期限内确认：

```sh
NETWORK_TXN='替换为本次输出的事务编号'
sh /home/rootx/fix-docker-bridge.sh --confirm "$NETWORK_TXN"
```

尚未确认且需要撤销时：

```sh
sh /home/rootx/fix-docker-bridge.sh --rollback "$NETWORK_TXN"
```

### 已启用 IPv6 的设备更新维护方式

```sh
sh /home/rootx/fix-docker-bridge.sh --install-ipv6-events
sh /home/rootx/fix-docker-bridge.sh --check --ipv6
systemctl status minas-docker-ipv6.service --no-pager
cat /run/minas-docker-ipv6/status.json
```

此更新不重启 Docker，日常维护由 Docker、nftables 和路由事件触发，没有每 30 秒或每 10 分钟的兜底轮询。一次性修复的 10 分钟回退期限仍然保留。

| 其他参数 | 说明 |
| --- | --- |
| `--ipv6-subnet fd6d:696e:6173:1::/64` | 与 `--ipv6` 配合，自定义不冲突的 ULA `/64` 子网 |
| `--yes` | 仅用于 `--apply`，明确允许中断容器并跳过交互输入 |
| `--help` | 显示参数帮助 |

IPv6 功能为默认 bridge 提供 NAT66 出口，不自动开放公网入站端口，也不重建已有的 IPv4-only 自定义网络。详细说明见 [Docker 网络修复](docker-bridge.md)。

## 5. 安装 Docker 29.9.2 与回退

适用于脚本预检支持的 ARM64 小米 NAS、原厂 Docker 配置流程和 `overlay2` 数据布局。以下命令在 **NAS root 终端**执行。

### 检查、准备与安装

```sh
# 只读检查；不传参数时也是检查
sh /home/rootx/upgrade-docker-29.9.2.sh --check

# 下载并校验运行时，不切换现有引擎
sh /home/rootx/upgrade-docker-29.9.2.sh --prepare

# 可选：启动临时隔离引擎验证基础启动
sh /home/rootx/upgrade-docker-29.9.2.sh --probe
```

按 `--check` 输出另行备份外部可写 bind mount 的业务数据。脚本对 Docker 数据根的备份不包含这些外部目录。

准备 root 专用的备份父目录后安装：

```sh
mkdir -p /data/minas/backups/docker
chmod 700 /data/minas/backups /data/minas/backups/docker

sh /home/rootx/upgrade-docker-29.9.2.sh --install \
  --backup-dir /data/minas/backups/docker \
  --bind-backups-confirmed
```

备份目录需要足够空间，位于真实 ext4/btrfs/xfs 文件系统，不能使用 `/nas/pool0` 的 FUSE 路径、tmpfs 或网络目录。`--bind-backups-confirmed` 表示已由操作者确认外部数据备份。

按提示输入 `INSTALL` 后，脚本停止容器，完成数据备份及一致性校验，再切换引擎；失败时尝试回退。记录输出的事务编号，并验证容器、端口、DNS、管理工具及应用数据。

### 查看事务与回退

```sh
sh /home/rootx/upgrade-docker-29.9.2.sh --status

DOCKER_TXN='替换为要回退的升级事务编号'

# 只读验证备份与回退兼容性，不停止服务
sh /home/rootx/upgrade-docker-29.9.2.sh --rollback-check "$DOCKER_TXN"

# 正式回退，会停止容器；按提示输入 ROLLBACK
sh /home/rootx/upgrade-docker-29.9.2.sh --rollback "$DOCKER_TXN"
```

默认回退范围：

| 内容 | 默认回退行为 |
| --- | --- |
| 原厂引擎、镜像、容器可写层及引擎元数据 | 恢复升级前状态 |
| 本地卷的 `_data/` 内容 | 保留回退时的当前数据 |
| 容器运行集合和 restart policy | 恢复升级前记录 |
| 外部 bind mount 业务数据 | 不由脚本还原 |

如果明确需要将本地卷内容也覆盖为升级前备份：

```sh
sh /home/rootx/upgrade-docker-29.9.2.sh \
  --rollback-check "$DOCKER_TXN" --restore-all-data

sh /home/rootx/upgrade-docker-29.9.2.sh \
  --rollback "$DOCKER_TXN" --restore-all-data
```

完整数据恢复需输入 `RESTORE-ALL-DATA`，会覆盖升级后对本地卷的改动。默认保留卷回退有容器、镜像和卷兼容性检查；新增或重建过容器、卷时可能被阻止，应按应用需要处理数据兼容性。

最初上传的 sh 文件不在时，可使用持久程序执行相同操作：

```sh
python3 -B /data/minas/docker/runtime/upgrade.py --status
python3 -B /data/minas/docker/runtime/upgrade.py --rollback-check "$DOCKER_TXN"
python3 -B /data/minas/docker/runtime/upgrade.py --rollback "$DOCKER_TXN"
```

| 其他参数 | 说明 |
| --- | --- |
| `--archive /path/to/docker-29.9.2.tgz` | 准备运行时时使用离线官方 ARM64 包，仍检查固定摘要 |
| `--proxy http://127.0.0.1:7890` | 仅为运行时下载指定 HTTP/HTTPS 代理 |
| `--yes` | 仅用于安装或回退，明确允许中断容器并跳过交互输入 |
| `--help` | 显示参数帮助 |

详细的备份范围、精确恢复、旧事务兼容性及验证边界见 [Docker 29.9.2 安装与回退](docker-upgrade-29.9.2.md)。

## 6. 自定义目录迁移与失败恢复

### 常规安装采用自动迁移

新版插件安装包、SSH 开机恢复安装器及 Docker 工具，执行安装、更新或修改操作时会检查旧目录：

- 全新设备直接建立 `/data/minas/` 布局。
- 旧设备先迁移实际存在的组件，再继续原操作，不要求七插件全部安装。
- 保留配置、任务、密钥、插件版本和服务原启停意图；镜像与卷数据不参与目录搬迁。
- 涉及旧自定义 Docker 运行目录时，会协调短暂停止 Docker 和容器。
- 只读检查和状态查询不会启动自动迁移；新旧目录冲突时停止，不自动合并覆盖。

### 手动迁移

`migrate-minas-data.sh` 是维护入口，**手动检查和执行均需要 `--bundle` 指向与当前 NAS 匹配的迁移包目录**，其中包含 `plan.json` 和校验过的配套文件。它不是插件安装包 `.tar.gz`，只复制此 sh 文件不能完成手动迁移；常规使用应选择上面的自动迁移流程。

已准备好匹配材料时，在 NAS root 终端执行：

```sh
BUNDLE='/data/已准备好的迁移包目录'

# 只读预检迁移计划
sh /home/rootx/migrate-minas-data.sh --check --bundle "$BUNDLE"

# 执行迁移，按提示输入 MIGRATE
sh /home/rootx/migrate-minas-data.sh --apply --bundle "$BUNDLE"

# 已有迁移事务时查看状态
sh /home/rootx/migrate-minas-data.sh --status
```

`--yes` 可与 `--apply` 配合跳过交互确认。迁移未完成、需要恢复原布局时，使用该迁移事务的编号：

```sh
LAYOUT_TXN='替换为未完成的迁移事务编号'
python3 -B /data/minas/migration/migrate.py --rollback "$LAYOUT_TXN"
```

失败通常会自动恢复；断电等中断后，再运行新版安装入口会先处理未完成事务。保留维护保护和恢复材料，按错误提示处理，不能手动启动受保护服务。状态已经是 `committed` 的成功迁移不能用旧事务回滚覆盖后续数据。

目录迁移、Docker 网络修复、Docker 升级分别管理事务，编号不能混用。完整布局和兼容策略见 [自定义持久目录](data-layout.md)。

## 运行文件与备份保留

| NAS 路径 | 用途 |
| --- | --- |
| `/data/minas/ssh/` | SSH 开机 worker 与独立恢复材料 |
| `/data/minas/privileged/modules/sshrecovery/var/` | SSH 恢复插件状态、公钥、主机密钥备份与操作锁 |
| `/data/minas/docker/network/` | 网络修复程序、IPv6 运行库、规则摘要和网络事务 |
| `/data/minas/docker/runtime/` | Docker 独立运行时、升级／回退程序及升级事务 |
| `/data/minas/backups/docker/` | 按上述示例保存的 Docker 升级前备份 |
| `/data/minas/migration/` | 迁移程序、事务及失败恢复材料 |
| `/run/minas-docker-ipv6/` | 内存中的 IPv6 监听状态与运行锁 |

运行目录中的配置、凭据、锁和恢复材料不能整体当作缓存删除。Docker 升级备份、对应事务，以及中断回退时数据根内的 `.minas-rollback-volumes-*` 暂存应保留，后者可能包含唯一的当前卷数据。旧备份路径由新版程序映射到迁移后的位置，无需手动修改旧事务 JSON。

[原厂插件管理脚本](firmware-reference/README.md) 仅供核对行为，不用于覆盖系统文件。
