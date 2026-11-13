# 检查、清理、卸载与恢复

`migrate-minas-data.sh` 已替换为检查清理卸载工具。文件名保留以兼容下载地址，不再执行迁移。安装包和脚本直接使用 `/data/minas/` 新布局，不探测、不搬移旧目录。旧目录只由这个管理工具识别。

在 NAS root 终端运行：

```sh
sh migrate-minas-data.sh
sh migrate-minas-data.sh --scan
sh migrate-minas-data.sh --scan --json
```

默认只检查。交互菜单可刷新检查、按 ID 多选删除、清理确认无引用的历史遗留、按账号卸载插件，以及撤销 SSH 开机恢复、Docker 网络修复和 Docker 升级。

## 目录识别与删除

列表分别显示归属、旧／新布局、路径、使用状态、保留原因和选择 ID。检查安装登记、插件链接、systemd/Cron/sudoers 引用、进程命令、文件句柄、内存映射与挂载点。原厂启用标记与实际后台状态分开显示；Mihomo、快传、下载中心读取 systemd 的实际状态，其余标注为页面/API 工具。原厂登记未启用不等于后台已停止，也不等于已卸载。没有发现引用的历史路径才可清理；当前恢复副本、现用持久数据及 Docker 升级备份受保护。

```sh
sh migrate-minas-data.sh --delete 选择ID1 选择ID2
sh migrate-minas-data.sh --clean-residue
```

删除前展示具体路径并要求输入 `YES`；非交互执行须明确附加 `--yes`。清理遗留仅处理已知的部署备份、过期恢复代、迅雷安装触发暂存、测试暂存和完成的迁移事务。旧版本卸载遗留的配置／任务须手动选中才删除；新统一卸载在提交后直接删除该插件自有数据。不会扫描或删除用户媒体、下载目录、容器数据或官方迅雷插件。

清理取得安装器和公共组件锁，删除前重新核验目录身份及使用状态。逐层使用目录 FD，拒绝链接跳转、目录替换、挂载点及跨文件系统进入；用户缓存内容先降权再访问。无法证明归属的系统路径不纳入自动清理。

## 卸载八个自定义插件

```sh
sh migrate-minas-data.sh --uninstall mediaeditor --user u103868178
```

插件 ID：`dockermanager`、`mihomo`、`taskcenter`、`nascenter`、`sshrecovery`、`quickshare`、`downloadcenter`、`mediaeditor`。卸载复用同版本安装器的正式流程，取消恢复登记、停止对应服务并撤销该账号授权；保留其他账号和官方插件。下载内容和容器保留；插件配置、任务、断点及专属恢复材料在卸载提交后彻底删除。

## 撤销三个脚本

```sh
sh migrate-minas-data.sh --restore ssh
sh migrate-minas-data.sh --restore network --transaction 事务编号
sh migrate-minas-data.sh --restore docker --transaction 事务编号
```

SSH 恢复撤销本工具的开机 Cron、启动挂钩和 shell 绑定；存在原始 passwd 备份时仅恢复 root 账户行。现用公钥、主机密钥和当前 SSH 连接保留。早期安装没有记录完整 SSH 初始状态，工具不猜测或删除登录材料，也不声称能还原未记录的初始 socket 启用状态。

网络恢复支持撤销已确认的修复，使用原事务恢复 UCI、转发策略、辅助规则和 IPv6 维护服务。Docker 回退复用升级脚本的备份完整性和兼容性检查，恢复升级前镜像及容器层，默认保留当前本地卷。两项会短暂停止容器并重启 Docker，执行前有独立确认；不能把它们作为普通缓存清理。

Docker 升级备份和网络／升级恢复所需事务不会被遗留清理删除。撤销成功后残留运行目录可重新扫描并手动选中清理。

恢复策略：插件遭系统清理后按原有启用意图重新登记，不强制启用。当前登记和独立服务状态分开显示；业务服务或任务调度的手动停止意图继续保留。
