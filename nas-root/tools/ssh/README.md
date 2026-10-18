# 小米智能存储 SSH 开启与开机恢复

版本 **2.1.2**。[仓库](https://github.com/daixu0502/xiaomi-nas-plugin/tree/main/nas-root)。开启入口以 Scruel 的 [minas-enable-ssh_wsl.sh](https://github.com/scruel/xiaomi-storage-minas-tool/blob/main/minas-enable-ssh_wsl.sh) 为基础，保持上游步骤顺序，不再将已有 SSH 或 Portainer 作为默认前提。仅在你自己的设备、已登录管理员客户端的情况下使用。下列命令从仓库的 `nas-root/` 目录执行。

## 尚未开启 SSH：使用开启脚本

先登录小米 Windows 客户端，确保 `%LOCALAPPDATA%/minasCert` 中有当前账户的证书。在 WSL Ubuntu 中运行：

```bash
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100
```

也可不填 IP，先按上游方法自动查找，找不到时手动输入。客户端通过证书取得当前账户 WebDAV 凭证，再上传临时开启脚本、触发执行，最后验证 SSH；**验证前不依赖已有 SSH**。

可选参数只保留 `-v`、`--identity 私钥路径`、`--cert-dir WSL证书目录` 和 `--help`。无需 `--user`，账户由客户端证书对应的 WebDAV 凭证决定；证书目录应只有一对匹配证书。多账户请先在小米客户端切换到要使用的账户。

```bash
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 \
  --identity /home/你的WSL用户/.ssh/登录私钥 \
  --cert-dir /mnt/c/Users/Windows用户名/AppData/Local/minasCert
```

默认复用电脑 `~/.ssh/id_ed25519`；缺失时才生成新密钥，只有公钥而缺少私钥时停止，避免误覆盖。登录私钥不会上传 NAS。原有登录公钥保留，Docker PATH 沿用上游的追加方式。

上传出现 HTTP 451 时，不能直接判断固件封堵：原厂 WebDAV 也用该状态表示用户存储目录不可用。2.1.2 的认证配置输出已修正为 `jq -Rrs`，避免额外 JSON 引号导致 curl 不发送用户名；增加真实 curl 对本地模拟服务的认证回归测试。获取 WebDAV 凭证成功，不代表后续上传请求已携带认证。

### 新旧固件兼容

- 旧固件：按上游逻辑准备 `/home/rootx`、公钥和 Dropbear socket，并保留旧 `/root` 的登录授权。
- mi-shell 固件：在 Dropbear 服务私有挂载空间绑定原有 Bash，保留原厂 mi-shell 文件；不覆盖不同的同名自定义配置。
- 只有 SSH 返回真实命令标记和 UID 0，才继续安装开机修复。mi-shell 的欢迎信息或退出码 0 不视为成功。
- HTTP 上传成功或触发返回成功都不代表命令已执行。无可用 SSH 且上游触发失败时，会止于 SSH 验证，**不会自动切换其他提权入口**。

**已知限制：缺少本次执行回执。** 当前只验证 SSH 是否能执行 root 命令，不能区分 SSH 是本次开启的还是原本已开启。如果原本已有可用 SSH，即使上传脚本没有成功触发，仍可能继续安装开机修复并提示成功。因此不能将此提示作为首次开启成功的依据；当前版本尚未修复该误判，也未完成真实首次开启验证。

这里的兼容指取得执行机会后的 SSH 配置适配，不承诺绕过所有新固件的首次开启限制。请提前保留 SSH 恢复插件或其他可信管理入口。

### 开启后自动安装同源开机修复

验证 SSH 可用后，脚本通过该 SSH 连接传输同源 `restore-ssh.sh`，调用 `--install-boot-only`，不再次导入或替换公钥。根目录单文件入口已内嵌此 worker，无需在线下载其他执行代码。

开机修复与 SSH 恢复插件、独立安装脚本完全同源：等待小米开机检查实际结束（最多 300 秒），修复缺失的登录授权和 mi-shell 环境，再每 5 秒有限复查 12 次，随后退出。安装不重启 NAS，也不修改 RPMB 或 root 密码。

初次开启成功但开机修复安装/校验失败时，会分别提示，不能据此认为重启后一定可用。可稍后使用下面的独立安装入口，或插件内的“安装 / 更新开机修复”。

## 已有可执行真实 root 命令的 SSH

无需再次走证书引导，直接使用保留的独立修复安装器：

```bash
bash install-ssh-autostart-fix.sh 192.168.31.100
# 指定现有私钥：
bash install-ssh-autostart-fix.sh 192.168.31.100 --identity /路径/登录私钥
```

该入口保留原有修复能力，不新增公钥。它与插件、开启脚本共享同一 worker，但不再从开启脚本的源码派生，避免首次开启重新依赖已有 SSH。

## 依赖、检查和数据位置

```bash
sudo apt update
sudo apt install openssh-client curl jq openssl coreutils dnsutils iproute2
```

自动读取 Windows 证书目录还需 WSL 的 `cmd.exe` 和 `wslpath`；可用 `--cert-dir` 手动指定，支持中文路径。NAS 使用原有 Shell、systemd、Cron、Dropbear、flock 等工具；开启和开机恢复均为纯 Shell。

```bash
ssh root@192.168.31.100 id
ssh root@192.168.31.100 '/bin/sh /data/ssh-autostart-fix/restore-ssh.sh --check'
ssh root@192.168.31.100 'journalctl -b -t ssh-autostart-fix --no-pager'
```

- `/home/rootx/.ssh/authorized_keys`：NAS root 登录公钥，不覆盖现有授权。
- `/data/ssh-autostart-fix/restore-ssh.sh`：同源开机 worker；`login-authorized-keys` 是安装时授权快照，仅在文件缺失时恢复；已有空文件也不会被旧快照覆盖。
- `/data/ssh-autostart-fix/host/dropbear_rsa_host_key`：主机身份恢复材料；已有主机密钥不轮换。
- `/etc/systemd/system/dropbear@.service.d/90-normal-shell.conf`：mi-shell 兼容配置。
- `/etc/systemd/system/minas.boot_check.service.d/90-keep-ssh.conf`、`/etc/cron.d/keep-ssh`：开机检查后的 SSH 恢复及有限复查。

客户端临时目录使用 0700 权限，WebDAV 密码不打印、不进入 curl 参数；成功或失败退出时清理临时文件并尝试删除本次 NAS 上传脚本。不再创建持久 `SUCCESS` 测试文件，不写入参与校验的插件源码目录。电脑私钥需自行妥善备份；更换或撤销登录公钥后，应重新安装开机修复以刷新授权快照。

## 源码与验证

```bash
python3 tools/ssh/build.py
python3 -B -m unittest discover -s tools/ssh/tests -p 'test_*.py'
```

`src/open-ssh-client.sh` 保留上游式首次开启流程；`src/install-ssh-client.sh` 保留独立修复安装器的既有流程；`src/restore-ssh.sh` 与插件 `sshrecovery/installer/common/modules/sshrecovery/restore-ssh.sh` 保持逐字节一致。构建生成 `nas-root/` 下两个单文件入口；构建环境所用语言不影响 NAS 运行依赖。公共权限组件的 SSH 状态和操作锁使用 `/data/.minas-privileged/modules/sshrecovery/var/`。

自动化测试模拟证书、WebDAV、SSH、旧固件、新 mi-shell 固件和开机检查，不会在真实 NAS 上关闭 SSH、重置固件或重启设备。脚本测试通过不代替真实首次开启与重启验证。
