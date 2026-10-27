# 七插件统一源码仓库

- 本目录是七插件后续 GitHub 上传、开发和提交的唯一源码来源，不再分别使用七个旧插件仓库，不用旧 WSL 副本覆盖本目录。七个插件子目录不建立嵌套 Git 仓库。
- 插件源码在各插件 ID 同名目录（如 `dockermanager/`、`mihomo/`）；公共构建与检查在 `workspace-tools/`。共有权限代码维护源为 `workspace-tools/privileged-component/`，各插件私有模块只维护在所属 `installer/common/modules/<插件>/`。恢复服务维护源为 `workspace-tools/plugin-recovery/recovery.py`。
- `nas-root/` 中的独立 SSH、网络和容器管理工具同属本仓库；构建维护其源码及根目录单文件入口，不再使用独立工具仓库提交。设备专属容器重建记录、离线材料和凭据留在本地。
- 测试仅本地保存。`tests/`、`__tests__/` 和独立测试文件不得加入 Git 索引或安装包；Markdown 文档保留。已跟踪测试只取消跟踪，不删除本地文件；不使用 `git add -f` 绕过规则。
- 新安装包统一输出根目录 `dist/`，构建清单为 `workspace-tools/package-manifest.json`，根 `package-manifest.json` 与其同步。不要创建 `releases/`。子项目旧 `dist/` 是本地历史产物。
- 缓存、构建依赖、密钥、凭据、离线应急导出和本地实例数据不进入源码提交或安装包。原七仓库 Git 历史保存在本目录外的工作区 `workspace-tools/local/legacy-plugin-git/`。
- 配置与持久数据使用插件 `etc/var` 或必要的受保护外部目录，不写进校验源码。普通缓存、日志、PID 使用官方 `tmp` → `plugintmp/<插件>/`；root 先降权再访问用户缓存。
- 配置、任务与断点、更新内核、root 授权和锁、独立 SSH 恢复材料不能迁入可清空缓存。原子替换最终暂存与目标同目录。
- 更新源码后同步 INFO 摘要和注册元数据，保留禁用状态、任务配置、SSH 材料及服务状态。完成原厂校验后才能报告真机部署成功；隔离测试不能替代真机验收。
- 统一远端为 `https://github.com/daixu0502/xiaomi-nas-plugin`。提交和推送按当前用户授权执行，不绑定旧插件仓库，不因为本地重构而自动提交或推送。
- 仓库根 `README.md` 只做导航，保留插件／工具名称、简短用途和对应 README 链接。安装、更新、卸载、配置、恢复、注意事项、常见问题和维护说明放在对应插件／工具 README 中，不再向根 README 添加或回填使用说明。

- 标准源码与安装包层级为根 INFO、src/files、src/ui、scripts/control、按需 etc；安装辅助在 installer/，不得重建 payload 包装层。
- 各插件的发布版本只维护在该插件根目录的 `INFO` 的 `version` 字段（如 `mihomo/INFO`），不创建仓库级版本配置。构建从各自 INFO 自动同步安装入口、页面资源缓存键与版本展示及接口版本；安装包携带完整 INFO 和已同步源码，可独立复制到 WSL 安装。也可运行 `python3 workspace-tools/sync_versions.py` 单独同步，`--check` 只检查。公共权限组件和恢复服务的版本分别维护在其公共维护源的 VERSION 常量，构建同步副本及组件摘要。不手动修改生成位置中的版本号，不将内核版本、协议版本或历史说明中的版本当作插件发布版本替换。
