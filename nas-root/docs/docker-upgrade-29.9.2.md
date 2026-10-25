# Docker 29.9.2 安装与回退

入口是 `nas-root/upgrade-docker-29.9.2.sh`。只需复制这一个文件到 NAS，在 NAS 的 root SSH 终端执行，不在 WSL 本机执行安装。

本工具针对已评估的 ARM64 小米 NAS：原厂 Docker 20.10.17、systemd/cgroup v2、`/data/docker_data`、`overlay2` 和原厂 `dockerd_prepare.sh`。不覆盖签名分区 `/data/docker`，不修改 Docker OTA 组件版本，不重建容器或改变镜像存储模式。

## 先检查

```sh
sh /path/to/upgrade-docker-29.9.2.sh --check
```

检查只读取当前运行环境，列出外部可写挂载及需要保留的容器状态。不下载程序、不停止服务、不创建持久事务目录。Docker 已停止、布局不符、swarm/live-restore、自定义运行时、远程卷或带额外挂载选项的命名卷启用时，工具会停止操作。切换和回退前还检查原数据目录身份、外部持久数据挂载是否仍然存在，防止磁盘未挂载时误用空目录。

默认不传参数也是检查；`--status` 只读查看已存在的事务和维护状态。

## 下载与隔离测试

```sh
# 下载、核对并解压官方完整运行时，原厂引擎保持运行
sh /path/to/upgrade-docker-29.9.2.sh --prepare

# 网络需要代理时显式指定；代理仅用于下载
sh /path/to/upgrade-docker-29.9.2.sh --prepare --proxy http://127.0.0.1:7890

# 启动独立 socket/data-root/exec-root 的临时引擎测试
sh /path/to/upgrade-docker-29.9.2.sh --probe
```

隔离测试不创建容器，关闭 bridge、iptables/ip6tables、转发和 masquerade，不接管现有数据或修改原厂 Docker 配置。它只能证明新引擎基础启动，不能代替端口、DNS、Portainer、原厂智能中枢和重启验收。正式安装也会先执行这一步。

运行时位于 root 专用目录 `/data/minas/docker/runtime/runtime/29.9.2/`。使用官方发布中配套的 docker、dockerd、containerd、shim、runc、proxy、init 和 ctr；核验 ARM64 ELF、所有文件摘要和每个 Go 程序的真实编译信息。当前官方包中七个 Go 程序均为 Go 1.26.10。

离线官方包也可使用，仍核对同一固定摘要：

```sh
sh /path/to/upgrade-docker-29.9.2.sh --prepare --archive /path/to/docker-29.9.2.tgz
```

官方来源：<https://download.docker.com/linux/static/stable/aarch64/docker-29.9.2.tgz>

本次从官方 HTTPS 站点下载文件计算并固定的 SHA-256：`873b3e0207857c3b2f28392fb922f3ea6efe37620c16d4d4aaf2fef409c90b73`。这是内容固定校验，不宣称存在额外的上游签名验证。

## 安装

先停止会主动改动 Docker 的自动化操作，备份 `--check` 列出的外部可写挂载。Docker 数据备份包含镜像、容器元数据和数据根内的本地卷，不包含外部 bind mount、远程卷或其他主机数据。

备份父目录必须预先建立，由 root 拥有、普通用户不可写，并位于真实 ext4/btrfs/xfs 文件系统。不要用 `/nas/pool0` 的 FUSE 路径、tmpfs 或网络目录。以下示例使用内置 `/data` 的独立目录；空间不足时可改为真实磁盘路径下的 root 专用目录。

```sh
# 首次使用先准备运行时，同时建立安全的父目录
sh /path/to/upgrade-docker-29.9.2.sh --prepare
mkdir -m 700 /data/minas/backups/docker

sh /path/to/upgrade-docker-29.9.2.sh --install \
  --backup-dir /data/minas/backups/docker \
  --bind-backups-confirmed
```

交互时需输入 `INSTALL`；非交互执行须明确添加 `--yes`。`--bind-backups-confirmed` 表示操作者确认已备份外部可写挂载，脚本不会把它当成自动备份已完成的证明。

安装会短暂停止全部容器。它记录原容器 ID、运行集合和 restart policy，临时取消自动重启，停止 Docker 服务和 socket，确认运行时及数据子挂载均退出后，用 `rsync -aHAXS --checksum --modify-window=-1 --numeric-ids --one-file-system --no-specials` 复制普通文件、目录、链接和设备节点，再精确复制下述 socket/FIFO 节点，保留所有权、权限、纳秒时间戳、硬链接、ACL、扩展属性及稀疏文件。备份会做逐内容比较和完整指纹登记，未完成前不会让新引擎接触原数据。

容器停止后仍可能残留 Unix socket 和 FIFO。rsync 对 socket 的便携重建方式会受 Unix socket 地址长度限制，即使普通文件路径合法，也可能因较长备份前缀报 `File name too long`。本工具不丢弃这些节点：在目标同目录通过 Linux `mknod` 创建临时节点并原子替换，不调用 socket bind、不连接或打开 FIFO；恢复节点类型、权限、属主、时间戳、扩展属性及硬链接关系，并恢复父目录时间戳。对节点清单和所有这些元数据另行严格比较。设备白化节点仍由 rsync 的设备复制功能处理。停服务前的备份存储预检包含长路径 socket/FIFO 以及普通/符号链接硬链接样本和整树指纹校验，复制与回退使用同一实现。

新版事务增加 `specialNodePolicy: mknod-v1`，备份指纹也覆盖 socket/FIFO 的硬链接关系；旧版备份仍使用建立时的指纹格式进行校验。rsync 的 `--no-specials` 仅避免由 rsync 创建 socket/FIFO，这些节点由原生 mknod 另行备份和还原。

符号链接自身的权限位也必须精确一致，不再豁免 rsync `.L...p.....` 差异。当前 NAS 的 ext4 上原链接可为 `0755`，普通 rsync 在私有进程 umask 下重建链接会成为 `0700`，又无法通过普通 chmod 修正。工具在复制后、校验前按原链接权限设置临时 umask，在目标所在目录创建临时链接并原子替换；不跟随链接修改其指向的文件。链接指向、属主、纳秒时间戳和扩展属性均保留，并重新连接同一 inode 的多个硬链接；随后还原受影响父目录的时间戳。

停服务前会从数据所在文件系统创建 `0755`、`0700`、`0777` 的链接样本，确认指定备份文件系统可以精确保存。如果文件系统不能表达原权限位，拒绝安装。备份和回退均执行精确修复及严格比较，任何剩余差异会显示具体路径，并写入事务目录的 `backup-verify.diff` 或 `restore-verify.diff`（仅 root 可读）。不能通过关闭权限或内容校验绕过错误。

新版事务标记为 `symlinkModePolicy: exact-v1`，备份树指纹包含原始链接权限位。旧版允许权限差异的备份没有保存丢失的原权限信息，无法从 `0700` 猜测原来是否为 `0755`；精确恢复版会在停止现有引擎之前拒绝还原此类旧备份，需保留并使用建立该旧事务的回退程序。已经回退的失败事务无需再还原，重试安装会创建新的精确备份。

仅新增本工具的 systemd drop-in：保持原厂配置生成、启动后智能中枢钩子和现有代理设置，在服务 PATH 前添加新运行时目录，并把生成的配置转换为保留 `overlay2`、关闭 containerd image store 的私有配置。IPv6 已开启时，升级前检查要求 `minas-docker-ipv6.service` 已启用、正在运行并使用 `--watch-ipv6` 事件模式，沿用其 NAT66 方案，避免启用 NAS 缺失的内核 ip6_tables；不依赖周期定时器。Docker 自身启动前生成配置时只检查该事件服务已安装、启用，不要求它已运行，以符合事件服务在 Docker 之后启动的顺序。旧版 IPv6 维护先使用最新 `fix-docker-bridge.sh --install-ipv6-events` 切换，无需重启 Docker。

切换后恢复原容器运行集合和 restart policy，验证实际 daemon/containerd/shim 路径、新旧 API 连通性及数据路径。原厂绝对路径的旧 CLI 保留，交互使用新版 CLI 可直接调用：

```sh
/data/minas/docker/runtime/runtime/29.9.2/docker version
```

### 重启后回到 20.10.17 的修复

该固件由 `minas.syspreinit.service` 挂载 `/etc` overlay，而 systemd 初始启动任务可能已经缓存了原厂 Docker 单元。升级 drop-in 文件存在，不代表开机启动的进程已使用它。`--status` 的 `runtimeSelection` 分别报告已加载配置、实际进程路径和 API 服务端版本。

新版安装成功后登记 `/etc/cron.d/minas-docker-runtime` 的 `@reboot` 一次性校正任务。在 `/etc` overlay 可用后，它等待必要的外部数据挂载和 Docker 接口，重新加载服务配置；若仍运行原厂 20.10.17，则短暂停止容器并切换到已核验的 29.9.2，恢复校正当时的容器运行集合和 restart policy。已经运行目标引擎时不重启。任务最长运行 360 秒，不安装周期轮询。

这是开机后的补偿机制，不能保证在固件早期原厂 Docker 第一次启动前完成接管。开机过程中可能先运行原厂引擎，再发生一次容器切换；需要避免任何旧引擎访问新版元数据的环境，应先解决原厂早期启动入口后再使用升级方案。

已经安装过 29.9.2 的设备可用原成功事务补装修复，不重新备份或还原引擎数据：

```sh
sh /home/rootx/upgrade-docker-29.9.2.sh --status
sh /home/rootx/upgrade-docker-29.9.2.sh --repair-startup 20261023-225226-21632746
# 非交互执行需明确添加 --yes；会短暂中断正在运行的容器。
```

将示例编号换成自己状态中 `phase=installed` 的事务。修复使用当前容器状态，保留新增容器、镜像和卷数据，不调用数据回退。实际切换前先核对挂载、数据目录身份和新运行时摘要，拒绝未知引擎、自定义启动配置或其他操作留下的维护锁。

`startup-repair.json` 保存本次切换的容器启停意图，`startup-last.json` 记录自动校正结果，均留在受保护的运行目录。切换失败时保留维护锁及修复记录，停止服务；修正问题后以同一 `--repair-startup ID` 显式重试。回退升级事务会撤销该事务的开机校正任务。

首次安装失败时自动尝试回退，默认保留本地卷内已写入的数据；开机校正不调用数据回退。若还原失败，保留维护锁，在配置重载后阻止服务和 socket 再次启动；开机任务检测到维护锁会停止误启动的引擎。此保护仍受上述早期启动限制约束，不能保证原厂引擎从未启动。修正存储或备份问题后，用同一事务、同一数据模式继续回退。

## 回退

安装会输出事务编号，例如 `20261023-180000-1234abcd`。不依赖最初复制的 sh 文件：

```sh
python3 -B /data/minas/docker/runtime/upgrade.py --status
python3 -B /data/minas/docker/runtime/upgrade.py --rollback-check 20261023-180000-1234abcd
python3 -B /data/minas/docker/runtime/upgrade.py --rollback 20261023-180000-1234abcd
```

`--rollback-check` 只读校验备份指纹、挂载和当前卷/容器的兼容条件，不停止服务、不创建维护标记、不写入事务；可在 Docker 29.9.2 运行时执行。实际回退仍会中断容器，非交互执行须加 `--yes`。

**默认回退保留当前本地卷数据。** 使用原来的 `--rollback ID` 命令即可，不需要重新安装 Docker 或重新建立备份。已有 `exact-v1` 精确备份按原指纹格式校验，不修改备份或升级事务的原始内容；不要求旧事务包含新增加的卷清单。

| 数据或配置 | 默认 `--rollback ID` | 加 `--restore-all-data` |
| --- | --- | --- |
| 原厂引擎、镜像、overlay2 层、容器可写层及引擎元数据 | 恢复升级前状态 | 恢复升级前状态 |
| 本地卷 `volumes/<名称>/_data/` | 保留回退时的数据及原目录 inode | 覆盖为升级前备份 |
| 卷注册数据库和卷目录的外围元数据 | 恢复升级前状态 | 恢复升级前状态 |
| `/nas/pool0` 等外部 bind mount 的业务数据 | 不作还原 | 不作还原 |
| 容器运行集合与 restart policy | 恢复升级前状态 | 恢复升级前状态 |

默认模式在停服务前要求当前与备份的卷名称完全一致，卷为无额外挂载选项的本地卷，容器 ID 和各容器所用镜像 ID 未改变。新增、删除、重建容器/卷，或升级过应用镜像时，拒绝直接将当前卷交给旧应用，应先按应用需要单独迁移；不能为了绕过检查直接选择完整恢复而丢弃需要保留的数据。旧事务首次执行保留卷回退需能读取当前 Docker API；新安装事务保存卷清单，安装失败时可据此离线自动回退。已建立保留计划的中断回退可以离线续接。

停止引擎、socket 并确认没有运行时或数据子挂载后，将当前整个 `volumes/` 原子改名到数据根内的 `.minas-rollback-volumes-<事务编号>/`，设置 root 专用权限。暂存必须处于数据根的同一挂载内：即使底层设备相同，跨 bind mount 的 rename 仍会失败。恢复引擎数据时排除整个卷目录和该暂存目录；恢复旧卷注册信息时排除每个 `_data/`，再将当前各 `_data/` 原目录原子移回。卷内容不复制、不覆盖，权限、属主、扩展属性、链接和目录 inode 随原目录保留。事务先记录数据目录身份，改名后同步父目录；中途失败可根据原 inode 判断数据所在位置继续，位置不明时拒绝覆盖。已开始的事务不能切换数据模式。成功后暂存中的原卷注册信息保留作为恢复材料，不是第二份卷数据。

完整备份继续保留，镜像层与引擎数据库一起恢复，以避免将新引擎写过的元数据直接交给旧引擎。容器可写层仍恢复到升级前，写在容器层中的业务数据不会按卷方式保留。需要把卷也恢复到升级前时间点时，明确执行：

```sh
python3 -B /data/minas/docker/runtime/upgrade.py \
  --rollback 20261023-180000-1234abcd --restore-all-data
```

完整恢复交互需输入 `RESTORE-ALL-DATA`；它会覆盖升级后对本地卷的改动。外部挂载数据应另按应用备份安排一致性恢复。

两种模式都会撤销本工具切换配置，启动原厂引擎并恢复原运行集合与 restart policy。原厂签名目录及其他服务 drop-in 不被覆盖；发现本工具文件被手动修改时停止。降级前仅移除精确匹配的新 Docker FORWARD 入口规则，不清空防火墙表、不修改 NAS 自定义规则或独立 NAT66 规则。规则变化背景见 [Docker 28 官方降级说明](https://docs.docker.com/engine/release-notes/28/#port-publishing-in-bridge-networks)。

备份保留用于人工验收，不提供默认超时删除。请在端口、DNS、镜像代理、管理插件、Portainer、ARMv7 智能中枢及 NAS 普通重启均验证后，再单独处理旧备份。事务记录可能包含私有挂载路径，不要公开。

## 文件与验证边界

- `/data/minas/docker/runtime/`：固定运行时、私有配置、维护标记、事务和独立回退程序。
- `/etc/systemd/system/docker.service.d/80-minas-runtime.conf`：本工具切换配置。
- `docker.service.d/79-minas-upgrade-guard.conf` 与 `docker.socket.d/79-minas-upgrade-guard.conf`：维护期间的重启保护。
- 指定备份父目录内的 `docker-before-<事务编号>/data/`：一致数据备份。必要的回退备份不能当缓存清除。
- `/data/docker_data/.minas-rollback-volumes-<事务编号>/`：root 专用的卷保留暂存；中断时可能包含唯一的当前卷数据，禁止当缓存删除。

2026-10-23 验证：38 项本地隔离测试通过，覆盖包校验、Go 安全门槛、真实 rsync 内容/硬链接/扩展属性还原、受损备份阻断、失败维护锁、容器状态恢复、FUSE 重挂载、严格链接权限比较及旧备份兼容性保护；shell 语法检查通过。单文件在当前 NAS 执行 `--check` 通过。NAS `/data` 下自动清理的私有测试目录已真实完成 `0755`、`0700`、`0777`、`0640`、`0000` 链接的备份、删除/修改后还原；备份和还原全树指纹均与原树一致，包括权限、属主、纳秒时间戳、文件扩展属性、普通/符号链接硬链接关系及父目录时间戳。指向外部文件的链接未改变该外部文件内容或权限，Docker 主进程 PID 保持不变。

另在 NAS 私有临时目录验证了 157 字节路径的 socket/FIFO、设备白化节点、纳秒时间戳、普通文件扩展属性及多种硬链接的真实备份还原，整树指纹一致；实际备份父目录的增强预检通过。该 NAS 文件系统不支持给 socket/FIFO 设置 POSIX ACL，因此不宣称已完成特殊节点 ACL 的真机测试。新增本地测试覆盖节点元数据损坏、硬链接破坏、文件类型变化和 mknod 失败保护。

2026-10-24 验证：54 项本地测试通过；在 NAS `/data` 的隔离目录执行 17 项回退测试通过，包括真实 bind mount 布局、已有格式备份、卷内容与 inode/权限/属主/扩展属性/链接保留、完整数据恢复、两次原子改名位置的中断续接、被替换数据拒绝覆盖及只读检查不写入事务。服务控制与 Docker API 在这些数据恢复测试中使用模拟，未对生产引擎执行回退。

当前生产 Docker 已由操作者升级到 29.9.2。本次只更新独立回退程序及 sh 入口，并使用原成功事务 `20261023-225226-21632746` 的备份进行只读回退条件校验；备份完整指纹通过，六个本地卷兼容。不能将隔离数据测试或只读校验理解为生产降级、实际网络和重启验收已完成。

2026-10-25：73 项本地隔离测试通过，新增开机一次性校正、已有正确引擎不重启、按当前容器状态恢复、中断重试及维护保护测试。NAS 当前已切回 29.9.2，9 个容器的 ID、镜像及 restart policy 保持一致，原来运行的 8 个容器已恢复。随后已补装开机校正入口，并手动执行同一 `--boot-reconcile` 入口，结果为 `changed=false`，引擎 PID、容器集合和原升级事务记录未改变。完整整机重启后的 20 → 29 校正链路尚未验收。

参考：[Docker 官方静态包说明](https://docs.docker.com/engine/install/binaries/)、[29.9.2 发布说明](https://docs.docker.com/engine/release-notes/29/)、[镜像存储升级行为](https://docs.docker.com/engine/storage/containerd/)。

符号链接语义参考：[Linux symlink(7)](https://man7.org/linux/man-pages/man7/symlink.7.html)、[rsync 差异格式说明](https://download.samba.org/pub/rsync/rsync.1)。
