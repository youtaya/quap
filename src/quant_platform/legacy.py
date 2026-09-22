"""Read-only-source legacy migration. Research and adjusted legacy prices stay untouched."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from quant_platform.domain import BasketInput, digest
from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict, save_basket


def inspect_legacy(runtime):
    root = Path(runtime).resolve()
    settings, events = {}, []
    with closing(sqlite3.connect((root / "service.sqlite3").as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT value FROM monitor_state WHERE key='settings'").fetchone()
        settings = json.loads(row[0])["data"] if row else {}
    path = root / "alerts.sqlite3"
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE mode='live' ORDER BY id")]
    baskets, unresolved = [], []
    for name, data in settings.get("baskets", {}).items():
        if "universe" in data:
            unresolved.append(
                {"name": name, "reason": "Dated universe membership requires a separately verified adapter."}
            )
            continue
        symbols, weights = data.get("symbols", []), data.get("weights")
        weights = weights or [1] * len(symbols)
        if len(weights) != len(symbols) or len(set(symbols)) != len(symbols):
            raise ValueError("Invalid legacy basket members/weights.")
        baskets.append(
            BasketInput(
                name=name,
                members=dict(zip(symbols, weights)),
                alerts={
                    k: v
                    for k, v in settings.get("alerts", {}).items()
                    if k in {"stock_change", "basket_change", "cooldown", "minimum_coverage"}
                },
            )
        )
    return {"settings": settings, "baskets": baskets, "events": events, "unresolved": unresolved}


def migrate_legacy(db, runtime, apply=False):
    data = inspect_legacy(runtime)
    source = str(Path(runtime).resolve())
    source_key = "legacy_source:" + digest(source)
    key = digest({"path": source, "settings": data["settings"], "events": data["events"]})
    summary = {
        "baskets": len(data["baskets"]),
        "live_events": len(data["events"]),
        "unresolved": data["unresolved"],
        "prices_imported": 0,
        "demo_events_imported": 0,
        "dry_run": not apply,
        "source_hash": key,
    }
    if not apply:
        return summary
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (source_key,))
        saved = conn.execute("SELECT value FROM settings WHERE key=%s FOR UPDATE", (source_key,)).fetchone()
        if saved and saved["value"]["snapshot"] == key:
            return {**summary, "already_imported": True}
        if not saved and conn.execute("SELECT 1 FROM imports WHERE source_hash=%s", (key,)).fetchone():
            raise Conflict("Previous import lacks a source mapping; reconcile existing baskets before reimporting.")
        mapping = saved["value"]["baskets"] if saved else {}
        for basket in data["baskets"]:
            content_hash = digest(basket.model_dump(exclude={"expected_revision"}))
            prior = mapping.get(basket.name)
            if prior and prior["hash"] == content_hash:
                continue
            if prior:
                current = conn.execute("SELECT * FROM baskets WHERE id=%s FOR UPDATE", (prior["id"],)).fetchone()
                if (
                    not current
                    or current["archived"]
                    or current["revision"] != prior["revision"]
                    or current["control_revision"] != prior["control_revision"]
                ):
                    raise Conflict("Imported basket was changed locally; reconcile it before importing source edits.")
                basket.expected_revision = prior["revision"]
            result = save_basket(conn, basket, prior["id"] if prior else None)
            mapping[basket.name] = {
                "id": result["id"],
                "revision": result["revision"],
                "control_revision": 0,
                "hash": content_hash,
            }
        db.set_setting(conn, source_key, {"snapshot": key, "baskets": mapping})
        for event in data["events"]:
            event_key = "legacy:" + digest((source, event))
            if conn.execute(
                "INSERT INTO alert_keys(event_key) VALUES(%s) ON CONFLICT DO NOTHING RETURNING event_key", (event_key,)
            ).fetchone():
                conn.execute(
                    "INSERT INTO alerts(event_key,data) VALUES(%s,%s)", (event_key, jsonb({**event, "legacy": True}))
                )
        db.set_setting(conn, "legacy_settings", data["settings"])
        conn.execute(
            "INSERT INTO imports(source_hash,data) VALUES(%s,%s) ON CONFLICT DO NOTHING", (key, jsonb(summary))
        )
    return summary
