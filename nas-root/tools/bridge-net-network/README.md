# bridge-net 通用出站网络服务

为 Docker 中名为 `bridge-net` 的自定义桥接网络恢复 IPv4 出站 NAT 和转发规则，与 BitComet 和其他应用无绑定。

- NAS 脚本：`/data/bridge-net/apply.py`，root 所有，0600。
- 开机单元：`/etc/systemd/system/bridge-net.service`。
- 开机及 Docker 服务重新启动后执行；不重启任何容器。
- 自动读取网段和桥接网卡名，重复运行不会添加重复规则。
- 仅处理该网络的 RFC1918 IPv4 网段；不处理默认 `bridge`，不改变任何端口映射，不自动创建网络。
- 规则注释为 `minas-bridge-net`。网络被重建时，先添加新规则，再清除本服务自己标记的旧规则。其他防火墙规则不删除。
- 网络不存在时正常跳过；若之后新建网络，手动运行 `systemctl restart bridge-net.service`。
- 此服务不负责公网入站映射、IPv6、容器启动或存储池挂载等待。

查看状态：`systemctl status bridge-net.service`。
重新应用：`systemctl restart bridge-net.service`。

不要只删除脚本：会造成开机服务失败。停用应先禁用单元；已经加载的规则不会因停用自动消失。
