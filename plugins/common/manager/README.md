# 统一插件安装与生命周期

唯一入口为套件根目录 manage.sh。套件含八插件、共有组件与 ARM64 默认内核。
安装本身无需访问外网，NAS 仍须具备 Python 3.9+、systemd 和原厂插件运行环境。

    sh manage.sh
    sh manage.sh install --ip 192.168.31.100 --users u123456 --plugins mihomo,quickshare
    sh manage.sh reinstall --ip 192.168.31.100 --yes
    sh manage.sh status --ip 192.168.31.100
    sh manage.sh uninstall --ip 192.168.31.100 --users u123456 --plugins quickshare --yes

NAS root 本机运行时省略 --ip。在源码仓库运行安装入口时，使用
--bundle /绝对路径/解压后的套件。--dry-run 只显示预检结果。
多个存储池的新账号安装需要通过 --pool pool1 明确指定。

reinstall 默认选择所有账号已有插件，执行整批切换；保留数据及原启停状态，
不额外安装原本没有的插件。普通 uninstall **彻底删除当前账号插件数据**，
包括配置、任务、断点、插件专属恢复材料及快照，成功后不保留回退备份。
用户下载与媒体文件、Docker 容器和卷、独立 SSH 登录及开机恢复工具不属于插件数据。
卸载失败或中断时保留事务材料；提交后的删除会在下次维护或开机继续完成。

全新安装立即启用入口并启动 Mihomo、下载中心、文件快传和空任务调度器；
SSH 登录及开机恢复须在对应页面明确配置。更新与保留数据重装遵守已有停止意图。

公共 Python 引擎位于 /data/minas/plugins/manager/，恢复程序和更新后台分别
保留在 /data/minas/plugins/recovery/、/data/minas/plugins/updater/。
权限组件位于 /data/minas/privileged/；六个提权插件只安装自己的模块。
文件快传和下载中心不安装公共权限组件。

layout.py 的 OWNERSHIP 是机器可检查的文件归属契约，并进入签名套件清单。
用户持久数据通过插件 etc/var 链接访问；普通缓存使用官方 tmp，root 不直接访问
其内容。未完成事务属于 root 私有恢复材料，不放入用户缓存。源码目录不保存运行数据。

构建使用仓库既有发布密钥：

    python3 -B workspace-tools/build/build_suite.py --release v2026.10.30.3

构建阶段下载并核对固定内核摘要；私钥、构建缓存及测试均不进入套件或源码提交。
套件与按插件划分的在线更新包输出 dist/。新发布要求更新器 2.0.0，旧更新器会提示
先通过统一安装器切换。在线更新继续保留签名校验、账号隔离和代理设置。
