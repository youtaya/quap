"""Active cost-basis holdings; one listed symbol at a time."""

from uuid import uuid4

from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict


def save_holding(conn, value, holding_id=None):
    known = conn.execute("SELECT symbol FROM instruments WHERE symbol=%s AND status='L'", (value.symbol,)).fetchone()
    if not known:
        raise Conflict("Holding must be a validated, currently listed instrument.")
    if holding_id:
        prior = conn.execute("SELECT * FROM holdings WHERE id=%s FOR UPDATE", (holding_id,)).fetchone()
        if not prior or prior["archived"] or prior["revision"] != value.expected_revision:
            raise Conflict("Holding revision changed or holding is unavailable.")
        clash = conn.execute(
            "SELECT 1 FROM holdings WHERE symbol=%s AND NOT archived AND id<>%s", (value.symbol, holding_id)
        ).fetchone()
        if clash:
            raise Conflict("An active holding already exists for this symbol.")
        revision = prior["revision"] + 1
        conn.execute(
            "UPDATE holdings SET symbol=%s,cost_price=%s,quantity=%s,note=%s,revision=%s,updated_at=now() "
            "WHERE id=%s",
            (value.symbol, value.cost_price, value.quantity, value.note, revision, holding_id),
        )
    else:
        if value.expected_revision != 0:
            raise Conflict("New holdings start at revision zero.")
        clash = conn.execute("SELECT 1 FROM holdings WHERE symbol=%s AND NOT archived", (value.symbol,)).fetchone()
        if clash:
            raise Conflict("An active holding already exists for this symbol.")
        holding_id, revision = uuid4(), 1
        conn.execute(
            "INSERT INTO holdings(id,symbol,cost_price,quantity,note,revision) VALUES(%s,%s,%s,%s,%s,%s)",
            (holding_id, value.symbol, value.cost_price, value.quantity, value.note, revision),
        )
    data = value.model_dump(exclude={"expected_revision"})
    conn.execute(
        "INSERT INTO audit(action,target,data) VALUES('holding_revision',%s,%s)",
        (str(holding_id), jsonb(data)),
    )
    return {"id": str(holding_id), "revision": revision, "symbol": value.symbol}
