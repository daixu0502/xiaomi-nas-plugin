# Docker bridge 网络出口修复

适用于使用 `/etc/config/mi_docker` → `/usr/sbin/dockerd_prepare.sh` → `/tmp/dockerd/daemon.json` 的小米智能存储固件；已在 Docker **20.10.17** 上检查。不是通用 Linux／小米路由器 Docker 安装器，也不安装 Docker 或获取 root。

之前的出口异常来自 `mi_docker.globals.iptables=0`：Docker 不生成 bridge 所需 NAT、转发及端口映射规则。最终采用的方案是恢复 **Docker 原生 iptables 管理**，而非继续使用 `/data/bridge-net/apply.py`。覆盖默认 `bridge` 和普通自定义 bridge；跳过明确设置 `internal` 或关闭 masquerade 的网络，不为它们擅自开启出口。不负责 IPv6、公网防火墙放行、代理或上游网络故障。

在 **NAS 的真实 root 命令行**执行（不是 WSL，不是容器内，不是受限 mi-shell）：

```sh
# 下载到一个新的私有临时目录；先检查，再明确修复。
work=$(mktemp -d /tmp/minas-network.XXXXXXXX) && chmod 700 "$work"
curl -fL https://raw.githubusercontent.com/daixu0502/xiaomi-nas-plugin/main/nas-root/fix-docker-bridge.sh -o "$work/fix-docker-bridge.sh"
sh "$work/fix-docker-bridge.sh" --check
sh "$work/fix-docker-bridge.sh" --apply
```

脚本也可从仓库下载后单文件复制到 NAS，无需 Python 包或其他旁边文件。下载失败时不要继续执行；建议先审阅源码。`--check`（不传参数时也是检查）完全只读：退出码 **0** 表示基础规则正常，**2** 表示需要处理，**1** 表示检查失败。检查不拉取镜像、不创建容器、不执行用户容器内命令、不重启 Docker。结构检查通过不等于互联网、DNS 或业务连通性已通过。

`--apply` 会要求输入 `APPLY`；自动化执行须显式加 `--yes`。**会短暂停止原运行容器并重启 Docker**，应在维护窗口执行。它会：

1. 确认真正的存储池已挂载、没有其他待确认事务或未提交 UCI 改动。
2. 在 root 专用事务目录记录原 UCI、运行容器 ID、防火墙规则和旧辅助服务状态，先启用 **10 分钟自动回退**。
3. 暂停旧 `bridge-net.service`（如果存在），只把 `mi_docker.globals.iptables` 改为 `1`，由原厂启动流程重建 Docker 配置。
4. 恢复原运行容器，包括原本 `--restart no` 的容器；不改容器定义、网络、卷、代理、restart policy 或 Docker 数据目录。
5. **仅删除注释严格等于 `minas-bridge-net` 的旧辅助规则**，不清空防火墙、不删除未标记规则，不直接删除历史脚本或服务文件。
6. 校验原生 IPv4 MASQUERADE、Docker 转发链入口、IPv4 转发开关与原运行容器状态。失败时尝试恢复原配置；正常时等待用户确认。

容器出网、DNS、映射端口、容器互通、Openlist 对下载器的连接都验证正常后，**10 分钟内**用输出的事务编号确认：

```sh
sh /path/to/fix-docker-bridge.sh --confirm <事务编号>
# 或不依赖最初下载位置：
python3 -B /data/docker-bridge-repair/repair.py --confirm <事务编号>

# 尚未确认时也可以立即手动回退：
python3 -B /data/docker-bridge-repair/repair.py --rollback <事务编号>
```

确认只取消本事务的回退，不替代业务连通性验证。未确认会自动回退并再次短暂重启 Docker；已确认／已回退事务不会被过期定时器再次回退。不要在待确认期间重启 NAS、编辑 Docker/UCI 或删除状态目录；systemd 临时回退定时器**不跨 NAS 重启保存**。存储池消失时拒绝启动原容器，应挂载恢复后手动回退。事务会记录原防火墙快照供排查，但回退仅恢复原 UCI 选项、转发开关、表策略及本工具识别的旧辅助规则，不把整个防火墙历史快照覆盖回去。

原生方案通过原厂 Docker 启动流程持续生效，无需新增常驻 NAT 服务；固件重置 UCI 或改变 Docker 规则后仍需重新检查。NAS 重启后的恢复需另行实际验证。当前已正常的设备重复执行 `--apply` 不重启服务、不建立事务。

### 工具数据与日志

- `/data/docker-bridge-repair/repair.py`：定时回退所需的独立程序；不注册开机常驻服务。
- `/data/docker-bridge-repair/transactions/<事务编号>/`：原始 `mi_docker.original` 和 `state.json`，目录 0700、文件 0600。包含本机路径、网络及容器信息，不应公开上传。
- `journalctl -u 'minas-docker-bridge-rollback-*' --no-pager`：自动回退过程；脚本检查结果直接输出到当前终端。
- 备份只用于尚未确认事务的回退。已确认／回退并再次验证正常后，可由 root 删除**对应的单个事务目录**；不要删除仍 pending 的事务或与 SSH 密钥有关的其他目录。

## 构建与测试

从仓库的 `nas-root/` 目录执行（测试代码仅本地保存）：

```bash
python3 tools/docker-bridge/build.py
python3 -B -m unittest discover -s tools/docker-bridge/tests -p 'test_*.py'
```

源码为本工具 `src/docker_bridge_repair.py`；生成根目录单文件 `fix-docker-bridge.sh`。测试使用隔离文件与模拟命令，不修改本机或 NAS 网络。
