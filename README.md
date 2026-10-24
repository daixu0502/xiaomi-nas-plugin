# 小米 NAS 插件

给小米智能存储增加七个功能，按需要安装即可。

| 想做什么 | 选这个插件 | 文件夹名称 |
| --- | --- | --- |
| 管理 Docker 容器、镜像和日志 | [Docker 管理](dockermanager/README.md) | `dockermanager` |
| 使用代理、导入订阅和切换节点 | [Mihomo](mihomo/README.md) | `mihomo` |
| 定时开关容器、执行脚本等 | [定时任务](taskcenter/README.md) | `taskcenter` |
| 看 CPU、内存、网络和硬盘状态 | [控制中心](nascenter/README.md) | `nascenter` |
| 恢复 SSH、设置开机自动恢复 | [SSH 恢复](sshrecovery/README.md) | `sshrecovery` |
| 发文件下载链接、接收上传 | [文件快传](quickshare/README.md) | `quickshare` |
| 下载直链、磁链和种子 | [下载中心](downloadcenter/README.md) | `downloadcenter` |

## 更新、卸载和常见问题

**更新**：用新版安装包重新走一遍安装步骤，选择 `1`。已有配置会保留。

**卸载**：运行同一个 `bash manage.sh`，选择 `2`，跟着提示确认。只在小米客户端里删除，插件可能会自动恢复。

**旧版本的数据**：新安装器会自动处理，一般不用自己搬。遇到目录冲突时按报错处理，保留原目录和数据。迁移旧 Docker 工具时可能短暂停止容器。

**提示找不到 manage.sh**：先进入真正包含 `manage.sh` 的插件文件夹，再运行命令。

**提示 SSH 连接或权限失败**：先确认 IP 正确、电脑能用 root SSH 登录 NAS，再重试安装。

**关闭页面后服务继续运行**：Mihomo、快传和下载中心会继续运行，需要停止时用各自页面的“停止服务”。

## 其他用法

- [NAS 脚本简单用法](nas-root/README.md)：开启 SSH、安装开机恢复、修复 Docker 网络、升级 Docker。
- [插件进阶安装与故障排查](docs/plugin-installation.md)：指定账号、在 NAS 本机安装、查看和手动恢复插件。
- [数据保存在哪里](nas-root/docs/data-layout.md)：配置、任务和恢复资料的位置。
