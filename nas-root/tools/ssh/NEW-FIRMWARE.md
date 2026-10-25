# 1.0.9.462 新 root 入口分析与 SSH 改写

分析日期：2026-10-19。上游：[lvmaple/minas_plugin/xiaomi-nas-root-tool-1.0.9.462](https://github.com/lvmaple/minas_plugin/tree/main/xiaomi-nas-root-tool-1.0.9.462)。审查时 `xiaomi-nas-root.py` Git blob 为 `95ee691e3d61ca8c57f1ac126b3418037bd432f0`。同时核对上游 README、USAGE、技术复盘及本地原厂 `NasSamba.lua`、`smb_mgr.sh`、systemd 配置。

## 上游链路

管理员客户端 mTLS 证书取得本人 WebDAV 凭据；通过 MKCOL 创建含换行和引号的目录，再调用 `netshare/add_samba_account`。原厂 `smb_mgr.sh` 将路径拼入 UCI batch 的 heredoc；换行能够注入额外 UCI 指令，将本次共享改为 `force user=root` 的 `/etc` 可写共享。

上游随后写 `/etc/ld.so.preload` 和 AArch64 `.so`，由动态加载器在系统任务运行时触发 root 脚本，开启 socat 无认证 shell 和 Dropbear。它验证端口开放，没有解决当前固件 Dropbear 固定调用 `/usr/sbin/mi-shell` 的行为。

上游明确声明没有在纯原厂设备端到端重放完整流程。该入口与旧视频 404 注入不同，不能用旧入口已修复的结论直接否定；也不能将上游的分步骤结果扩大为本改写已经真机首次开启成功。

## 发现的问题与处理

| 问题 | 2.2.0 的处理 |
| --- | --- |
| 固定 `_2`，但原厂实际先用 1、有 1 时选 2；已有 2 也可能被碰触 | 读取账户列表、按规则选空闲位、拒绝满槽和同名账户；写入前再次核对 |
| `set_samba_account` 将目录改为 `/etc` 后，原厂 `_set_samba` 也把共享名改成 `etc-UID`，上游仍连接旧随机目录名前缀共享 | SMB 始终连接修复后的 `etc-UID` |
| 多证书随机选第一份、UID 可由命令行任意覆盖 | 要求唯一一对证书，账户取自认证后的 WebDAV 响应 |
| 缺失业务 `code` 可被当成成功，网络/解析失败后继续 | 关键变更必须有明确成功码；仅添加共享的 2502 允许进入账户核对，再执行路径修复 |
| 全局 preload 使用固定文件名，可能覆盖既有 preload，且引发大量系统进程加载；子进程可能递归触发 | 直接经 root `/etc` 共享写一次性 Cron；取消 preload、共享库和汇编依赖 |
| socat 无认证端口；端口开放不等于本次 root 执行成功 | 仅使用公钥 SSH，并验证本次随机回执、命令标记和 UID 0 |
| 上游称 SSH exec 异常，但未处理固件固定 mi-shell | 沿用当前 NAS 的 Dropbear 私有 Bash bind；只对新连接生效，保留原厂文件和其他连接 |
| 固定密钥注释判断会漏装更换后的公钥；已有 `.pub` 可能过期 | 从实际私钥导出公钥，按完整行去重合并；保留原有授权和主机身份 |
| 错误消息/SMB 挂载参数可能带明文密码 | 使用本地受限认证文件和标准输入，不把密码放到进程参数或输出 |
| 清理仅在后半段注册，且依赖尚未取得的 root shell；模糊 sed 删除账号可误删 | 从客户端建立临时目录后注册 EXIT/信号处理，SMB 先撤任务，API 再只删匹配本次随机密码的账户；异常保留明确恢复指引 |
| 默认自动安装持久化，不符合本次交互要求 | SSH 与清理均确认后询问；同意才调用同目录安装器，失败显示放置路径及重试命令 |

## 2.2.1 后续完善

- 区分客户端 TLS 证书与 SSH 登录密钥，显示复用或新生成密钥，拒绝失效私钥链接；`-v` 提供阶段与公钥指纹诊断。
- 拒绝重复 SMB 槽位；新共享须同时匹配本次槽位、账户、目录和随机密码，才能继续修改。
- NAS 任务在其他写入完成后才发布回执；终止信号转为失败回执，Cron 超时附带强制终止宽限。客户端读取失败回执后提前结束等待。
- 异常清理先撤销任务和载荷，再等待可能仍在执行的任务退出，最后清理回执和共享。共享撤销后的 WebDAV 重试不再依赖 SMB；目录创建结果不明时保留恢复材料。
- SSH 主机密钥校验失败时直接提示核对，不删除 `known_hosts`。拒绝带前导零的歧义 IPv4。

以上通过本地隔离回归核对，验证边界仍如下所述。

## 2.2.2：issue #3 真机复现与修复

2026-10-24 在现有 NAS 经 WSL 分步骤复现，载荷仅写随机回执，不修改 SSH 配置或重启 SSH 服务：

- 旧版上传出的文件为 root 所有、`0764`，cronie 1.7.2 报 `BAD FILE MODE`。原厂全局 `force create mode=0760` 在 `create mask` 之后按位或入权限，因此仅指定 `create_mask=0644` 无效。
- 在本次临时共享内覆盖 `force create mode=0644`，并关闭 archive 到执行位映射；原厂共享生成器读取 UCI 的多行 `create_mask` 值生成这些参数。使用真实换行，避免 WebDAV 拒绝反斜线；本机实际目录名 235 字节，小于 255 字节上限。
- 上传载荷与任务均为 `0644`，原厂 Cron 自动发现并执行。随机回执与 UID 0 验证通过。
- `smbclient allinfo` 对缺失文件输出 `NT_STATUS_OBJECT_NAME_NOT_FOUND`，但退出码为 0。旧版误判文件仍存在；新版始终检查明确的缺失状态，断网和认证失败不能当作已删除。
- 修正恢复说明的原厂命令为 `nas call smb_mgr.sh del_account one <账户> <槽位>`。超时消息区分无回执与任务已完成但 SSH 验证未通过。
- 成功与失败回执两种路径均自动清理，测试前后 SSH 文件、主机密钥、授权、profile、passwd/shadow 和 Samba UCI 配置一致；SSH socket 保持 active。测试共享、任务及临时目录已经撤销。

## 真机核对与验证边界

当前 NAS 的原有 root SSH 返回 UID 0；`crond` 为 **cronie 1.7.2**，服务处于 active。原厂 Dropbear 是 socket 激活的 `dropbear@.service`；当前 `90-normal-shell.conf` 内容是：

```ini
[Service]
BindReadOnlyPaths=/usr/bin/bash.bash:/usr/sbin/mi-shell
```

本改写沿用该配置。最初的配置核对只读；2.2.2 的真机验证进一步覆盖了上文所列的临时共享和 Cron 链路，未修改 SSH 配置或安装开机恢复。隔离回归覆盖成功、mi-shell/旧回执/非 root 误报、已有共享保留、上传内容损坏、清理断网、安装确认和安装器失败，以及模拟文件树内的授权合并、主机密钥保留、配置冲突及符号链接拒绝。

待真机验证的部分包括：纯原厂设备从 UCI 注入到首次 SSH 成功的完整串联，以及用户选择安装后的真实重启恢复。现有 NAS 上的共享、权限和 Cron 验证不能替代这些验收。
