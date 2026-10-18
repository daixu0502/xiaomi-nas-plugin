# SSH 恢复后端

页面通过唯一 CGI 调用受保护的固定权限模块，不额外启动 HTTP 服务。权限模块源码在 `installer/common/modules/sshrecovery/`，安装后存放于 `/data/.minas-privileged/`。本目录不保存运行数据或 SSH 材料。
