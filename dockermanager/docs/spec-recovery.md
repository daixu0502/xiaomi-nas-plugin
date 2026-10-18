# 规范与恢复服务

安装完成会补齐 INFO、config、300×300 PNG、完整 resource 与 frontend，调用原厂校验并登记独立恢复服务。详见集中分发目录的恢复说明。

`etc/var` 使用指向 `/data/.minas-plugin-data/<用户>/<插件>/` 的受控链接；普通缓存仍使用官方 tmp。手动 `manage.sh uninstall` 取消自动恢复登记，持久数据保留。应用市场的删除会由恢复服务补回；确实要删除请使用 manage.sh。

固件清除 systemd 与 Cron 后，需在 root 终端执行：

```sh
python3 -B /data/.minas-plugin-recovery/recovery.py bootstrap
```
