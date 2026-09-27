"""Fixture-only research workspace journeys; backend qualification is separate."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).with_name("research_dashboard_fixture.py")


def widget(elements, label):
    return next(item for item in elements if item.label == label)


def test_explicit_source_collection_and_quarantine_journey():
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not app.exception
    assert len(app.sidebar.radio[0].options) == 7
    assert "Qlib Report" in app.sidebar.radio[0].options
    assert app.session_state["fixture_data"]["mutations"] == []
    widget(app.checkbox, "启用该研究数据源").check()
    widget(app.checkbox, "我已阅读并确认该上游的数据条款。").check()
    widget(app.button, "保存数据源版本").click().run()
    assert not app.exception
    assert not widget(app.button, "请求研究采集").disabled
    widget(app.text_area, "显式股票代码（逗号分隔，最多 50 只）").set_value("SH600000")
    widget(app.checkbox, "请求这次有界采集；这不代表历史数据可用。").check()
    widget(app.button, "请求研究采集").click().run()
    assert not app.exception
    mutations = app.session_state["fixture_data"]["mutations"]
    assert [row["path"] for row in mutations] == ["/research-sources/adata-eastmoney-intraday", "/research-collections"]
    assert mutations[-1]["body"]["expected_revision"] == 1
    assert any("quarantined" in element.value for element in app.json)
    assert not any("Accept" in button.label for button in app.button)


def test_factor_configuration_comparison_freeze_and_challenger_journey():
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    app.sidebar.radio[0].set_value("Models & Validation").run()
    assert not app.exception
    widget(app.text_input, "因子名称").set_value("Momentum")
    widget(app.text_input, "因子作者").set_value("Fixture")
    widget(app.button, "保存实验性因子").click().run()
    assert not app.exception
    factors = app.session_state["fixture_data"]["factors"]
    widget(app.text_input, "因子集名称").set_value("Candidate factors")
    widget(app.multiselect, "有序因子版本").set_value([factors[0]])
    widget(app.button, "保存实验性因子集").click().run()
    assert not app.exception
    for name in ("Baseline", "Candidate"):
        widget(app.text_input, "训练配置名称").set_value(name)
        widget(app.button, "保存不可变训练配置").click().run()
        assert not app.exception
    widget(app.button, "比较开发折").click().run()
    assert not app.exception
    before_evidence = len(app.session_state["fixture_data"]["mutations"])
    widget(app.checkbox, "加载不可变的验证集因子证据").check().run()
    assert not app.exception
    assert widget(app.selectbox, "验证特征").value == "R_Momentum"
    assert len(app.session_state["fixture_data"]["mutations"]) == before_evidence
    widget(app.checkbox, "我仅依据验证集证据做选择；最终测试集的曝光会被记录。").check()
    widget(app.button, "冻结候选配置").click().run()
    assert not app.exception
    widget(app.button, "用已冻结的候选创建挑战模型").click().run()
    assert not app.exception
    mutations = app.session_state["fixture_data"]["mutations"]
    assert mutations[-1]["path"] == "/model-training-runs"
    assert (
        mutations[-1]["body"]["training_configuration_id"]
        == app.session_state["fixture_data"]["freeze"]["configuration_id"]
    )
    assert all(not mutation["path"].endswith(("/promote", "/accept")) for mutation in mutations)


def test_qlib_report_page_renders_ic_quantiles_and_book():
    """The Qlib Report workspace shows IC, quantile means and the benchmark-relative book."""
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    app.sidebar.radio[0].set_value("Qlib Report").run()
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["IC"] == "0.0500"
    assert metrics["Rank IC"] == "0.0700"
    assert metrics["日均 IC"] == "0.0400"
    assert metrics["相对基准收益"] == "7.00%"
    assert metrics["有效样本"] == "1,200"
    subheaders = [element.value for element in app.subheader]
    assert "五分位平均未来 5 日收益" in subheaders
    assert "相对基准的等权组合" in subheaders
    # The book-vs-benchmark caption only renders for a complete book, so it proves the comparison.
    captions = [element.value for element in app.caption]
    assert any("每 20 个交易日再平衡" in text and "调仓 3 次" in text for text in captions)
    # Two bar charts: the quantile means and the book-vs-benchmark comparison. Streamlit testing
    # exposes vega-lite charts as untyped elements, so count those.
    charts = [element for element in app.main if type(element).__name__ == "UnknownElement"]
    assert len(charts) == 2
    # The quantile table and the rebalance detail table both render.
    assert len(app.dataframe) == 2
    # The raw JSON stays available behind an expander.
    assert len(app.json) == 1
    # Descriptive evidence only: the page offers no accept/promote action.
    assert not any("Accept" in button.label for button in app.button)
