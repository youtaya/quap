"""Versioned basket mutations and immutable effective-date membership."""

from uuid import uuid4

from quant_platform.domain import CN, now
from quant_platform.storage import jsonb


class Conflict(ValueError):
    pass


def save_basket(conn, value, basket_id=None):
    day = now().astimezone(CN).date()
    next_day = conn.execute(
        "SELECT day FROM calendars WHERE day>%s AND exchange IN ('SSE','SZSE') AND is_open "
        "GROUP BY day HAVING count(*)=2 ORDER BY day LIMIT 1",
        (day,),
    ).fetchone()
    if not next_day:
        raise Conflict("A verified next trading session is required before activating a basket revision.")
    covered = conn.execute(
        "SELECT count(*) AS n FROM (SELECT day FROM calendars WHERE day>%s AND day<=%s "
        "AND exchange IN ('SSE','SZSE') GROUP BY day HAVING count(*)=2 "
        "AND min(is_open::int)=max(is_open::int)) verified",
        (day, next_day["day"]),
    ).fetchone()["n"]
    if covered != (next_day["day"] - day).days:
        raise Conflict("Calendar gaps prevent next-session activation.")
    codes = list(value.members)
    known = conn.execute("SELECT symbol FROM instruments WHERE symbol=ANY(%s) AND status='L'", (codes,)).fetchall()
    if {r["symbol"] for r in known} != set(codes):
        raise Conflict("Every constituent must be a validated, currently listed instrument.")
    if basket_id:
        prior = conn.execute("SELECT * FROM baskets WHERE id=%s FOR UPDATE", (basket_id,)).fetchone()
        if not prior or prior["archived"] or prior["revision"] != value.expected_revision:
            raise Conflict("Basket revision changed or basket is unavailable.")
        revision = prior["revision"] + 1
        conn.execute("UPDATE baskets SET name=%s,revision=%s WHERE id=%s", (value.name, revision, basket_id))
    else:
        if value.expected_revision != 0:
            raise Conflict("New baskets start at revision zero.")
        basket_id, revision = uuid4(), 1
        conn.execute("INSERT INTO baskets(id,name,revision) VALUES(%s,%s,%s)", (basket_id, value.name, revision))
    data = value.model_dump(exclude={"expected_revision"})
    conn.execute(
        "INSERT INTO basket_revisions(basket_id,revision,effective_day,data) VALUES(%s,%s,%s,%s)",
        (basket_id, revision, next_day["day"], jsonb(data)),
    )
    conn.execute("INSERT INTO audit(action,target,data) VALUES('basket_revision',%s,%s)", (str(basket_id), jsonb(data)))
    return {"id": str(basket_id), "revision": revision, "effective_day": next_day["day"]}
