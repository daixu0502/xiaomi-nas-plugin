# Docker bridge 网络出口修复

适用于使用 `/etc/config/mi_docker` → `/usr/sbin/dockerd_prepare.sh` → `/tmp/dockerd/daemon.json` 的小米智能存储固件；已在 Docker **20.10.17** 上检查。不是通用 Linux／小米路由器 Docker 安装器，也不安装 Docker 或获取 root。

IPv4 出口异常来自 `mi_docker.globals.iptables=0`：Docker 不生成 bridge 所需 NAT、转发及端口映射规则。IPv4 方案仍然恢复 **Docker 原生 iptables 管理**，覆盖默认 `bridge` 和普通自定义 bridge；跳过明确设置 `internal` 或关闭 masquerade 的网络。IPv6 可通过 `--ipv6` 显式启用：保留原厂 UCI 生成流程，为默认 bridge 分配 ULA /64，使用独立 nftables 表提供 NAT66 出口和入站隔离。NAS 内核缺少传统 `ip6_tables` 时无需更换内核，也不关闭应用的 IPv6。不负责代理、运营商或上游路由故障。

在 **NAS 的真实 root 命令行**执行（不是 WSL，不是容器内，不是受限 mi-shell）：

```sh
# 下载到一个新的私有临时目录；先检查，再明确修复。
work=$(mktemp -d /tmp/minas-network.XXXXXXXX) && chmod 700 "$work"
curl -fL https://raw.githubusercontent.com/daixu0502/xiaomi-nas-plugin/main/scripts/fix-docker-bridge.sh -o "$work/fix-docker-bridge.sh"
sh "$work/fix-docker-bridge.sh" --check
sh "$work/fix-docker-bridge.sh" --apply
```

脚本也可从仓库下载后单文件复制到 NAS，无需 Python 包或其他旁边文件。下载失败时不要继续执行；建议先审阅源码。`--check`（不传参数时也是检查）完全只读：退出码 **0** 表示基础规则正常，**2** 表示需要处理，**1** 表示检查失败。检查不拉取镜像、不创建容器、不执行用户容器内命令、不重启 Docker。结构检查通过不等于互联网、DNS 或业务连通性已通过。

`--apply` 会要求输入 `APPLY`；自动化执行须显式加 `--yes`。**会短暂停止原运行容器并重启 Docker**，应在维护窗口执行。它会：

1. 确认真正的存储池已挂载、没有其他待确认事务或未提交 UCI 改动。
2. 在 root 专用事务目录记录原 UCI、运行容器 ID、防火墙规则和旧辅助服务状态，先启用 **10 分钟自动回退**。
3. 暂停旧 `bridge-net.service`（如果存在），只把 `mi_docker.globals.iptables` 改为 `1`，由原厂启动流程重建 Docker 配置。
4. 恢复原运行容器，包括原本 `--restart no` 的容器；不改容器定义、卷、代理、restart policy 或 Docker 数据目录。IPv4 模式不改变网络定义；IPv6 模式为默认 bridge 增加 IPv6 子网。
5. **仅删除注释严格等于 `minas-bridge-net` 的旧辅助规则**，不清空防火墙、不删除未标记规则，不直接删除历史脚本或服务文件。
6. 校验原生 IPv4 MASQUERADE、Docker 转发链入口、IPv4 转发开关与原运行容器状态。失败时尝试恢复原配置；正常时等待用户确认。

容器出网、DNS、映射端口、容器互通、Openlist 对下载器的连接都验证正常后，**10 分钟内**用输出的事务编号确认：

```sh
sh /path/to/fix-docker-bridge.sh --confirm <事务编号>
# 或不依赖最初下载位置：
python3 -B /data/minas/docker/network/repair.py --confirm <事务编号>

# 尚未确认时也可以立即手动回退：
python3 -B /data/minas/docker/network/repair.py --rollback <事务编号>
```

确认只取消本事务的回退，不替代业务连通性验证。未确认会自动回退并再次短暂重启 Docker；已确认／已回退事务不会被过期定时器再次回退。不要在待确认期间重启 NAS、编辑 Docker/UCI 或删除状态目录；systemd 临时回退定时器**不跨 NAS 重启保存**。存储池消失时拒绝启动原容器，应挂载恢复后手动回退。事务会记录原防火墙快照供排查，但回退仅恢复原 UCI 选项、转发开关、表策略及本工具识别的旧辅助规则，不把整个防火墙历史快照覆盖回去。

IPv4 原生方案通过原厂 Docker 启动流程持续生效，无需新增常驻 NAT 服务。可选 IPv6 方案使用纯事件服务恢复规则，不保留周期轮询。固件重置 UCI 后仍需重新检查；NAS 整机重启后的恢复需另行实际验证。当前已正常的设备重复执行相同参数的 `--apply` 不重启服务、不建立事务。

### 工具数据与日志

- `/data/minas/docker/network/repair.py`：定时回退所需的独立程序；可选 IPv6 模式的规则恢复服务也使用同一程序。
- `/data/minas/docker/network/transactions/<事务编号>/`：原始 `mi_docker.original` 和 `state.json`，目录 0700、文件 0600。包含本机路径、网络及容器信息，不应公开上传。
- `journalctl -u 'minas-docker-bridge-rollback-*' --no-pager`：自动回退过程；脚本检查结果直接输出到当前终端。
- 备份只用于尚未确认事务的回退。已确认／回退并再次验证正常后，可由 root 删除**对应的单个事务目录**；不要删除仍 pending 的事务或与 SSH 密钥有关的其他目录。

## IPv6 双栈修复

NAS 自身必须已有可用的 IPv6 默认路由和公网连接。通过同一个入口检查并启用：

```sh
sh fix-docker-bridge.sh --check --ipv6
sh fix-docker-bridge.sh --apply --ipv6
# 非交互维护：--apply --ipv6 --yes
# 需要另选不重叠的 ULA /64：--ipv6-subnet fd12:3456:789a:1::/64
```

IPv6 模式同样使用 10 分钟确认和自动回退。它只持久修改 `mi_docker.globals.ipv6=1`、`fixed_cidr_v6` 及原有 IPv4 `iptables` 选项，不直接编辑原厂 `dockerd_prepare.sh` 或临时 daemon.json。默认子网为 `fd6d:696e:6173:1::/64`，子网冲突会在停止容器前拒绝。

默认 bridge 下原有和新建容器启动后会自动获取 IPv6，原 `--network bridge` 可继续使用。host 模式共享 NAS 的 IPv6。自定义 bridge 在创建时显式开启，例如：

```sh
/data/docker/docker network create --ipv6 --subnet fd6d:696e:6173:2::/64 apps-v6
```

已有 IPv4-only 自定义网络不会被强制重建，必须按业务计划迁移；`internal`、明确关闭 masquerade 的路由网络继续保持原意图。事件服务随 Docker 启动、停止和重启；订阅 Docker 网络及 daemon 事件、nftables 专用表变化、原生 Netlink IPv6 地址/路由及链路事件。启动后首次同步，后续仅在相关事件发生时同步，短时间内的事件合并处理；空闲时无限期阻塞，不执行每 30 秒或每 10 分钟的周期检查。普通链路、IPv6 地址和路由通知（包括 RA 有效期续期）仅从 /proc 读取内核路由并按需恢复转发和 RA 设置，不重新启动 Docker/nft/ip 命令或读取持久配置；Docker 网络、专用防火墙表事件仍执行完整同步。Docker 或监听连接异常时保留现有规则，由 systemd 仅在进程故障后重新启动并订阅。

已启用旧版 IPv6 修复的 NAS 可直接升级维护方式，不重启 Docker 或容器：

```sh
sh /path/to/fix-docker-bridge.sh --install-ipv6-events
sh /path/to/fix-docker-bridge.sh --check --ipv6
```

升级会移除旧 `minas-docker-ipv6.timer`，保留 UCI、网络、原有事务和 nft 运行库。新安装通过 `--apply --ipv6` 自动使用事件服务。运行状态保存在内存目录 `/run/minas-docker-ipv6/status.json`；只有实际同步后才更新，不是定时心跳。监听断开后重新订阅会再做一次完整同步，期间不清空原规则。若 Docker 升级或网络修复正在持有维护锁，事件同步会等待该锁释放，不轮询锁文件。

NAS 缺少 `nft` 时，首次应用会从 Debian 官方仓库下载固定版本的 ARM64 nftables 及依赖，逐包核对内嵌 SHA-256，解压到 `/data/minas/docker/network/nft-runtime/`，保留其许可证；不执行包安装脚本、不覆盖系统库。纯 IPv4 修复和只读检查不下载这些依赖。自动准备运行库需要 Python 3.12 或更新版本及 HTTPS 外网访问；下载或校验失败会在更改 Docker 配置之前停止。

IPv6 规则仅使用 `ip6 minas_docker_ipv6` 表：给符合条件的 bridge 子网提供 NAT66、允许已建立连接和 ICMPv6，隔离未请求的转发入站及跨 bridge 连接。不添加入站端口，不将 Web 管理界面自动开放到公网。原来绑定 `10.31.0.66` 的 `-p` 仍只绑定 IPv4；需要 IPv6 入站时显式添加对应映射，例如 `-p '[::]:6881:6881/tcp'` 和 `-p '[::]:6881:6881/udp'`。NAT66 出口不表示每个容器拥有公网 IPv6 地址。

新增受保护运行材料：

- `ipv6-state.json`：规则摘要；计数器变化不触发重写，规则或网络改变时原子更新。
- `nft-runtime/`：独立工具与许可证，仅本机保存。
- `/etc/systemd/system/minas-docker-ipv6.service`：调用同一 `repair.py --watch-ipv6` 持续等待事件，无 `.timer`；首次启用前的服务、规则和转发/RA 设置写入事务备份。

回退只恢复本事务涉及的 UCI 项、IPv6 专用表、服务和转发/RA 设置；不会覆盖其他 nftables 表。Docker 重启可能重新分配未固定的容器内部地址；业务应使用已发布的 NAS 端口或明确配置的稳定地址。不要把动态内部 IP 当作固定接口。

## 构建与测试

从仓库的 `scripts/` 目录执行（测试代码仅本地保存）：

```bash
python3 tools/docker-bridge/build.py
python3 -B -m unittest discover -s tools/docker-bridge/tests -p 'test_*.py'
```

源码为本工具 `src/docker_bridge_repair.py`；生成根目录单文件 `fix-docker-bridge.sh`。测试使用隔离文件与模拟命令，不修改本机或 NAS 网络。
