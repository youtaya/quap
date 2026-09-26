"""Fixture-only research workspace journeys; backend qualification is separate."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).with_name("research_dashboard_fixture.py")


def widget(elements, label):
    return next(item for item in elements if item.label == label)


def test_explicit_source_collection_and_quarantine_journey():
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not app.exception
    assert len(app.sidebar.radio[0].options) == 6
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
