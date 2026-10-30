# 插件在线更新组件

八个插件共用更新后台、签名验证和版本号入口。维护源为本目录；`sync-plugin-updater.py` 分发后台、公钥和普通用户 CGI 适配器。界面维护源在 `../ui/online-update.js` 和 `online-update.css`，通过 `sync-ui-controls.py` 同步。

## 使用方式

电脑端点击左下角版本号，手机端点击底部版本号打开更新弹窗；新版本显示 `NEW`。打开插件时自动检查是否需要查询，成功或失败均缓存六小时；同账号后台检查在五分钟内复用已验证的发布清单。没有常驻网络轮询或自动安装，手动“检查更新”可立即查询。

“更新代理”按 NAS 账号保存，八个插件共用；仅用于 GitHub 检查和下载安装包，不修改其他服务代理。支持 HTTP/HTTPS CONNECT，例如 Mihomo 的 `http://127.0.0.1:7890`。用户名和密码分开填写，密码不返回页面，留空保持原密码，勾选清除后保存可删除。启用代理后连接失败不会静默直连。不支持 GitHub 镜像前缀或 SOCKS 地址。

点击“立即更新”并确认后，独立 systemd 任务完成下载、验证、备份、安装和原厂校验，关闭页面仍会继续。重新打开弹窗可看进度，完成后关闭插件窗口再打开。运行中的下载任务、快传活动连接及公共组件关键任务会阻止安装，请稍后重试。原本停止的服务不会因更新而被启动。

首次使用仍需通过 `manage.sh install` 安装带此组件的版本。安装器只授权当前账号更新当前插件，不保存 NAS root 密码，不开放 root HTTP 服务。

## 发布新版本

插件版本和说明仍只维护在各自根 `INFO`。更新后台行为变化时增加本目录 `updater.py` 的 `VERSION`；同版本后台内容不允许静默替换。恢复组件版本由其维护源管理。

同步、测试、设备验证并在独立工作树提交源码后，构建并发布：

```powershell
python -B workspace-tools/sync-plugin-updater.py
python -B workspace-tools/sync-ui-controls.py
python -B workspace-tools/build_plugin_packages.py
python -B workspace-tools/publish-plugin-release.py --tag v2026.10.29.3 --target 完整源码提交SHA --signing-key local/plugin-update-signing/private.pem --publish
```

不加 `--publish` 只构建和签名。发布工具使用现有 Git 凭据或 `GH_TOKEN`/`GITHUB_TOKEN`，不显示令牌。先上传草稿的八包、`plugin-updates.json`、二进制签名 `plugin-updates.sig` 和 `SHA256SUMS`，校验附件后才公开并标记 latest。正式标签不允许替换附件，每次使用新标签。清单分别记录各插件 INFO 版本、架构、说明、大小和摘要。

`release-public-key.txt` 是公开 RSA 公钥，通过 OpenSSL 验证 SHA-256/RSA 签名。私钥只留在本地被忽略的受保护目录，不进入源码、安装包、日志或 GitHub 附件。丢失私钥后必须通过受信任安装器分发新公钥，不能用修改远端清单绕过验证。

## 权限与恢复

- 更新前、更新后及回退校验统一使用恢复组件的 locale 边界。原厂校验失败的结构化诊断在回退前保存；即使回退另有错误，也保留最初的校验原因。任务清理后，当前结果的 `verification` 字段继续保留退出码、校验环境、权限与摘要诊断，不保留源码内容或历史日志。

- 后台位于 `/data/minas/plugins/updater/`；代理密码、签名候选清单、任务和回退记录保存在其 root 私有账号目录，不进入可清空缓存。
- CGI 仅接受 NAS 认证网关下的 JSON POST，通过固定 sudo 命令调用后台。账号须与 sudo UID 一致，不接受浏览器指定下载地址、shell、安装路径或其他账号。
- 网络始终降为插件用户执行。代理认证经 stdin 传递，不在命令行或日志中出现。root 验证签名、SHA-256、大小、INFO、架构及压缩包路径；拒绝链接、设备、重复成员、特殊权限和越界解压。
- 更新与原安装器共用全局安装锁及每插件恢复锁。源码在目标同目录暂存，内容写入和清理由插件用户执行；root 通过固定目录句柄创建及切换目录项，不放宽原厂 root:root 源码父目录的权限；配置、任务、更新内核和 SSH 材料不作为源码覆盖。注册信息与快照一起更新，最后执行原厂 verify。
- 失败恢复源码、控制入口、注册信息、权限模块和快照指针。开机的 `minas-plugin-update-recover.service` 和下次更新会恢复未提交事务。更新成功并完成校验后立即删除安装包、解压目录与本次回退副本，恢复快照只保留当前生效的一份。已结束任务的整个目录及日志一并删除，不保留最近任务历史；尚在运行或未完成回退的事务保留到恢复完成，清理失败会在后续检查或开机时重试。旧版本升级到本清理机制时，由新组件等待工作进程退出和安装锁释放后完成历史材料清理。
- 卸载通过原安装器取消该账号、插件的更新 sudo 规则，不影响其他插件。

root 可手动恢复中断事务：

```sh
/usr/bin/python3 -I -B /data/minas/plugins/updater/updater.py recover
systemctl status minas-plugin-update-recover.service
```

进度由页面查询；root 私有的 `users/<账号>/<插件>/jobs/<任务>/status.json` 和 `journal.json` 仅在任务执行与中断恢复期间保存阶段和回退状态，不记录代理密码或密钥。任务结束清理后，仅在 `current.json` 保留当前结果和检查时间，供页面获知成功或失败并避免重复自动检查，不包含历史日志、请求和清单副本。页面已显示的日志仅留在当前窗口内存，重新打开插件不恢复历史日志。测试代码只留本地。
