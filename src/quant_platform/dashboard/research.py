"""Research-only panels inside the existing API-only workspaces."""

from datetime import datetime
from urllib.parse import urlencode

import pandas as pd
import streamlit as st

from quant_platform.domain import CN
from quant_platform.dashboard.workflow import FREQUENCIES, request_key


def paged(call, path, key, size=25):
    index = int(st.number_input("结果页码", min_value=1, max_value=4000, value=1, key="page:" + key))
    separator = "&" if "?" in path else "?"
    response = call("GET", f"{path}{separator}limit={size}&offset={(index - 1) * size}")
    if not isinstance(response, dict):
        return []
    if response.get("next_offset") is not None:
        st.caption("下一页还有更多结果。")
    return response.get("items", [])


def mutate(call, method, path, body):
    body = {**body, "request_key": request_key(path, body)}
    result = call(method, path, body)
    if result is not None:
        st.session_state.notice = f"研究请求已记录：{result}。未发生模型发布或建议采纳。"
        st.rerun()
    return result


def job_controls(call, item, key):
    state = item.get("status")
    if state not in {"pending", "running", "blocked", "failed"}:
        return
    action = "cancel" if state in {"pending", "running"} else "retry"
    label = "取消研究任务" if action == "cancel" else "重试研究任务"
    if st.button(label, key="control:" + key):
        mutate(
            call, "POST", f"/research-jobs/{item['job_id']}/{action}", {"expected_revision": item["control_revision"]}
        )


def source_panels(call, table):
    st.subheader("补充研究数据源")
    st.warning("adata 仅用于展示与交叉校验，不能用于合格化生产数据、股票池、模型或建议。")
    response = call("GET", "/research-sources")
    if not isinstance(response, dict):
        st.info("研究数据源目录不可用。")
        return
    sources = response.get("items", [])
    if not sources:
        return
    selected_id = st.selectbox("研究数据源", [row["source_id"] for row in sources])
    selected = next(row for row in sources if row["source_id"] == selected_id)
    st.json(selected)
    with st.form("source:" + selected["source_id"]):
        enabled = st.checkbox("启用该研究数据源", value=selected.get("enabled", False))
        terms = st.checkbox("我已阅读并确认该上游的数据条款。")
        submitted = st.form_submit_button("保存数据源版本")
    if submitted:
        mutate(
            call,
            "PUT",
            "/research-sources/" + selected["source_id"],
            {"expected_revision": selected["revision"], "enabled": enabled, "terms_acknowledged": terms},
        )
    if not response.get("collection_enabled"):
        st.info("当前部署已关闭采集。仅确认数据源条款不会开启网络访问。")
    with st.form("research_collection"):
        symbols = st.text_area("显式股票代码（逗号分隔，最多 50 只）")
        today = datetime.now(CN).date()
        start = st.date_input("研究起始日期", value=today, max_value=today)
        end = st.date_input("研究结束日期", value=today, max_value=today)
        confirmed = st.checkbox("请求这次有界采集；这不代表历史数据可用。")
        submitted = st.form_submit_button(
            "请求研究采集",
            disabled=not response.get("collection_enabled") or not selected.get("enabled"),
        )
    if submitted:
        if not confirmed:
            st.error("请确认这次显式采集请求。")
        else:
            mutate(
                call,
                "POST",
                "/research-collections",
                {
                    "source_id": selected["source_id"],
                    "expected_revision": selected["revision"],
                    "symbols": [value.strip() for value in symbols.split(",") if value.strip()],
                    "start": str(start),
                    "end": str(end),
                },
            )
    collections = paged(call, "/research-collections", "collections")
    table(collections)
    if collections:
        by_id = {row["id"]: row for row in collections}
        item_id = st.selectbox(
            "查看采集任务", list(by_id), format_func=lambda key: f"{key} · {by_id[key]['status']}"
        )
        job_controls(call, by_id[item_id], "collection")
    snapshot_panel(call, table, "source=" + selected["source_id"], source_id=selected["source_id"])


def snapshot_panel(call, table, key, **filters):
    st.caption("快照抓取时间不等于历史可用时间。被隔离或失败的结果仍会显示，且不会替换已成功的证据。")
    rows = paged(call, "/research-snapshots?" + urlencode(filters), "snapshots:" + key)
    table(rows, columns=["id", "source_id", "symbol", "status", "retrieved_at", "row_count"])
    if not rows:
        st.info("暂无补充快照。覆盖率与历史可用性未知。")
        return
    chosen = st.selectbox(
        "查看补充快照",
        rows,
        key="snapshot:" + key,
        format_func=lambda row: f"{row['symbol']} · {row['status']} · {row['retrieved_at']}",
    )
    detail = call("GET", f"/research-snapshots/{chosen['id']}?limit=100")
    if detail:
        st.json(detail["snapshot"])
        table(detail["rows"]["items"])
        if detail["rows"].get("next_offset") is not None:
            st.caption("预览最多 100 行；已认证的 API 支持继续翻页。")
    checks = call("GET", "/research-quality?" + urlencode({"snapshot_id": chosen["id"]}))
    if checks:
        st.subheader("诊断差异与覆盖率")
        st.json(checks)


def factor_library(call, table):
    inventory = call("GET", "/factors/inventory")
    if inventory:
        with st.expander("不可变的 Alpha158 与五分钟因子清单"):
            st.json(inventory)
    factors = paged(call, "/factors", "factors")
    table(factors)
    with st.form("new_factor"):
        name = st.text_input("因子名称", max_chars=64)
        expression = st.text_area("受限的日线因子表达式", value="$close/Ref($close,5)-1", max_chars=2048)
        author = st.text_input("因子作者", max_chars=120)
        revision = st.number_input("预期因子版本（新建填 0）", min_value=0, value=0)
        st.caption(
            "仅支持行情字段、四则运算、Abs/Log、过去 Ref/Delta、Mean/Std/Sum/Min/Max/Corr；窗口 1–252。"
            "不允许 Python 或标签。"
        )
        submitted = st.form_submit_button("保存实验性因子")
    if submitted:
        mutate(
            call,
            "POST",
            "/factors",
            {"name": name, "expression": expression, "author": author, "expected_revision": int(revision)},
        )
    sets = paged(call, "/factor-sets", "factor-sets")
    table(sets)
    with st.form("new_factor_set"):
        name = st.text_input("因子集名称", max_chars=120)
        members = st.multiselect(
            "有序因子版本", factors, format_func=lambda row: f"{row['name']} r{row['revision']}"
        )
        revision = st.number_input("预期因子集版本（新建填 0）", min_value=0, value=0)
        submitted = st.form_submit_button("保存实验性因子集")
    if submitted:
        mutate(
            call,
            "POST",
            "/factor-sets",
            {"name": name, "factors": [row["id"] for row in members], "expected_revision": int(revision)},
        )


def configurations(call, table):
    rows = paged(call, "/training-configurations", "configurations")
    table(rows)
    sets = paged(call, "/factor-sets", "configuration-sets")
    with st.form("new_training_configuration"):
        name = st.text_input("训练配置名称", max_chars=120)
        frequency = st.selectbox(
            "配置频率", ["day", "5min"], format_func=lambda value: FREQUENCIES.get(value, value)
        )
        factor_set = st.selectbox(
            "特征契约",
            [None, *sets],
            format_func=lambda row: "内置特征" if row is None else f"{row['name']} r{row['revision']}",
        )
        rate = st.number_input("学习率", min_value=0.001, max_value=0.3, value=0.05, format="%.3f")
        leaves = st.number_input("LightGBM 叶子数", min_value=2, max_value=127, value=31)
        rounds = st.number_input("最大提升轮数", min_value=10, max_value=2000, value=1000)
        seed = st.number_input("随机种子", min_value=0, max_value=2147483647, value=42)
        revision = st.number_input("预期配置版本（新建填 0）", min_value=0, value=0)
        st.caption(
            "三个固定的时间顺序折。日线窗口 756/252/252 个交易日；五分钟 120/40/60。"
            "最终测试集保留至冻结时使用。"
        )
        submitted = st.form_submit_button("保存不可变训练配置")
    if submitted:
        train, valid, test = (756, 252, 252) if frequency == "day" else (120, 40, 60)
        mutate(
            call,
            "POST",
            "/training-configurations",
            {
                "name": name,
                "frequency": frequency,
                "factor_set_id": factor_set["id"] if factor_set else None,
                "learning_rate": rate,
                "num_leaves": int(leaves),
                "rounds": int(rounds),
                "seed": int(seed),
                "expected_revision": int(revision),
                "train_sessions": train,
                "validation_sessions": valid,
                "test_sessions": test,
            },
        )
    return rows


def experiment_panel(call, table):
    configs = configurations(call, table)
    # Only pipeline-prepared generations carry frozen pools and a strategy snapshot; a scheduled
    # daily export is not a comparable experiment input.
    generations = [
        row
        for row in (call("GET", "/qlib-generations") or [])
        if row.get("origin") != "scheduled"
    ]
    if len(configs) >= 2 and generations:
        with st.form("compare_research"):
            generation = st.selectbox(
                "已准备的规范数据代次",
                generations,
                format_func=lambda row: f"{row['id']} · {FREQUENCIES.get(row['frequency'], row['frequency'])}",
            )
            baseline = st.selectbox(
                "基线配置", configs, format_func=lambda row: f"{row['name']} r{row['revision']}"
            )
            candidate = st.selectbox(
                "候选配置", configs, index=1, format_func=lambda row: f"{row['name']} r{row['revision']}"
            )
            submitted = st.form_submit_button("比较开发折")
        if submitted:
            mutate(
                call,
                "POST",
                "/research-experiments",
                {
                    "expected_revision": 0,
                    "generation_id": generation["id"],
                    "baseline_configuration_id": baseline["id"],
                    "candidate_configuration_id": candidate["id"],
                },
            )
    else:
        st.info("比较需要两个不可变配置和一个已合格的规范数据代次。")
    experiments = paged(call, "/research-experiments", "experiments")
    table(experiments)
    if not experiments:
        return
    by_id = {row["id"]: row for row in experiments}
    selected_id = st.selectbox(
        "查看开发实验", list(by_id), format_func=lambda key: f"{key} · {by_id[key]['status']}"
    )
    selected = by_id[selected_id]
    detail = call("GET", f"/research-experiments/{selected['id']}?limit=100")
    if not detail:
        return
    trials = detail["trials"]["items"]
    table(trials)
    curves = []
    for trial in trials:
        for point in (trial.get("result") or {}).get("quality", []):
            curves.append(
                {
                    "time": point["time"],
                    "configuration": str(trial["configuration_id"]),
                    "fold": trial["fold"],
                    "rank_ic": point.get("rank_ic"),
                }
            )
    if curves:
        frame = pd.DataFrame(curves)
        st.line_chart(frame, x="time", y="rank_ic", color="configuration")
    with st.expander("已固定的折、留出集、尝试次数与时延"):
        st.json(detail)
    if detail["trials"].get("next_offset") is not None:
        st.caption("仅显示前 100 次尝试；其余尝试可通过分页 API 获取。")
    with st.expander("因子覆盖率、分布、相关性与稳定性"):
        if st.checkbox("加载不可变的验证集因子证据", key="factor-evidence:" + selected_id):
            trial_evidence(call, table, selected_id, trials)
    job_controls(call, detail["experiment"], "experiment")
    frozen = detail.get("freeze")
    if selected["status"] == "complete" and not frozen:
        with st.form("freeze_candidate"):
            chosen = st.selectbox("要冻结的配置", [selected["baseline_id"], selected["candidate_id"]])
            confirmed = st.checkbox("我仅依据验证集证据做选择；最终测试集的曝光会被记录。")
            submitted = st.form_submit_button("冻结候选配置")
        if submitted and confirmed:
            mutate(
                call,
                "POST",
                f"/research-experiments/{selected['id']}/freeze",
                {"configuration_id": chosen, "expected_revision": 1},
            )
    if frozen:
        st.caption("此处仅冻结配置。训练仍需通过既有的评估、影子与人工发布门禁。")
        if st.button("用已冻结的候选创建挑战模型"):
            config = (
                selected["spec"]["baseline"]
                if str(selected["baseline_id"]) == str(frozen["configuration_id"])
                else selected["spec"]["candidate"]
            )
            body = {
                "purpose": "training",
                "frequency": config["frequency"],
                "training_configuration_id": frozen["configuration_id"],
            }
            body["request_key"] = request_key("frozen-training:" + str(frozen["id"]), body)
            result = call("POST", "/model-training-runs", body)
            if result is not None:
                st.success(f"候选模型训练任务 {result['run_id']} 已提交；当前发布版本未变。")


def trial_evidence(call, table, experiment_id, trials):
    available = {row["id"]: row for row in trials if row.get("state") == "succeeded" and row.get("artifact_id")}
    if not available:
        st.info("暂无可用的成功因子证据。")
        return
    trial_id = st.selectbox(
        "验证尝试",
        list(available),
        format_func=lambda key: f"{available[key]['configuration_id']} · 第 {available[key]['fold']} 折 · {key}",
        key="trial-evidence:" + experiment_id,
    )
    path = f"/research-experiments/{experiment_id}/trials/{trial_id}/diagnostics"
    catalog = call("GET", path + "?limit=1")
    if not catalog or catalog.get("status") != "available":
        st.info("因子诊断不可用；平台不会生成替代证据。")
        return
    feature = st.selectbox("验证特征", catalog["features"], key="feature-evidence:" + trial_id)
    index = int(
        st.number_input("因子稳定性页码", min_value=1, max_value=1667, value=1, key="stability:" + trial_id)
    )
    evidence = call("GET", path + "?" + urlencode({"feature": feature, "limit": 60, "offset": (index - 1) * 60}))
    if not evidence or evidence.get("status") != "available":
        st.info("所选因子证据不可用。")
        return
    st.caption("仅供验证期诊断，不是最终测试集证据或生产合格化。相关性覆盖前 64 个特征。")
    st.json({key: evidence.get(key) for key in ("coverage", "missingness", "distribution", "correlation")})
    quality = evidence["quality"]
    table(quality["items"])
    if quality["items"]:
        st.line_chart(pd.DataFrame(quality["items"]).set_index("time")[["ic", "rank_ic"]])
    if quality.get("next_offset") is not None:
        st.caption("下一页还有更多稳定性观测。")


def diagnostics_panel(call, table, model_id):
    response = call("GET", f"/models/{model_id}/diagnostics?limit=100")
    if not response or response.get("status") == "unavailable":
        st.info("诊断不可用。旧模型需要重新训练才能获得仅基于训练的漂移基线；标签成熟后才会报告质量。")
        return
    st.caption("仅供参考：PSI 阈值 0.1/0.25 不会触发交易、重训或发布。盘中质量按交易日汇总。")
    st.json(response["summary"]["rolling"])
    sessions = response["summary"].get("sessions", [])
    if sessions:
        st.line_chart(pd.DataFrame(sessions).set_index("session")[["ic", "rank_ic"]])
    items = response.get("items", [])
    table(items)
    if items:
        latest = items[0]["data"]
        drift = latest.get("drift", {})
        st.json(
            {"drift": drift, "realized": latest.get("realized"), "stage_latency_seconds": latest.get("latency_seconds")}
        )
        importance = drift.get("importance_gain", {})
        if importance:
            st.bar_chart(pd.DataFrame({"gain": importance}).sort_values("gain", ascending=False).head(30))
