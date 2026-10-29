# 小米 NAS SSH 恢复

## 功能说明

- 查看 SSH 服务、监听端口、服务器身份密钥、恢复公钥及实际登录授权状态。
- 手动开启 SSH，按保存的材料恢复登录公钥和必要的服务器身份密钥。
- 导入自己的登录公钥，或在明确选择后生成并下载新的登录密钥。
- 安装或更新开机 SSH 修复，下载离线应急包，并查看操作记录。

## 如何使用

### 安装和更新

1. 在电脑的 **Ubuntu（WSL）终端**中，进入有 `manage.sh` 的插件文件夹，运行：

```bash
bash manage.sh
```

2. 输入 `1`，选择“安装 / 更新”，按提示输入 NAS 的 IP，再选择自己的账号。只有一个账号时会自动选择。
3. 等待提示安装成功，再到小米客户端的应用列表打开本插件。看不到时，退出客户端后重新打开。

更新也是这三步，已有配置会保留。需要先让电脑能用 root SSH 登录 NAS；还没开启时，见[SSH 开启步骤](../nas-root/README.md#开启-ssh)。

更新插件会同步已有的开机修复和应急恢复程序，保留 SSH 的当前运行和开机启用状态。首次安装插件时，是否安装开机修复仍由你在页面中选择。

### 恢复 SSH

1. 打开“SSH 恢复”的“状态”页。点击“管理恢复公钥”，在弹窗中导入自己的 `.pub` 文件或粘贴公钥，再保存。
2. 点击“恢复 SSH”，核对并确认操作。没有保存公钥且未选择生成密钥时，只开启 SSH。
3. 如需生成新密钥，先勾选“无公钥时生成登录密钥”，再执行恢复，并及时下载密钥包保存到电脑。
4. 使用对应私钥从电脑实际登录，验证授权是否生效。例如：

```bash
ssh -i /path/to/private_key -p 22 root@192.168.31.100 id
```

将私钥路径、端口及地址换成实际值。

### 开机修复与应急恢复

需要开机后自动恢复 SSH 时，进入“开机恢复”页，点击“安装 / 更新开机修复”并确认。该操作独立于插件安装和启用。

“开机 SSH 修复”分别显示当前已安装脚本和插件自带脚本的版本，从各自脚本的版本标记读取。旧脚本没有标记时显示“未标记”及 SHA256 内容摘要；未安装时显示“未安装”。开机修复脚本版本独立于插件发布版本，统一维护在 `installer/common/modules/sshrecovery/restore-ssh.sh` 的 `SSH_RECOVERY_VERSION`；独立安装器的标题、帮助和内嵌脚本均从这里构建，目前为 2.1.2。已安装脚本版本以设备上的实际文件为准，页脚插件版本由接口读取已安装插件的 `INFO.version`。

在“应急恢复”页提前下载“离线应急包”并保存到电脑；在“操作记录”页查看历史结果。SSH 和插件都无法访问时，可通过提前准备的 Portainer、BusyBox 救援容器恢复。完整操作见下方[离线应急恢复详细步骤](#离线应急恢复详细步骤)。

### 卸载

再次运行 `bash manage.sh`，输入 `2` 选择“卸载”，按提示选择账号并确认。

请用这个菜单卸载；只在小米客户端里删除，插件可能会自动恢复。手动指定账号等用法见[进阶说明](../docs/plugin-installation.md)。

## 离线应急恢复详细步骤

这套方法适用于：**NAS 仍能正常开机、Docker 和 Portainer 仍可使用，但 SSH 连不上，SSH 恢复插件也打不开。**

“离线”指恢复时不用联网下载程序或镜像。NAS、电脑之间仍需有局域网连接。宿主机就是 NAS 本身；BusyBox 是临时使用的救援工具箱，Portainer 是打开这个工具箱的网页入口。

恢复流程是：**浏览器进入 Portainer → 启动 BusyBox → 打开容器终端 → 执行应急包 → 等待 NAS 开启 SSH → 从电脑验证登录。**

请在 NAS 正常时先完成第一至第四步。发生故障后，从第五步开始。

### 第一步：保存公钥、私钥和离线应急包

1. 在“SSH 恢复”插件的“登录公钥恢复材料”中保存自己的公钥，并用对应私钥从电脑实际登录 NAS，确认可用。
2. 如果选择让插件生成新登录密钥，及时下载密钥包，把**私钥单独保存在电脑**。私钥是登录凭据，不要上传到 NAS 共享文件夹，也不要发给其他人。
3. 点击“下载离线应急包”，把 `nas-ssh-emergency.zip` 保存到电脑；建议再备份一份到 U 盘。更新插件或更换登录公钥后，重新导出一份。
4. 在电脑上解压，检查是否有以下文件：

| 文件 | 用途 |
| --- | --- |
| `recover-ssh.py` | 执行 SSH 恢复的主程序 |
| `restore-ssh.sh` | 开机 SSH 修复脚本 |
| `manifest.json` | 记录导出时的插件版本和 NAS 用户编号 |
| `recovery.pub` | 已保存的登录公钥；导出时有保存公钥才会包含 |
| `README.txt` | 随包提供的操作说明与恢复命令 |
| `SHA256SUMS` | 检查文件是否完整的校验清单 |

应急包不包含登录私钥、NAS 主机私钥或容器镜像。没有 `recovery.pub` 时，准备自己的 OpenSSH `.pub` 公钥文件，留待第六步使用。

记录 NAS 的局域网 IP、Portainer 地址和管理员密码、SSH 端口，以及私钥存放位置。下面以 `192.168.31.100` 为 NAS 地址、`u123456789` 为用户编号、`pool0` 为存储池举例，实际操作要换成自己的值。**用户编号可在应急包的 `manifest.json` 中查看，不是小米账号昵称。**

### 第二步：准备 Portainer 网页管理入口

**已经有 Portainer 的用户，沿用现有地址和账号，确认能打开本机 Docker 环境，然后进入第三步。** 现有地址可能是 `http://NAS地址:9001`，以自己的实际端口映射为准。

没有 Portainer 时，在 NAS 正常、SSH 仍能登录的时候，先连接到 **NAS 的 root SSH 终端**，再执行以下命令。这些命令在 NAS 上执行，不是在电脑的普通终端直接执行：

```sh
/data/docker/docker volume create portainer_data

/data/docker/docker run -d \
  --name portainer \
  --platform linux/arm64 \
  --restart always \
  -p 9443:9443 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v portainer_data:/data \
  portainer/portainer-ce:lts
```

这会创建 Portainer，并把管理配置保存在 `portainer_data` 数据卷中。首次部署需要 NAS 能联网下载镜像；端口 `9443` 需要未被其他服务占用。示例直接使用 NAS 自带位置的 Docker 客户端，不要求安装 Docker Compose 命令。

部署后，在**电脑浏览器**打开 `https://192.168.31.100:9443`，把地址换成自己的 NAS IP。首次访问可能出现自签名证书提示，核对地址确实属于自己的 NAS 后继续。按页面提示创建管理员账号，选择本机 Docker 环境；菜单可能叫 `local` 或“本地环境”。

如果首次设置要求令牌，在 **NAS root SSH 终端**查看容器日志，按 Portainer 页面提示完成设置：

```sh
/data/docker/docker logs --tail 100 portainer
```

管理密码和设置令牌只用于自己的管理操作，不要公开。这个入口能够管理 NAS 上的容器，应只供可信管理员在局域网中使用。Portainer 的安装和端口说明见[官方文档](https://docs.portainer.io/start/install-ce/server/docker/linux)。

### 第三步：部署 BusyBox 救援容器

BusyBox 提供一个可以输入命令的终端。恢复时，它会把 NAS 的文件系统接进来，再使用 **NAS 本身的 Python** 执行应急程序，因此不用另装 Python 容器或 SSH 容器。

1. 在 Portainer 中选择 NAS 的本机 Docker 环境。
2. 左侧打开 **Stacks（堆栈）→ Add stack（添加堆栈）**。Stacks 就是用一份配置集中创建和管理容器的功能；NAS 终端没有 `docker compose` 命令也可以使用此入口。
3. 名称填写 `ssh-rescue`，选择 **Web editor（网页编辑器）**，粘贴下面的完整配置：

```yaml
services:
  ssh-rescue:
    image: busybox:1.37.0
    platform: linux/arm64
    container_name: ssh-rescue
    user: "0:0"
    network_mode: none
    restart: "no"
    cap_add:
      - SYS_CHROOT
    volumes:
      - type: bind
        source: /
        target: /nas
        read_only: false
    command:
      - sh
      - -c
      - "while :; do sleep 3600; done"
```

4. 点击 **Deploy the stack（部署堆栈）**。首次需要联网下载镜像；完成后，在 **Containers（容器）** 中应看到状态为 `running` 的 `ssh-rescue`。

这份配置使用支持 ARM64 的 [BusyBox 官方镜像](https://hub.docker.com/_/busybox)。`source: /` 表示 NAS 根目录，`target: /nas` 表示它在容器中的位置，`read_only: false` 允许恢复程序修改 NAS 文件。该容器具有修改宿主机文件的能力，只供管理员救援使用。它没有对外开放端口，启动后只等待命令，**不会自动执行 SSH 恢复**。

`restart: "no"` 表示不会随 Docker 重启自动运行。平时可在 Portainer 中停止它，保留容器和镜像；救援时手动启动。Stacks 的操作说明见[Portainer 官方文档](https://docs.portainer.io/user/docker/stacks/add)。

### 第四步：提前验证入口并备份镜像

先保留当前可用的 SSH 连接，在 **Portainer 网页**中操作：

1. 打开 **Containers → ssh-rescue**，确认容器已启动。
2. 点击 **Console（控制台）**。命令选择 `/bin/sh`，用户填写 `0`；没有该命令选项时，打开自定义命令并填写 `/bin/sh`。
3. 点击 **Connect（连接）**，在出现的 **BusyBox 容器终端**中逐条执行：

```sh
id
ls /nas/etc/cron.d
chroot /nas /usr/bin/python3 --version
```

`id` 应包含 `uid=0(root)`；第二条应能列出 NAS 的 Cron 目录；第三条应显示 NAS 的 Python 3 版本。`chroot /nas` 可以理解为“让接下来的程序把 `/nas` 当作 NAS 根目录”。这三条只检查入口，不执行 SSH 修复。

宿主机 Cron 是否运行，在 **NAS root SSH 终端**检查：

```sh
systemctl is-active crond.service
```

正常应显示 `active`。救援用的是 NAS 的 Cron，不是在 BusyBox 容器中启动 Cron。能显示 Python 版本只说明进入宿主机的入口可用，真正恢复后仍需用电脑验证 SSH 登录。

为了在断网时也能重新创建救援容器，再保存以下材料：

1. 在 Portainer 的 **Images（镜像）**中找到 `busybox:1.37.0`，选中后点击 **Export this image（导出镜像）**，把浏览器下载的镜像文件保存到电脑和 U 盘。
2. 同样导出**当前 Portainer 容器实际使用的镜像**，并记下镜像名称、标签和管理端口；可在容器详情中查看。镜像导出不包含 `portainer_data` 中的管理配置，配置需要另外备份。
3. 把第三步的 YAML 保存为 `ssh-rescue.yml`，与离线应急包一起留存。不要把救援镜像当作可随意清理的无用镜像。

导出操作见[官方说明](https://docs.portainer.io/user/docker/images/export)。以后 BusyBox 镜像丢失而 Portainer 仍可使用时，可通过 **Images → Import（导入）**上传之前保存的镜像文件；再使用本地已有镜像创建容器，关闭界面中的强制重新拉取选项（如有）。[镜像导入说明](https://docs.portainer.io/user/docker/images/import)

验证完成后，关闭 Console，并在 Portainer 中停止 `ssh-rescue`。关闭终端窗口本身不会停止容器。NAS 的根目录挂载可能在开机后才完成，实际救援时应等 NAS 启动和存储池挂载完成，再启动救援容器。

### 第五步：故障时，把应急包放到 NAS

SSH 连不上、插件打不开时，先确认还能从浏览器登录 Portainer。恢复依赖 NAS 的 Docker、存储池、Python 和 Cron 仍然可用；如果 Portainer 也无法打开，先看下方故障排查。

通过小米客户端的文件管理或 SMB 共享，在**导出应急包的那个 NAS 用户的“我的文件”根目录**创建 `ssh-emergency` 文件夹，把电脑上解压后的应急包内容复制进去。可以在 NAS 正常时就提前放好一份。

上传后应是下面的结构，程序文件应直接放在 `ssh-emergency` 中：

```text
我的文件/
└── ssh-emergency/
    ├── recover-ssh.py
    ├── restore-ssh.sh
    ├── manifest.json
    ├── recovery.pub       ← 导出时有保存公钥才会包含
    ├── README.txt
    └── SHA256SUMS
```

不要只上传 ZIP 压缩包。解压后如果多套了一层同名文件夹，要找到真正包含 `recover-ssh.py` 的那一层。

本例对应的 **NAS 实际路径**为：

```text
/nas/pool0/u123456789/data/ssh-emergency/
```

用户编号以包内 `manifest.json` 为准；使用其他存储池时，把 `pool0` 换成实际名称。共享文件夹显示的昵称与这里的用户编号可能不同。

### 第六步：从 BusyBox 执行应急程序

1. 在 **Portainer → Containers** 中找到 `ssh-rescue`，点击 **Start（启动）**。如果它一直没有停止，先停止再启动，使宿主机目录挂载与当前状态一致。
2. 按第四步打开 **Console**，命令为 `/bin/sh`、用户为 `0`。
3. 在 **BusyBox 容器终端**执行下面两条命令，先确认目录和文件。示例里的存储池、用户编号要换成自己的值：

```sh
cd /nas/nas/pool0/u123456789/data/ssh-emergency
ls
```

这里出现两层 `/nas` 是正常的：第一层是容器挂载点，第二层是 NAS 自己的存储池目录。当前路径中应能看到 `recover-ssh.py`、`manifest.json`、`README.txt` 和 `SHA256SUMS`。

4. 在同一个 **BusyBox 容器终端**中检查应急包完整性：

```sh
sha256sum -c SHA256SUMS
```

清单中的文件应全部显示 `OK`。如果出现缺失或校验失败，重新从电脑保存的原始 ZIP 解压、上传，确认无误后再继续。不要编辑程序或清单来跳过校验。

5. 确认已经有 `recovery.pub`。如果原包没有它，把自己对应私钥的 **OpenSSH 公钥文件**复制到这个目录，命名为 `recovery.pub`。保持 `manifest.json` 中的用户编号不变；私钥继续保存在电脑。
6. 在同一个 **BusyBox 容器终端**中执行恢复命令。优先核对本包 `README.txt` 中的命令；下面给出完整示例：

```sh
chroot /nas /usr/bin/python3 \
  /nas/pool0/u123456789/data/ssh-emergency/recover-ssh.py \
  --local-recover --offline
```

`chroot /nas` 之后，程序看到的是 NAS 本身的路径，所以脚本参数中恢复为一层 `/nas/pool0/...`。无需给 Python 程序添加执行权限，也无需在容器里安装 Python。**通过这条容器路线操作时保留 `--offline`**，让 NAS 自己的 Cron 完成服务激活。

### 第七步：等待激活，并检查结果

执行后，程序会输出包含下面字段的信息：

```json
{"ok": true, "state": "pending"}
```

实际输出还会包含提示和结果文件位置。`pending` 表示文件已经准备好，正在等待 NAS 的一次性 Cron 任务，**此时还不能当作 SSH 已恢复**。

先等约 1–2 分钟，在 **BusyBox 容器终端**查看结果：

```sh
cat /nas/data/minas/ssh/emergency-result.json
```

如果 NAS 还在进行开机检查，激活程序会继续等待，等待上限约 5 分钟。不要反复提交恢复命令；先读取结果文件判断当前状态。

| 结果字段 | 含义与操作 |
| --- | --- |
| `"state": "pending"` | 仍在等待 Cron 或开机检查。稍后再次读取结果文件 |
| `"ok": true, "state": "ready"` | NAS 的 SSH 服务已开启，继续下一步实际登录 |
| `"ok": false, "state": "failed"` | 恢复失败，查看同一文件中的 `error` 并保留完整错误内容 |

这个结果文件是 **NAS 自己的 `/data/minas/ssh/emergency-result.json`**，所以从容器读取时，前面只需要再加挂载点 `/nas`。宿主机接手一次性 Cron 任务后会移除任务文件，不会永久每分钟重复恢复 SSH。

### 第八步：从电脑验证 SSH，并收好救援材料

在**电脑终端**中使用第一步保存的私钥登录。以下示例适用于 Ubuntu / WSL，路径、端口和 NAS IP 均按自己的实际情况填写：

```sh
ssh -i ~/.ssh/id_ed25519 -p 22 root@192.168.31.100 id
```

成功输出 `uid=0(root)` 才说明这把私钥能够实际登录 NAS。结果文件显示 `ready` 仅表示服务已开启，不能代替这一步。

确认可以登录后，在 Portainer 中停止 `ssh-rescue`，保留它的定义、镜像和电脑上的离线备份。重新打开 SSH 恢复插件，核对公钥授权和开机修复状态；需要开机后继续自动修复时，再通过插件安装或更新开机修复。

如果 SSH 提示主机身份与过去不一致，先通过仍可访问的可信管理入口核对新的主机指纹，再更新电脑中该 NAS 的记录；不要直接忽略身份变化。

### 常见问题

| 现象 | 先检查什么 |
| --- | --- |
| Portainer 打不开 | 核对 NAS IP、实际管理端口、Docker 和 Portainer 是否运行。该路线需要有一个可用的容器管理入口 |
| 容器部署时拉取镜像失败 | 在正常联网时提前部署并保留镜像；断网时使用已导出的 ARM64 镜像，在 Portainer 中导入后再创建 |
| BusyBox 显示 `exited` | 核对是否完整使用第三步配置，特别是持续等待的 `command`；默认直接运行 `sh` 可能很快退出 |
| Console 连接失败或找不到 Bash | 使用 `/bin/sh`、用户 `0`，确认容器已经启动 |
| `chroot` 提示 `Operation not permitted` | 核对容器用户为 `0:0`，并保留 `SYS_CHROOT` 能力；rootless 或其他额外隔离策略需要另行核对 |
| 找不到 `/nas/usr/bin/python3`，或看不到 NAS 目录 | 核对挂载是宿主机 `/` → 容器 `/nas`；等待存储池挂载后停止、再启动救援容器 |
| 找不到 `recover-ssh.py` | 用 `ls` 核对目录层级、用户编号、存储池名，并确认已经解压上传。容器中的文件位置有两层 `/nas`，传给 `chroot` 后程序的路径只有一层 |
| 提示缺少用户编号或登录授权 | 保留原包 `manifest.json`，核对 `recovery.pub` 存在且是正确的 OpenSSH 公钥 |
| 超过约 7 分钟仍为 `pending` | 检查 NAS 是否仍在开机检查、宿主机 Cron 是否正常运行；保留结果文件和错误输出，进一步排查 |
| 提示路径权限异常或包含符号链接 | 保留报错的完整路径，核对固件和安装状态；不要全盘修改权限或关闭脚本中的检查 |
| 显示 `ready` 仍登录失败 | 检查 NAS IP、SSH 端口、公私钥是否对应，以及 root 登录授权是否生效 |

如果 Docker、存储池、宿主机 Python 或 Cron 已不可用，或者所有容器管理入口都丢失，这套方法无法保证恢复。它依赖提前保留的管理能力，不提供隐藏账户或免认证入口。


### 应急准备检查

在“应急恢复”页点击“检查准备情况”，只读核对恢复公钥、宿主机 Python/Cron、ARM64 BusyBox 镜像、救援容器配置及运行中的 Portainer。救援容器需为 root、可写绑定 `/` 到 `/nas`、具有 `SYS_CHROOT` / `CAP_SYS_CHROOT`（或特权能力）且网络为 `none`。检查不会创建容器、拉取镜像或修改 SSH 授权；工具不可访问时显示未能检查，不假定已经准备完成。

“查看使用步骤”提供准备和恢复流程及本账号的应急命令，可点击“复制命令”；自动复制受限时会选中命令，支持电脑快捷键或手机长按复制。上次生成时间只证明插件生成过应急包，仍需自己确认文件已经保存到电脑；页面无法验证电脑备份。完整容器部署与应急使用说明仍见本 README 的离线恢复步骤。

## 注意事项

- NAS 存储池需已挂载，目标用户需在小米客户端完成初始化；远程安装需能使用 root SSH 登录。
- 只给可信管理员安装。恢复操作影响 NAS 的 root SSH 登录；安装或启用插件本身不会自动开启 SSH。
- 推荐导入公钥。需要从私钥提取公钥时，仅使用可信 HTTPS 连接和受支持格式；妥善保管自己的私钥。
- 新生成的登录私钥只允许下载一次且有时限，丢失后不能从公钥重新下载。离线应急包不包含登录私钥或 NAS 主机私钥。
- “恢复公钥已保存”和“SSH 端口已监听”都不等于已经能登录，应以实际 SSH 登录结果为准。
- 开机修复等待系统开机检查结束，并进行有限次数复查；固件若移除所需依赖或所有管理入口，无法保证恢复。
- 移除保存的恢复公钥不等于撤销已经安装的登录授权。卸载插件不会关闭现有 SSH，也不会移除已安装的登录公钥。
- 使用安装脚本卸载会取消本插件的自动恢复登记；仅在应用市场删除时，恢复服务可能将插件补回。

安装、卸载和用户检查由 `manage.sh` 内的 Shell 流程执行，与其他插件一致。公共权限组件、持久目录初始化、SSH 密钥保护及恢复登记继续调用已有 Python 组件，不再分发独立 `installer/install.py`。安装保留启用状态，源码校验或组件安装失败时回退插件文件及登记；安装本身不启用 SSH 或增加开机任务。

页面右上角“外观主题”可选择跟随系统、浅色或深色，本地记忆选择；跟随系统时自动响应系统外观变化。


## 格式化存储分区后重新安装

分区格式化可能删除插件源码，而系统分区中的应用图标、登记和恢复记录仍存在，因此看到图标不代表插件文件完整。先在小米客户端完成存储池和账号初始化，确认原账号可以打开文件，再从最新源码运行 `bash manage.sh install`，只选择失败的真实 `u数字` 账号；日志中的星号不能作为用户名输入。

新版安装器会在严格核对官方路径后，检查目标用户能否进入插件基础目录。对于 root 重建成不可进入状态的公共插件父目录，只补上必要的进入权限，不递归修改文件，不修改账号私有目录、配置、任务或 SSH 密钥，也不重新格式化硬盘。随后仍以该用户执行原厂源码校验；失败时输出 INFO 可读、源码可访问、临时目录可写和源码摘要匹配的诊断，失败安装继续回滚。

若诊断显示账号根目录或存储池不可访问，应先修复原厂账号/存储挂载；若目录可访问但摘要不同，请保留诊断结果检查真实文件变化，不要关闭源码校验。上述权限缺陷可解释部分格式化后重装失败，实际设备还需依据诊断排除其他原因。

## 在线更新

电脑端点击左下角版本号，手机端点击底部版本号打开更新弹窗；发现新版本显示 `NEW`。打开插件时按缓存策略自动检查，也可手动点击“检查更新”。点击“立即更新”并确认后，后台自动下载、校验和安装，关闭页面仍会继续。完成后关闭插件窗口再打开。

“更新代理”支持 HTTP/HTTPS，例如 `http://127.0.0.1:7890`；按 NAS 账号在八个插件间共用，仅用于检查和下载安装包，不改变其他服务代理。认证用户名、密码分别填写，密码不会回传页面。修改后保存，启用代理时失败不会自动直连。

更新保留配置、任务、启用状态、已更新内核和 SSH 材料，验证失败自动回退。有下载、传输或关键任务时请稍后更新。首次使用需通过原安装器安装一次含在线更新组件的版本。签名发布与恢复细节见 [在线更新组件说明](installer/updater/README.md)。
