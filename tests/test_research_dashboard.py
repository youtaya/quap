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
    widget(app.checkbox, "Enable this research source").check()
    widget(app.checkbox, "I have reviewed and acknowledge this upstream's data terms.").check()
    widget(app.button, "Save source revision").click().run()
    assert not app.exception
    assert not widget(app.button, "Request research collection").disabled
    widget(app.text_area, "Explicit stock codes (comma-separated, at most 50)").set_value("SH600000")
    widget(app.checkbox, "Request this bounded collection; no historical availability is implied.").check()
    widget(app.button, "Request research collection").click().run()
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
    widget(app.text_input, "Factor name").set_value("Momentum")
    widget(app.text_input, "Factor author").set_value("Fixture")
    widget(app.button, "Save experimental factor").click().run()
    assert not app.exception
    factors = app.session_state["fixture_data"]["factors"]
    widget(app.text_input, "Factor-set name").set_value("Candidate factors")
    widget(app.multiselect, "Ordered factor revisions").set_value([factors[0]])
    widget(app.button, "Save experimental factor set").click().run()
    assert not app.exception
    for name in ("Baseline", "Candidate"):
        widget(app.text_input, "Training-configuration name").set_value(name)
        widget(app.button, "Save immutable training configuration").click().run()
        assert not app.exception
    widget(app.button, "Compare development folds").click().run()
    assert not app.exception
    before_evidence = len(app.session_state["fixture_data"]["mutations"])
    widget(app.checkbox, "Load immutable validation factor evidence").check().run()
    assert not app.exception
    assert widget(app.selectbox, "Validation feature").value == "R_Momentum"
    assert len(app.session_state["fixture_data"]["mutations"]) == before_evidence
    widget(app.checkbox, "I select using validation evidence only; final test exposure will be recorded.").check()
    widget(app.button, "Freeze candidate specification").click().run()
    assert not app.exception
    widget(app.button, "Create challenger from frozen candidate").click().run()
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
