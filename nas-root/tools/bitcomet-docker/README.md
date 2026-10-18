# BitComet Docker 部署

官网链接的 `wxhere/bitcomet-webui:v2.22.0`，ARM64 WebUI 版；不替换下载中心或 qBittorrent。

- WebUI：NAS 局域网地址的 `6080` 端口，入口 `/webui/`；经典界面 `/panel/`。
- BT：`6082/TCP` 和 `6082/UDP`，需要入站连接时可自行在路由器转发。不要把管理端口直接开放到公网。
- 用户：`admin`，随机密码由部署程序生成。NAS 上 `/data/bitcomet/credentials.env` 为管理员专用 0600 文件，不纳入仓库。
- 下载：容器 `/root/Downloads` 对应所属 NAS 用户的 `data/Download/BitComet`。只挂载该专用子目录；不要把容器内其他路径用作大文件下载目录。
- 配置：Docker 命名卷 `bitcomet_config`；配置和少量元数据使用 Docker 存储区，大文件位于存储池。
- 权限：镜像增加最小目录权限适配，以 NAS 用户的 UID/GID 运行，丢弃全部 Linux capabilities，禁止提权；不挂载 Docker socket。
- 网络：自定义桥接网络 `bridge-net`。出站 NAT 由独立通用服务 `bridge-net.service` 管理，脚本位于 `/data/bridge-net/apply.py`，不再依赖 `/data/bitcomet/`。实现与说明见相邻 `bridge-net-network/` 目录。
- 使用显式 bind 挂载，不会像 `-v` 自动创建缺失的宿主下载路径。存储池晚于 Docker 挂载时，容器可能启动失败；应待存储池正常后手动启动，不能在硬盘未就绪时补建路径。

## 文件

- `Dockerfile`：基于官方镜像的普通用户权限适配。构建参数 `NAS_UID`、`NAS_GID` 应与目标用户一致。
- `load_image.py`：可选，通过 NAS 已有的 `127.0.0.1:7890` 代理下载固定 ARM64 清单与镜像层，逐项校验 SHA-256 后导入 Docker；不改 Docker 全局代理。临时下载文件自动清理。
- `deploy.py`：一次性部署入口，参数为 NAS 用户和局域网 IP；拒绝覆盖现有容器、卷或目录。使用前先创建独立网络并安装网络恢复单元。

升级上游版本时需重新构建权限适配镜像，再保留配置卷和下载挂载重建容器，不要直接把镜像改回上游默认 root 版本。停机超时配置为 120 秒。未实际重启 NAS 测试。
