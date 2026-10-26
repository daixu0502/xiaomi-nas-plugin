# 自定义持久目录

自定义组件和独立工具使用真实父目录 `/data/minas/`，由 root 拥有、0755。敏感子目录保持 0700，插件的 `etc/var` 保持各 NAS 用户拥有、0700。

| 位置 | 用途 |
| --- | --- |
| `privileged/` | 公共权限入口、模块、授权、任务和跨插件锁 |
| `plugins/data/<用户>/<插件>/` | 八插件的持久配置与状态 |
| `plugins/data/<用户>/mediaeditor/protected/` | root 保护的影视修改计划、参考与数据库备份；与用户 etc/var 平级 |
| `privileged/modules/mediaeditor/var/` | 跨账号影视编辑全局写锁，root 私有 |
| `plugins/recovery/` | 独立恢复程序、登记和当前恢复快照 |
| `ssh/` | 独立 SSH 开机修复、应急程序及登录材料 |
| `docker/network/` | bridge/IPv6 事件维护、nft 运行库与网络事务 |
| `docker/runtime/` | Docker 29.9.2 程序、私有配置与升级事务 |
| `backups/` | Docker、权限组件及插件卸载备份 |

原厂 `/data/docker`、`docker_data`、`plugin`、系统服务文件、官方插件源码和缓存保留原位。`/run` 和下载中心内存暂存保持现有策略。


安装包直接初始化新目录，不再探测、搬移或清理旧布局。已安装账号的 etc/var 使用固定持久链接；protected 保存 root 专用的影视计划、参考和备份。运行缓存继续使用官方 tmp。

旧目录、历史测试和部署备份统一交给[检查清理卸载工具](cleanup-and-uninstall.md)处理。该工具区分现用与卸载后保留数据，并保留 Docker 升级备份及恢复所需事务。

各插件不再携带独立 installer/data_layout.py。Shell 安装入口和 SSH Python 安装器复用既有恢复组件中的初始化函数，先建立并校验公共根目录，再创建账号的持久链接。共有函数仍由 workspace-tools/data-layout/scoped_common.py 统一维护并在构建时嵌入，避免复制维护。
