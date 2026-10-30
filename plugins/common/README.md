# 公共组件与共享源码

本目录集中维护公共代码。安装器按插件依赖安装组件；各插件私有提权模块仍只维护在所属插件的 `installer/common/modules/<插件>/`。

| 目录 | 职责 | NAS 安装位置 |
| --- | --- | --- |
| [manager](manager/README.md) | 预检、事务、安装卸载和回退 | `/data/minas/plugins/manager/` |
| [privileged](privileged/README.md) | 固定提权入口、账号授权、公共锁和事件服务 | `/data/minas/privileged/` |
| [recovery](recovery/README.md) | 观察事件，调用管理引擎恢复已托管插件 | `/data/minas/plugins/recovery/` |
| [updater](updater/README.md) | 签名更新、账号设置、调用管理引擎 | `/data/minas/plugins/updater/` |
| runtime | 插件路径、服务控制与日志工具 | 同步到各插件所属文件 |
| data-layout | 当前持久目录布局的共享代码 | 同步到对应组件及独立脚本源码 |
| [ui](ui/README.md) | 主题、表单、页面行为及更新界面 | 同步到各插件页面资源 |

文件快传和下载中心不安装公共权限组件、不获得其业务授权。其他六插件只安装自己的私有模块。包内仍使用 `components/<组件>/` 与 `plugins/<插件>/`，仓库目录不决定 NAS 的目标路径；归属契约由 manager/layout.py 的 OWNERSHIP 和 file_contract 校验。

安装与卸载见[根教程](../../README.md)，同步与打包见[维护工具](../../workspace-tools/README.md)。
