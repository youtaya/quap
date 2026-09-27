from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from quant_platform.api import create_app
from quant_platform.domain import BasketInput, CN, now
from quant_platform.jobs.analyze import alert
from quant_platform.providers import Deferred, ProviderError
from quant_platform.providers.market import CAPABILITY_SOURCES, SourceBudget as Budget
from quant_platform.storage import LostLease, jsonb
from quant_platform.storage.baskets import Conflict, save_basket

pytestmark = pytest.mark.postgres


def seed(db):
    day = now().astimezone(CN).date()
    with db.transaction() as conn:
        for i in range(-3, 4):
            for exchange in ("SSE", "SZSE"):
                conn.execute("INSERT INTO calendars VALUES(%s,%s,true,now())", (exchange, day + timedelta(days=i)))
        conn.execute(
            "INSERT INTO instruments VALUES('SH600895','Test','Main Board','SSE',%s,NULL,'L','{}',now())",
            (day - timedelta(days=500),),
        )
    return day


def enqueue(db):
    with db.transaction() as conn:
        return db.enqueue(conn, "test", "test", "unique")


def test_single_claim_fencing_atomic_publication(db):
    jid = enqueue(db)
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = list(executor.map(lambda owner: db.claim("test", owner), ["a", "b"]))
    assert sum(j is not None for j in jobs) == 1
    old = next(j for j in jobs if j)
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (jid,))
    new = db.claim("test", "replacement")
    assert new["fence"] > old["fence"]
    with pytest.raises(LostLease):
        with db.publication(old):
            pytest.fail("Expired owner must not publish")
    with pytest.raises(RuntimeError):
        with db.publication(new) as conn:
            db.set_setting(conn, "rollback", 1)
            raise RuntimeError("crash")
    assert db.setting("rollback") is None
    with db.publication(new) as conn:
        db.set_setting(conn, "published", 1)
        db.enqueue(conn, "child", "test", "downstream")
    assert db.setting("published") == 1
    assert db.rows("SELECT status FROM jobs WHERE id=%s", (jid,))[0]["status"] == "complete"
    assert enqueue(db) == jid


def test_data_revision_reversions_are_not_lost(db):
    with db.transaction() as conn:
        a = db.dataset(conn, "daily", "2026-01-01", [1], {})
        repeated = db.dataset(conn, "daily", "2026-01-01", [1], {})
        b = db.dataset(conn, "daily", "2026-01-01", [2], {})
        reverted = db.dataset(conn, "daily", "2026-01-01", [1], {})
    assert a == repeated
    assert a < b < reverted


def test_basket_effective_next_session_and_conflict(db):
    day = seed(db)
    value = BasketInput(name="basket", members={"SH600895": 1})
    with db.transaction() as conn:
        saved = save_basket(conn, value)
    assert saved["effective_day"] == day + timedelta(days=1)
    assert db.active_baskets(day) == []
    assert len(db.active_baskets(day + timedelta(days=1))) == 1
    with pytest.raises(Conflict):
        with db.transaction() as conn:
            save_basket(conn, value, UUID(saved["id"]))


def test_alert_duplicate_restart_missing_not_rearmed(db):
    at = now()
    with db.transaction() as conn:
        alert(conn, "rule", "first", 0.04, 0.03, 300, {}, at)
        alert(conn, "rule", "first", 0.04, 0.03, 300, {}, at)
        alert(conn, "rule", "missing", None, 0.03, 300, {}, at + timedelta(minutes=10))
        alert(conn, "rule", "sustained", 0.04, 0.03, 300, {}, at + timedelta(minutes=10))
    assert len(db.rows("SELECT * FROM alerts")) == 1
    with db.transaction() as conn:
        alert(conn, "rule", "exit", 0.01, 0.03, 300, {}, at + timedelta(minutes=11))
        alert(conn, "rule", "reenter", 0.04, 0.03, 300, {}, at + timedelta(minutes=12))
    assert len(db.rows("SELECT * FROM alerts")) == 2


def test_source_budget_is_shared_between_workers(db, settings):
    settings.ordinary_rpm = 1
    one, two = Budget(db, settings), Budget(db, settings)
    one.acquire("eastmoney")
    with pytest.raises(Deferred):
        two.acquire("eastmoney")
    two.acquire("sina")  # 每个公开源有各自的窗口，一个源用尽不会拖住其它源


class MembershipFeed:
    """Security master plus a constituent endpoint that can be taken down independently."""

    def __init__(self, settings, members=None, error=None):
        self.settings = settings
        self.members = members or set()
        self.error = error
        self.last_sources = {"securities": "sina"}

    def securities(self):
        return {
            "SH600895": {
                "name": "张江高科",
                "board": "Main Board",
                "exchange": "SSE",
                "list_date": None,
                "delist_date": None,
                "list_status": "L",
            }
        }

    def constituents(self):
        if self.error:
            raise ProviderError(self.error)
        return self.members


def test_reference_keeps_the_security_master_when_membership_is_unavailable(db, settings):
    """默认 history_scope=index 下成分股是唯一范围来源，但取不到也不能让主表回滚。"""
    from quant_platform.jobs import collect

    with db.transaction() as conn:
        db.enqueue(conn, "securities", "history", "reference-without-membership")
    job = db.claim("history", "tester")
    collect.reference(db, MembershipFeed(settings, error="No public source served constituents (eastmoney:ProviderError)"), job)

    assert [row["symbol"] for row in db.rows("SELECT symbol FROM instruments")] == ["SH600895"]
    assert db.setting("index_members") is None
    assert "constituents" in db.setting("directory")["index_members"]["error"]
    assert db.setting("directory")["index_members"]["symbols"] == []


def test_reference_records_the_membership_source(db, settings):
    from quant_platform.jobs import collect

    with db.transaction() as conn:
        db.enqueue(conn, "securities", "history", "reference-with-membership")
    job = db.claim("history", "tester")
    collect.reference(db, MembershipFeed(settings, members={"SH600895", "SZ300750"}), job)

    membership = db.setting("index_members")
    assert membership["symbols"] == ["SH600895"]  # 只保留本次主表确认存在的身份
    assert db.setting("directory")["index_members"]["error"] is None


def test_constraints_job_derives_limits_from_stored_closes(db, settings):
    """`constraints` is declared `derived`: the job must read stored closes, never the provider.

    Deriving limits from a per-security history request needs `sessions x securities` provider calls
    — 1500 x 298 in a default deployment — so the bootstrap could never converge inside the tokenless
    quota, and the admission check that needs one `constraints` partition per session never passed.
    """
    import httpx

    from quant_platform.jobs import market as market_jobs
    from quant_platform.providers.market import Market

    day = seed(db)
    previous = day - timedelta(days=1)
    with db.transaction() as conn:
        db.set_setting(conn, "index_members", {"index": "SH000300", "symbols": ["SH600895"], "dated": False})
        did = db.dataset(conn, "history", "SH600895", [{"day": str(previous)}], {})
        conn.execute("INSERT INTO daily_bars VALUES('SH600895',%s,%s,%s)", (previous, did, jsonb({"close": 10.0})))
        db.enqueue(conn, "constraints", "history", "derive-limits", {"day": str(day)})
    job = db.claim("history", "tester")

    def refuse(request):
        raise AssertionError(f"constraints must not fetch {request.url}")

    feed = Market(settings, db, client=httpx.Client(transport=httpx.MockTransport(refuse)), sleep=lambda _: None)
    market_jobs.constraints(db, feed, job)

    rows = db.rows("SELECT * FROM market_constraints WHERE day=%s", (day,))
    assert [row["symbol"] for row in rows] == ["SH600895"]
    assert rows[0]["data"]["limit_up"] == pytest.approx(11.0)  # 主板 ±10%
    assert rows[0]["data"]["limit_down"] == pytest.approx(9.0)
    assert rows[0]["data"]["derived"] is True
    assert rows[0]["data"]["suspended"] is None
    assert rows[0]["data"]["suspended_source"] == "unavailable"
    dataset = db.rows("SELECT * FROM datasets WHERE endpoint='constraints'")[0]
    assert dataset["scope"] == str(day) and dataset["metadata"]["source"] == "daily_bars"
    assert feed.last_sources == {}


def test_api_auth_and_versioned_mutations(db, settings):
    seed(db)
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/api/v1/baskets").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        assert client.post("/api/v1/baskets", json={"name": "Basket", "members": {"SH600895": 1}}).status_code == 410
        targets = {"name": "Model", "weights": {"SH600895": 0.05}, "cash_weight": 0.95}
        response = client.post("/api/v1/model-portfolios", json=targets)
        assert response.status_code == 201, response.text
        data = response.json()
        assert client.get("/api/v1/model-portfolios").json()[0]["revision"] == 1
        assert client.put("/api/v1/model-portfolios/" + data["id"], json=targets).status_code == 409
        assert (
            client.put("/api/v1/model-portfolios/" + data["id"], json={**targets, "expected_revision": 1}).status_code
            == 200
        )
        assert client.get("/api/v1/status").status_code == 200
        assert client.post("/api/v1/control", json={"action": "pause"}).status_code == 202
        assert db.setting("polling_paused") is True


# ------------------------------------------------------------ capability qualification


class ProbeFeed:
    """A doctor feed with a fixed reachability probe and no capability call at all."""

    def __init__(self, probes):
        self.probes = probes

    def probe(self):
        return self.probes

    def close(self):
        pass


def capabilities(db):
    return {row["endpoint"]: row for row in db.rows("SELECT * FROM capabilities")}


def test_doctor_never_marks_a_capability_verified_from_a_reachability_probe(db, settings):
    """`readiness` 门禁读的是 `schema_verified`，而探测只能证明源此刻可达。

    换源时这个写入口随旧供应商一起丢了：三处消费者要求 `schema_verified` 为真，却没有任何代码
    写过它，于是 `data-readiness` 永远报 `permissions: <全部日常能力>`，`qualification` 也永远
    停在 pending。
    """
    from quant_platform.operations import doctor

    result = doctor(db, settings, feed=ProbeFeed({"sina": {"reachable": True, "rows": 5}}))
    assert "securities" in result["successful"]  # 可达即可解除阻塞，让任务去试

    rows = capabilities(db)
    assert rows["securities"]["status"] == "reachable"
    assert rows["securities"]["data"]["reachable_sources"] == ["sina"]
    assert rows["securities"]["data"]["schema_verified"] is False  # 探测不能证明契约成立
    # 派生能力没有可探测的源，「已验证」就是推导代码本身。
    assert rows["constraints"]["status"] == "reachable"
    assert rows["constraints"]["data"]["schema_verified"] is True
    assert rows["constraints"]["data"]["source"] == "derived"
    assert rows["security_history"]["data"]["schema_verified"] is True
    assert rows["security_history"]["data"]["source"] == "derived"


def test_a_capability_read_out_of_another_request_still_records_its_own_verification(db, settings):
    """`readiness` 要求 `DAILY_CAPABILITIES` 的每一项都带 `schema_verified`。

    `calendar` 与 `benchmark` 走的是 `history` 的请求，`factors` 走 `_factors` 的成对请求，
    所以它们都不经过 `_degrade(<自己的名字>)`。只按发出请求的那个能力记录，这三项就永远是假：
    `data-readiness` 会报「未验证能力」，`qualification` 的两个交易日门禁也永远不成立 —— 平台
    明明在采集，却永远报自己被挡住。
    """
    from quant_platform.providers.market import Market

    feed = Market(settings, db)
    feed.budget = None
    feed._qualify_derived("calendar", "sina", [{"day": "2026-09-25"}])
    feed._qualify_derived("benchmark", "tencent", [{"day": "2026-09-25"}])
    feed._qualify_derived("factors", "tencent", [{"day": "2026-09-25"}])

    rows = capabilities(db)
    for capability in ("calendar", "benchmark", "factors"):
        assert rows[capability]["status"] == "reachable"
        assert rows[capability]["data"]["schema_verified"] is True
    assert rows["benchmark"]["data"]["source"] == "tencent"
    assert rows["calendar"]["data"]["rows"] == 1


def test_a_derived_capability_with_no_source_records_no_verification(db, settings):
    """`_factors` 拿不到成对价格时返回空映射并给出 `None` 源；那没有证据，不能写成已验证。"""
    from quant_platform.providers.market import Market

    feed = Market(settings, db)
    feed.budget = None
    feed._qualify_derived("factors", None, {})

    assert "factors" not in capabilities(db)


def test_doctor_keeps_a_verification_earned_by_a_real_collection(db, settings):
    from quant_platform.operations import doctor

    with db.transaction() as conn:
        db.capability(
            "history",
            "reachable",
            {"schema_verified": True, "source": "sina", "rows": 5, "verified_at": "2026-09-26T07:00:00+00:00"},
        )
    doctor(db, settings, feed=ProbeFeed({"sina": {"reachable": True, "rows": 5}}))

    data = capabilities(db)["history"]["data"]
    assert data["schema_verified"] is True
    assert (data["source"], data["rows"], data["verified_at"]) == (
        "sina",
        5,
        "2026-09-26T07:00:00+00:00",
    )  # 真实采集留下的证据不能被探测改写


def test_doctor_drops_a_verification_no_source_can_still_serve(db, settings):
    from quant_platform.operations import doctor

    with db.transaction() as conn:
        db.capability("securities", "reachable", {"schema_verified": True, "source": "eastmoney"})
    doctor(db, settings, feed=ProbeFeed({"tencent": {"reachable": True, "rows": 5}}))

    row = capabilities(db)["securities"]
    assert row["status"] == "blocked"
    assert row["data"]["schema_verified"] is False


def test_doctor_prunes_capabilities_the_platform_no_longer_declares(db, settings):
    """旧令牌供应商的端点行会把 `data_quality` 的阻塞清单永久撑满。"""
    from quant_platform.operations import CAPABILITY_SOURCES, data_quality, doctor

    with db.transaction() as conn:
        db.capability("stock_basic", "blocked", {})
        db.capability("adj_factor", "blocked", {})
    doctor(db, settings, feed=ProbeFeed({"sina": {"reachable": True, "rows": 5}}))

    assert set(capabilities(db)) == set(CAPABILITY_SOURCES)
    blocked = {row["endpoint"] for row in data_quality(db)["blocked_capabilities"]}
    assert blocked <= set(CAPABILITY_SOURCES)


def test_market_records_the_capability_verification_once_per_instance(db, settings):
    """一次成功采集就是契约成立的证据；一个 `history` 任务每只证券一次请求，不能每只都写一行。"""
    from quant_platform.providers.market import Market

    feed = Market(settings, db)
    feed.budget = None  # 只验证上报，不消耗源配额
    feed._qualify("history", "sina", [{"day": "2026-09-25"}])
    feed._qualify("history", "sina", [{"day": "2026-09-24"}])

    row = capabilities(db)["history"]
    assert row["status"] == "reachable"
    assert row["data"]["schema_verified"] is True
    assert row["data"]["source"] == "sina" and row["data"]["rows"] == 1
    assert row["data"]["sources"] == list(CAPABILITY_SOURCES["history"])


def test_market_records_a_circuit_open_when_every_source_fails(db, settings):
    from quant_platform.providers import ProviderError
    from quant_platform.providers.market import Market

    feed = Market(settings, db)
    feed.budget = None

    def broken():
        raise ProviderError("Source transport failed: RemoteProtocolError")

    with pytest.raises(ProviderError):
        feed._degrade("securities", (("eastmoney", broken), ("pytdx", broken)))

    row = capabilities(db)["securities"]
    assert row["status"] == "circuit_open"
    assert row["data"]["schema_verified"] is False
    assert row["data"]["failures"] == ["eastmoney:ProviderError", "pytdx:ProviderError"]


def test_market_records_nothing_when_every_source_defers(db, settings):
    """配额耗尽说明不了契约有任何问题，不能把一个已验证的能力降级掉。"""
    from quant_platform.providers import Deferred, ProviderError
    from quant_platform.providers.market import Market

    with db.transaction() as conn:
        db.capability("securities", "reachable", {"schema_verified": True, "source": "sina"})
    feed = Market(settings, db)
    feed.budget = None

    def throttled():
        raise Deferred("Source quota exhausted; request deferred.", 60)

    with pytest.raises(ProviderError):
        feed._degrade("securities", (("sina", throttled),))

    row = capabilities(db)["securities"]
    assert row["status"] == "reachable" and row["data"]["schema_verified"] is True


def test_market_cools_a_source_that_failed_instead_of_retrying_it_per_security(db, settings):
    """没有冷却，每只证券都要先把不可达的源超时一遍：298 只证券的回填因此要一个多小时。"""
    from quant_platform.providers import ProviderError
    from quant_platform.providers.market import Market

    feed = Market(settings, db)
    feed.budget = None
    tried = []

    def dead(name):
        def call():
            tried.append(name)
            raise ProviderError("Source transport failed: RemoteProtocolError")

        return call

    def live():
        tried.append("sina")
        return ["bar"]

    attempts = (("pytdx", dead("pytdx")), ("eastmoney", dead("eastmoney")), ("sina", live))
    for _ in range(3):
        assert feed._degrade("history", attempts) == ("sina", ["bar"])

    # 第一轮三个源都试；之后两轮只碰还在冷却窗口外的那个可用源。
    assert tried == ["pytdx", "eastmoney", "sina", "sina", "sina"]
    assert feed._cooldown.keys() == {"pytdx", "eastmoney"}


def test_market_retries_a_cooled_source_once_the_window_elapses(db, settings, monkeypatch):
    """冷却必须能自行解除，否则一次抖动就要等运维介入。"""
    import quant_platform.providers.market as market_module
    from quant_platform.providers import ProviderError
    from quant_platform.providers.market import Market

    clock = [1000.0]
    monkeypatch.setattr(market_module.time, "monotonic", lambda: clock[0])
    feed = Market(settings, db)
    feed.budget = None
    tried = []

    def flaky():
        tried.append("sina")
        if len(tried) == 1:
            raise ProviderError("Source transport failed: RemoteProtocolError")
        return ["bar"]

    attempts = (("sina", flaky),)
    with pytest.raises(ProviderError):
        feed._degrade("history", attempts)  # 第一次：源真的失败了
    with pytest.raises(ProviderError):
        feed._degrade("history", attempts)  # 冷却窗口内：直接跳过，不再打源
    assert tried == ["sina"]

    clock[0] += market_module.SOURCE_COOLDOWN_SECONDS + 1
    assert feed._degrade("history", attempts) == ("sina", ["bar"])
    assert tried == ["sina", "sina"]


def test_market_does_not_cool_a_source_that_only_returned_an_empty_window(db, settings):
    """停牌证券在哪个源上都是空的；契约性空响应只对本次请求成立。"""
    from quant_platform.providers import ProviderError
    from quant_platform.providers.market import Market

    feed = Market(settings, db)
    feed.budget = None
    tried = []

    def empty():
        tried.append("sina")
        return []

    with pytest.raises(ProviderError):
        feed._degrade("history", (("sina", empty),), accept=bool)

    assert feed._cooldown == {}
    assert tried == ["sina"]


def test_readiness_clears_the_capability_blocker_once_every_capability_is_verified(db, settings):
    """端到端把这条门禁跑通：缺的是一次成功采集，不是「权限」。"""
    from quant_platform.pipeline import DAILY_CAPABILITIES, readiness

    blocked = readiness(db, settings)["frequencies"]["day"]["blockers"]
    assert any(item.startswith("数据源未验证：") for item in blocked)

    with db.transaction() as conn:
        for capability in DAILY_CAPABILITIES:
            db.capability(capability, "reachable", {"schema_verified": True, "source": "sina"})
    blockers = readiness(db, settings)["frequencies"]["day"]["blockers"]

    assert not any(item.startswith("数据源未验证：") for item in blockers)
    assert blockers == ["采集范围为「index」；生产验收要求 QUANT_HISTORY_SCOPE=all，"
                        "且全市场日线覆盖率至少 95%",
                        "必需的 Qlib 服务未就绪", "没有已发布、已合格且未过期的 Qlib 模型",
                        "没有已验证的 Qlib 数据代次"]


def test_readiness_states_the_collection_scope_instead_of_hiding_it_behind_coverage(db, settings):
    """`minimum_coverage` 的下限是 0.95，所以「采了多少市场」必须自己说清楚，不能靠覆盖门禁暗示。"""
    from quant_platform.pipeline import readiness

    scoped = readiness(db, settings)["frequencies"]["day"]["blockers"]
    assert any("采集范围为「index」" in item for item in scoped)

    full = settings.model_copy(update={"history_scope": "all"})
    assert not any("采集范围" in item for item in readiness(db, full)["frequencies"]["day"]["blockers"])


def test_pipeline_scope_is_the_collection_scope(db, settings):
    """训练宇宙必须等于采集范围：分母是全市场而分子只来自成分股时，覆盖门禁永远不成立。

    复用本文件已有的 `seed`：两条路径必须给出同一个集合，而范围之外的证券不能被带进来。
    """
    from quant_platform.jobs.collect import scoped_codes, scoped_codes_on

    day = seed(db)
    with db.transaction() as conn:
        db.set_setting(conn, "index_members", {"index": "SH000300", "symbols": ["SH600895"], "dated": False})
    assert scoped_codes(db, settings, day) == {"SH600895"}
    with db.transaction() as conn:
        assert scoped_codes_on(conn, settings, day) == {"SH600895"}


def test_data_quality_names_the_securities_that_cannot_enter_a_generation(db):
    """有 K 线但一根复权因子都没有的证券进不了代次，样本面会无声缩小，所以必须显式报出来。

    参考 QuantMind 对缺列的处置：缺口要么被命名，要么就没人知道它存在。这个数量与名单既不进
    覆盖门禁、也不进任何报错，正是最容易被漏掉的那一类。
    """
    from quant_platform.operations import data_quality

    day = seed(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO instruments VALUES('SZ000001','NoFactor','Main Board','SZSE',%s,NULL,'L','{}',now())",
            (day - timedelta(days=500),),
        )
        did = db.dataset(conn, "history", str(day), [{"symbol": "SH600895"}], {})
        for code in ("SH600895", "SZ000001"):
            conn.execute(
                "INSERT INTO daily_bars(symbol,day,dataset_id,data) VALUES(%s,%s,%s,%s)",
                (code, day, did, jsonb({"close": 10})),
            )
    quality = data_quality(db)
    assert quality["bars_without_any_adjustment"] == {
        "count": 2,
        "symbols": ["SH600895", "SZ000001"],
    }
    assert quality["instruments_without_list_date"] == 0
    assert quality["point_in_time_risk_state"]["rows"] == 0
    assert quality["point_in_time_risk_state"]["dated"] is False


def test_market_health_separates_a_quiet_session_from_a_real_failure():
    """非交易时段没有新行情是正常的；和真实采集故障共用一个字段会让这个告警失去可信度。"""
    from quant_platform.operations import market_health

    weekend = datetime(2026, 9, 27, 11, 0, tzinfo=CN)  # 周日
    assert market_health(weekend, 10, 10, True, False) == "fresh"
    assert market_health(weekend, 10, 3, True, False) == "quiet_outside_session"
    # 日历覆盖不全时不许猜「休市」——那会把「不知道」冒充成「已确认」。
    assert market_health(weekend, 10, 3, True, None) == "degraded_or_unavailable"
    # 目录不新鲜就是真故障，与今天开不开市无关。
    assert market_health(weekend, 10, 3, False, False) == "degraded_or_unavailable"
    # 交易时段内验证不足仍然是降级。
    intraday = datetime(2026, 9, 25, 10, 0, tzinfo=CN)  # 周五盘中
    assert market_health(intraday, 10, 3, True, True) == "degraded_or_unavailable"
