# 小米 NAS SSH 开启与恢复

开启脚本 **2.2.1**，运行环境为 WSL / Linux 的 Bash。新入口参考 [lvmaple 的 1.0.9.462 root 工具](https://github.com/lvmaple/minas_plugin/tree/main/xiaomi-nas-root-tool-1.0.9.462)，替换旧版已失效的视频 404 注入。仅用于自己管理且客户端已登录的 NAS。

上游声明只做过分步骤、函数级真机验证，没有在纯原厂机器重放完整首次开启。本改写完成隔离模拟回归，当前 NAS 仅做配置读取，**尚未完成纯原厂首次开启及重启验收**。当前设备已有 root SSH，不能拿它证明首次开启有效。详见 [入口分析与修复记录](NEW-FIRMWARE.md)。

## 使用

本说明对应固件 **1.0.9.462** 的 `xiaomi-storage-open-ssh-wsl-1.0.9.462.sh`。固件 **1.0.8.425** 的旧入口单独保留为 `nas-root/xiaomi-storage-open-ssh-wsl-1.0.8.425.sh`，不由当前构建重新生成；其旧流程在成功后自动安装开机恢复。请先核对固件版本再选择脚本。

登录小米 Windows 客户端，准备当前账户的 `%LOCALAPPDATA%/minasCert`。在 WSL Ubuntu 安装依赖：

```bash
sudo apt update
sudo apt install openssh-client curl jq openssl coreutils smbclient
```

从仓库根目录运行，将 IP 替换为自己的 NAS：

```bash
bash nas-root/xiaomi-storage-open-ssh-wsl-1.0.9.462.sh 192.168.31.100
# 可指定私钥和 WSL 中的证书目录；支持空格及中文路径：
bash nas-root/xiaomi-storage-open-ssh-wsl-1.0.9.462.sh 192.168.31.100 \
  --identity /home/你的WSL用户/.ssh/登录私钥 \
  --cert-dir /mnt/c/Users/Windows用户名/AppData/Local/minasCert
```

未给 IP 时手动输入。证书目录须包含 `ca_chain.pem` 和当前账户唯一一对 `*_cert.pem`、`*_private_key.pem`。默认复用 `~/.ssh/id_ed25519`，不存在时生成；仅有 `.pub` 时停止，避免覆盖。公钥从私钥重新导出，私钥不上传。

这里有两类不同的认证材料：小米客户端的 **TLS 证书**由已登录的客户端提供，本脚本只读取，不生成；**SSH 登录密钥**有复用和新生成两条路径，脚本会明确显示实际采用哪条。SSH 使用公钥认证，没有签发 SSH 证书。

```bash
# 复用已有私钥：该文件必须已存在，已有 .pub 不会被重写
bash nas-root/xiaomi-storage-open-ssh-wsl-1.0.9.462.sh 192.168.31.100 --identity ~/.ssh/id_ed25519
# 新生成一对密钥：指定一个私钥及同名 .pub 都不存在的新路径
bash nas-root/xiaomi-storage-open-ssh-wsl-1.0.9.462.sh 192.168.31.100 --identity ~/.ssh/xiaomi_nas_ed25519
```

新生成的 Ed25519 私钥不设口令，请妥善保存。已有私钥路径若是目录或失效符号链接会停止。`-v` 显示当前阶段、SSH 公钥指纹和验证结果，不输出认证密码或私钥。

脚本需要一个空闲 SMB 账户槽位，且当前 NAS 账户名没有被已有 SMB 账户占用。两个槽位都占用或同名时停止，请先在客户端自行安排；脚本不替用户删除已有共享。只接受 API 确认的单个 `/pool0/data` 内部存储池，其他布局停止。

## 流程及成功判据

1. 用客户端证书验证 TLS、取得本账户 WebDAV 凭据，读取当前 SMB 账户列表。
2. 创建本次随机前缀的换行目录，经原厂 `smb_mgr.sh` 的 UCI heredoc 注入建立临时 root `/etc` 共享。按原厂槽位分配规则选择空闲的 1 或 2，复查配置，确认新账户与本次事务匹配后才修正共享路径。
3. 通过 `smbclient` 上传纯 Shell 脚本，回读核对内容。一次性 Cron 文件先上传到 `cron.d/` 中的隐藏暂存，再在同目录改名激活。无需 CIFS 挂载、Keystone、共享库或 socat。
4. Cron 首先删除自己的任务，再保留原登录授权和主机密钥、配置普通 Bash、启动 SSH socket。对原厂 `mi-shell` 使用当前 NAS 的服务私有映射：`BindReadOnlyPaths=/usr/bin/bash.bash:/usr/sbin/mi-shell`，没有该 Bash 时检查 `/bin/bash`。不同的已有同名 drop-in 会使流程停止。
5. 客户端在约 130 秒等待窗口内反复验证（单次网络请求受独立超时限制），要求新的 SSH 连接读到**本次随机执行回执**并输出对应标记及 UID `0`。同时经 SMB 读取回执，任务明确失败时提前停止。原有 SSH、端口开放、HTTP 200、mi-shell 欢迎文字均不能代替这些判据。主机密钥校验失败时提示核实设备身份及 `known_hosts`，不自动删除主机记录。
6. 确认临时任务、上传文件和本次共享清理完毕后，显示登录命令，再询问是否安装开机 SSH 修复。

## 可选的开机修复

提示 `是否安装开机 SSH 修复？[y/N]` 时，输入 `y`、`yes` 或 `是` 才调用开启脚本**实际所在目录**的 `install-ssh-autostart-fix.sh`，传递 NAS IP 与本次私钥。回车、其他回答或输入结束均跳过。

两份单文件入口应放在同一目录：

```text
任意目录/
  xiaomi-storage-open-ssh-wsl-1.0.9.462.sh
  install-ssh-autostart-fix.sh
```

安装器缺失或调用返回失败时，开启脚本会明确显示放置目录、LF 换行要求和重试命令；此时 SSH 获取成功与开机恢复失败分别报告，退出码为非零。重试示例：

```bash
bash /实际目录/install-ssh-autostart-fix.sh 192.168.31.100 --identity /实际路径/登录私钥
```

安装器继续使用 SSH 恢复插件的同源 `restore-ssh.sh`，不新增登录公钥、不重启 NAS。开启入口不内嵌开机恢复，也不调用 `systemctl enable`。跳过安装表示本次 SSH 已验证，重启后的可用性未保证。

## 数据与失败处理

- NAS 保留 `/home/rootx/.ssh/authorized_keys` 中已有授权，并合并旧 `/root` 的授权；不覆盖原厂 `mi-shell` 二进制，不改 root 密码或 RPMB。
- `/data/minas-ssh-bootstrap/passwd.before` 是首次修改前的受保护备份，不覆盖旧备份。最终文件替换都在目标同目录暂存。
- `/etc/.minas-ssh-<随机号>.sh`、同名前缀 `.status`、`/etc/cron.d/minas-first-ssh-<随机号>` 和本次 WebDAV 目录是事务临时材料，正常成功或失败均尝试撤销。一次性任务开始执行就删除自身，不形成持续轮询任务。
- 清理只匹配本次槽位、账户名和临时随机密码，不按用户名模糊删除 `/etc/passwd` 或 `/etc/shadow`。网络错误不视为“文件不存在”。无法确认任务撤销时保留 SMB 恢复通道和本地材料。
- 任务超时为 50 秒，再给 5 秒终止宽限；回执是任务最后发布的文件。异常撤销先确认 Cron 和载荷已删除；若尚无本次完成回执，额外等待最多约 60 秒，让已启动的任务退出，再删除回执及共享。WebDAV 删除失败重试时不再连接已经撤销的 SMB 账户。
- MKCOL 请求若因网络错误无法确认结果，保留 `recovery.txt` 供核对，不据此删除可能未归本次所有的目录。已有 SMB 槽位重复，或新账户密码与本次事务不匹配时停止，不继续修改该账户。
- 自动清理不能确认完成时，返回失败并保留本机 `/tmp/minas-first-ssh.*/` 中的 `recovery.txt`、SMB 认证材料和诊断。按提示清理后删除该目录；其中可能包含凭据，不可公开。临时断网、进程被强制终止或 NAS 掉电仍可能需要人工清理。
- WebDAV/SMB 密码不输出，也不放入 curl、smbclient 或 jq 的命令行参数；本地临时目录及凭据用 0700/0600 权限。`-v` 不开启网络认证调试。

不要在开启过程中同时从其他终端改 SMB 配置。客户端复查可检测通常的配置变化，但原厂 API 没有跨请求事务锁，无法完全阻止并发竞争。

## 构建与本地验证

```bash
python3 nas-root/tools/ssh/build.py --ssh-entrypoint-only
python3 -B -m unittest discover -s nas-root/tools/ssh/tests -p 'test_*.py'
```

`src/open-ssh-client.sh` 为客户端维护源，`src/first-ssh-nas.sh` 为本次 SSH 开启载荷，构建将后者内嵌到根目录单文件入口。`src/install-ssh-client.sh` 和 `src/restore-ssh.sh` 继续生成独立安装器；worker 与 SSH 恢复插件保持逐字节一致。测试代码仅保留本地，不进入 Git 索引或安装包。
