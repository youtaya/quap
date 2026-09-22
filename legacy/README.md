# 公开行情旧监控

这是迁出 Qlib 之前的实验性采集进程，使用公开行情适配器和本地 SQLite。生产采集、分析和看板在仓库根目录的 `quant-platform` 服务中。两套进程不能同时拥有同一组观察列表的实时行情。

新的启动需要显式打开 `QLIB_MONITOR_LEGACY_ENABLE=1`。`status` 和 `stop` 保持可用，避免迁移期间误启动第二个采集所有者。

在 `legacy/` 目录：

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dashboard]"
.venv/bin/python -m legacy_monitor.service status --config examples/market_monitor/config.yaml
.venv/bin/python -m legacy_monitor.service stop --config examples/market_monitor/config.yaml
```

经批准的实验或回滚启动：

```bash
QLIB_MONITOR_LEGACY_ENABLE=1 .venv/bin/python -m legacy_monitor.service start --config examples/market_monitor/config.yaml
.venv/bin/python -m streamlit run examples/market_monitor/app.py --server.address 127.0.0.1
```

运行时文件写在配置文件旁边的 `.runtime/`。导入到 QUAP 时，把该目录以只读方式挂进 API 容器。可选的 Qlib 历史读取依赖本机已安装的 Qlib，并且只读本地 `provider_uri`。
