# Docker 容器网络及 IPv6 出口修复

使用单文件入口 [fix-docker-bridge.sh](../fix-docker-bridge.sh)，在小米 NAS 的真实 root SSH 终端执行。针对原厂 `mi_docker` → `dockerd_prepare.sh` 配置流程；IPv4 使用 Docker 原生 NAT，IPv6 使用独立 nftables 表 `ip6 minas_docker_ipv6` 提供 NAT66 及转发入站隔离。

## 检查和首次修复

```sh
sh /path/to/fix-docker-bridge.sh --check
sh /path/to/fix-docker-bridge.sh --check --ipv6

# 以下会暂停容器并重启 Docker，按需选择一个
sh /path/to/fix-docker-bridge.sh --apply
sh /path/to/fix-docker-bridge.sh --apply --ipv6
```

修复会输出事务编号并启用 10 分钟自动回退。验证容器出网、DNS、端口和业务连接后，在期限内确认：

```sh
sh /path/to/fix-docker-bridge.sh --confirm <事务编号>
# 尚未确认时可手动回退
python3 -B /data/minas/docker/network/repair.py --rollback <事务编号>
```

这里的一次性安全回退与 IPv6 日常维护不同；后者没有周期定时器。IPv4 修复不要求运行 IPv6 服务。IPv6 NAT66 只提供出口，不自动开放公网入站端口，也不会重建已有 IPv4-only 网络。

## 已启用 IPv6 的设备升级维护方式

```sh
sh /path/to/fix-docker-bridge.sh --install-ipv6-events
sh /path/to/fix-docker-bridge.sh --check --ipv6
```

此操作不重启 Docker 或容器，不改 UCI 和容器网络定义，保留运行库和历史事务；会替换 IPv6 维护服务、禁用并删除旧 `minas-docker-ipv6.timer`。新安装通过 `--apply --ipv6` 直接使用新方式。

`minas-docker-ipv6.service` 使用 `repair.py --watch-ipv6`：

- 随 Docker 开机启动、停止和重启，启动时同步一次。
- 订阅 Docker 网络和 daemon 事件、nftables 专用表变化、内核 Netlink 链路及 IPv6 地址/路由事件；不依赖 BusyBox `ip monitor`。
- 相关事件短暂合并后同步，空闲时无限期阻塞等待；不保留每 30 秒、每 10 分钟或其他周期兜底。
- 监听断开或事件溢出时保留最后有效规则，systemd 在进程故障后重启监听并重新同步。故障重连不是周期网络检查。
- 与 Docker 升级及网络修复共用维护锁；锁被占用时等待释放，避免并发修改规则。

没有事件覆盖的外部手动 sysctl 修改不会靠周期检查发现，需手动执行 `--sync-ipv6` 或重启 IPv6 维护服务。普通流量计数变化不会触发规则重写；服务不扫描用户文件或访问容器卷。

## 运行文件与检查

```sh
systemctl status minas-docker-ipv6.service --no-pager
cat /run/minas-docker-ipv6/status.json
journalctl -u minas-docker-ipv6.service --no-pager
```

`status.json` 位于内存，仅在启动/事件引发同步后更新；它不是心跳。程序和库在 `/data/minas/docker/network/repair.py`、`nft-runtime/`，规则摘要为 `ipv6-state.json`。这些仍在使用，不能把整个目录当缓存删除。事务备份位于 `transactions/`，不要删除待确认的事务。

Docker 升级入口 [upgrade-docker-29.9.2.sh](../upgrade-docker-29.9.2.sh) 的 IPv6 预检也使用事件服务，不再依赖旧定时器。

## 已完成的验证

2026-10-23：41 项网络修复/事件测试和 34 项 Docker 安装回退测试通过。NAS 上已验证临时内部网络创建/删除、临时本机 IPv6 地址变化、专用表内无效果计数规则变更均触发同步；测试网络、地址和规则均清理。监听子进程中断后会自动重新订阅并首次同步，连续 35 秒无事件时同步次数和状态文件均不变。部署及验证期间 Docker 主进程、原容器运行集合均保持不变，IPv4/IPv6 健康检查通过。

Docker 配置生成在事件服务尚未启动时也已真机验证通过，避免 `After=docker.service` 与 Docker 自身启动预检形成循环依赖。未通过重启整台 NAS 或重启生产 Docker 验证完整启动链。

参考：[Docker 事件接口](https://docs.docker.com/reference/cli/docker/system/events/)、[nftables monitor](https://netfilter.org/projects/nftables/manpage.html)。
