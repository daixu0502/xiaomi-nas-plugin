# 公共权限组件 1.3.0（API v1，布局 v3）

共有代码只维护在 plugins/common/privileged/，完整离线套件中仅有一份。
Docker 管理、Mihomo、任务中心、控制中心、SSH 恢复和影视编辑分别维护自己的
installer/common/modules/<插件>/。文件快传与下载中心没有权限组件依赖或业务授权。

固定安装位置为 /data/minas/privileged/：minas-helper、component_admin.py、common/、
modules/<插件>/、registry.json、users/<账号>/。代码 root 所有且用户不可写；
任务、SSH 材料、授权和锁使用 root 私有目录。现有锁保持 inode，不用替换锁文件解锁。
SSH 材料在 modules/sshrecovery/var/；影视 protected 状态在账号插件持久目录，
全局写锁在 modules/mediaeditor/var/。独立 /data/minas/ssh 工具不属于插件。

统一引擎先校验 API、版本及完整摘要，再安装共有代码、所属模块和当前账号授权。
更高兼容版本不降级；同版本内容冲突拒绝覆盖。只初始化缺失状态，不改已有任务、
密钥、模块材料或内核。源码冲突与非当前布局直接报告路径，不自动迁移。

组件变更归入外层插件事务，sudoers、Cron、systemd 和授权随整个事务回退。
安装锁 → 恢复记录锁 → 组件锁 → 模块状态锁；调用需要自行加锁的组件或服务前
释放其锁。活动任务和关键业务操作阻止维护，维护期间拒绝新的业务变更。

普通卸载按账号撤权并删除该插件自有数据，成功后不保留回退备份；最后一个引用
消失后才清理模块代码，固定锁保留。更新与旧版切换重装保留数据和原启停状态。
首次安装任务中心只有空任务列表，不执行模板；不自动配置 SSH 登录或开机恢复。

显示缓存、普通日志和 PID 通过降权 IO 访问官方 tmp；root 不直接遍历用户缓存。
最终原子替换暂存与目标同目录。公共版本来自 privileged_runtime.py 的 VERSION。
