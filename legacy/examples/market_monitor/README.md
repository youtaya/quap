# A 股公开行情看板

面向科创板、创业板和沪深主板的实验看板。它轮询公开行情适配器或确定性演示数据，计算日频板块与篮子代理，并记录本地告警。不提交订单。

生产系统是仓库根目录的 QUAP 平台，说明见 [部署说明](../../../docs/部署说明.md)。本目录只保留迁移和回滚所需的旧进程。

在 `legacy/` 目录、Python 3.10+：

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dashboard]"
.venv/bin/python -m streamlit run examples/market_monitor/app.py --server.address 127.0.0.1
```

默认配置是 `live`。演示数据把数据模式改为演示后，点两次「立即刷新」可以看到价格和告警变化。公开轮询是尽力而为，不作为生产行情。

可选 Qlib 历史把 `provider_uri` 指向含 `calendars/day.txt` 的本地数据集。`csi300` 这类宇宙篮子需要该数据集。
