"""The real reader runs only in the separately installed optional environment."""

from copy import deepcopy
from datetime import date
import os

import pytest

from quant_platform.adapters.qlib.export import GenerationStore, external_features, external_universe


@pytest.mark.skipif(os.getenv("QUANT_TEST_QLIB") != "1", reason="Real Qlib requires the isolated optional environment")
def test_two_real_generations_and_reader_failure_isolation(tmp_path, history_rows, monkeypatch):
    store = GenerationStore(tmp_path)
    days = [r["day"] for r in history_rows]
    with store.lock:
        stage, first, _ = store.build({"SH600895": history_rows}, days)
        store.publish(stage, first)
    initial = store.features("SH600895")
    assert initial["rows"][-1]["$close"] == pytest.approx(11.39, abs=1e-5)
    assert initial["rows"][-1]["$volume"] == 1000000
    revised = deepcopy(history_rows)
    for item in revised:
        for field in ("open", "high", "low", "close"):
            item["data"][field] *= 2
    with store.lock:
        stage, second, _ = store.build({"SH600895": revised}, days)
        store.publish(stage, second)
    assert first != second and (store.root / first).is_dir()
    assert store.features("SH600895")["rows"][-1]["$close"] == pytest.approx(22.78, abs=1e-5)
    before = {str(p.relative_to(store.current())): p.read_bytes() for p in store.current().rglob("*") if p.is_file()}
    assert external_features(store.current(), "SH600895")["rows"]
    after = {str(p.relative_to(store.current())): p.read_bytes() for p in store.current().rglob("*") if p.is_file()}
    assert before == after

    def fail(*args, **kwargs):
        raise RuntimeError("component unavailable")

    monkeypatch.setattr("quant_platform.adapters.qlib.export.isolated_read", fail)
    with store.lock, pytest.raises(RuntimeError):
        store.build({"SH600895": revised}, days)
    assert store.current().name == second
    assert not list(store.root.glob("staging-*"))
    from quant_platform.analysis import indicators

    assert indicators(history_rows)["status"] == "complete"


def test_external_universe_requires_dated_membership(tmp_path):
    folder = tmp_path / "instruments"
    folder.mkdir()
    path = folder / "selected.txt"
    path.write_text("SH600895\t2025-01-01\t2025-12-31\nSZ000001\t2026-01-01\t2026-12-31\n")
    assert external_universe(tmp_path, "selected", date(2025, 6, 1)) == ["SH600895"]
    path.write_text("SH600895\n")
    with pytest.raises(ValueError, match="start date"):
        external_universe(tmp_path, "selected", date(2025, 6, 1))
    with pytest.raises(ValueError, match="Invalid universe"):
        external_universe(tmp_path, "../outside", date(2025, 6, 1))
