"""The spend report of the agents (docs/opo/TASKS.md, ОПО-5.6): by agents, projects, skills, tiers, models and who
asked; failures by reason; CSV for Excel; a person sees only his own agents."""
import datetime as dt

from fastapi.testclient import TestClient

from app import accounts
from app.db import SessionLocal, User
from app.net import agents, spend
from app.net.models import NetSpend


def _today():
    return dt.datetime.now(dt.timezone.utc).date()


def _row(agent_id, days_ago=0, **kw):
    day = (_today() - dt.timedelta(days=days_ago)).isoformat()
    base = {"day": day, "agent_id": agent_id, "tier": "mid", "model": "google/gemini-2.5-flash", "project": "24-017",
            "skill": "plc-io-from-excel", "prompt_tokens": 1000, "completion_tokens": 200, "cached_tokens": 400,
            "cost": 0.05, "ok": True, "error": "", "role": ""}
    return NetSpend(**{**base, **kw})


def _ivan_with_agent():
    accounts.register("ivan", "correct-horse")
    with SessionLocal() as s:
        ivan = s.query(User).filter_by(username="ivan").one()
        ivan.status = "approved"
        s.commit()
        ivan_id = ivan.id
    return agents.create(ivan_id, "opo-ivan", None, None, "hermes", "viewer", by="test")


def test_the_report_sums_by_everything(client, net_agent):
    ivan_agent = _ivan_with_agent()
    with SessionLocal() as s:
        s.add_all([_row(net_agent["id"]), _row(net_agent["id"], 2, tier="strong", model="anthropic/claude-sonnet-4.5",
                                               cost=0.2, role="workers", project="", skill=""),
                   _row(net_agent["id"], 1, cost=0.0, ok=False, error="budget_exceeded: Дневной бюджет агента $1 исчерпан",
                        prompt_tokens=0, completion_tokens=0, cached_tokens=0),
                   _row(ivan_agent.id, 0, cost=0.01), _row(net_agent["id"], 40, cost=9.0)])
        s.commit()
    r = spend.report(None, 7)
    assert r["days"] == 7 and len(r["daily"]) == 7 and abs(r["total"]["cost"] - 0.26) < 1e-9
    assert r["total"]["requests"] == 4 and r["total"]["errors"] == 1
    by_agent = {x["label"]: x for x in r["groups"]["agent"]}
    assert by_agent["opo-sysai"]["share"] == 96 and by_agent["opo-ivan"]["owner"] == "ivan"
    assert by_agent["opo-sysai"]["href"] == f"/agents/{net_agent['id']}" and "budget_pct" in by_agent["opo-sysai"]
    assert {x["label"] for x in r["groups"]["project"]} == {"24-017", "без проекта"}
    assert [x["label"] for x in r["groups"]["role"]][0] == "исполнитель"
    assert r["groups"]["tier"][0]["label"].startswith("sysai/strong")
    assert r["failures"] == [{"code": "budget_exceeded", "ru": "бюджет исчерпан", "count": 1,
                              "example": "Дневной бюджет агента $1 исчерпан"}]
    assert r["total"]["cache_pct"] == round(100 * 1200 / 3000)
    assert spend.report(None, 90)["total"]["cost"] > 9 and spend.period("13") == 7
    mine = spend.report([ivan_agent.id], 30)
    assert mine["total"]["requests"] == 1 and [x["label"] for x in mine["groups"]["agent"]] == ["opo-ivan"]


def test_the_page_and_the_csv(client, net_agent):
    ivan_agent = _ivan_with_agent()
    with SessionLocal() as s:
        s.add_all([_row(net_agent["id"], cost=0.123456), _row(ivan_agent.id, cost=0.5, project="25-001")])
        s.commit()
    page = client.get("/agents/spend?days=30").text
    assert "Расходы агентов" in page and "opo-sysai" in page and "opo-ivan" in page and "25-001" in page
    assert 'href="/agents/spend"' in page and 'aria-current="page"' in page.split('href="/agents/spend"')[1][:120]
    one = client.get(f"/agents/spend?agent={net_agent['id']}").text
    assert "opo-sysai" in one and "25-001" not in one
    csv = client.get("/agents/spend.csv?days=7")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    text = csv.content.decode("utf-8")
    assert text.startswith("\ufeffдень;агент;проект") and "opo-ivan" in text and "0,123456" in text
    assert "attachment" in csv.headers["content-disposition"]
    assert 'href="/agents/spend?agent=' in client.get(f"/agents/{net_agent['id']}").text
    from app.main import app
    with TestClient(app) as c:  # a person sees only his own agents, and no foreign one
        c.post("/login", data={"username": "ivan", "password": "correct-horse"})
        page = c.get("/agents/spend?days=30").text
        assert "opo-ivan" in page and "opo-sysai" not in page and "25-001" in page and "24-017" not in page
        assert c.get(f"/agents/spend?agent={net_agent['id']}").status_code == 404
        assert c.get(f"/agents/spend.csv?agent={net_agent['id']}").status_code == 404
        assert "opo-sysai" not in c.get("/agents/spend.csv?days=30").content.decode("utf-8")


def test_an_empty_period_says_so(client, net_on):
    page = client.get("/agents/spend?days=1").text
    assert "запросов к нейронкам не было" in page and "$0.00" in page


def test_the_tables_fit_their_cards_without_bars(client, net_agent):
    with SessionLocal() as s:
        s.add(_row(net_agent["id"], cost=1.0))
        s.commit()
    page = client.get("/agents/spend").text
    assert "sp-bar" not in page and "Размер запроса" in page  # the share is a column; no stripes under the names
    css = client.get("/static/net.css").text
    assert ".sp-grid .table-wrap" in css and "overflow-x: visible" in css  # no scrollbar under a table of a card


def test_a_sum_is_split_into_fresh_input_cache_reads_and_the_answer(client, net_agent, monkeypatch):
    """One number per request comes from the provider; the page says what it is made of by the prices of the model."""
    from app.net import gateway
    prices = {"google/gemini-2.5-flash": (1.0, 0.1, 5.0), "anthropic/claude-sonnet-4.5": (3.0, 3.0, 15.0)}   # in, cache read, out
    monkeypatch.setattr(gateway, "price_parts", lambda model: prices.get(model))
    with SessionLocal() as s:
        # 600 fresh + 400 cached + 200 of the answer at 1 / 0.1 / 5 → 600 : 40 : 1000
        s.add_all([_row(net_agent["id"], cost=0.164),
                   _row(net_agent["id"], model="anthropic/claude-sonnet-4.5", project="25-001", cost=0.6, cached_tokens=0),
                   _row(net_agent["id"], model="some/unknown", project="25-001", cost=0.05)])
        s.commit()
    r = spend.report(None, 7)
    by = {x["label"]: x for x in r["groups"]["project"]}
    one = by["24-017"]
    assert (round(one["cost_in"], 4), round(one["cost_cache"], 4), round(one["cost_out"], 4)) == (0.06, 0.004, 0.1)
    two = by["25-001"]   # 1000 x 3 + 200 x 15 → half and half of the 0.6; the model without a price stays apart
    assert (round(two["cost_in"], 4), two["cost_cache"], round(two["cost_out"], 4), two["cost_unknown"]) == (0.3, 0.0, 0.3, 0.05)
    total = r["total"]
    assert abs(total["cost_in"] + total["cost_cache"] + total["cost_out"] + total["cost_unknown"] - total["cost"]) < 1e-6
    page = client.get("/agents/spend?days=7").text
    assert "Из чего сложилась сумма" in page and "Вход / кэш / ответ" in page and "модели без цены в каталоге" in page
    text = client.get("/agents/spend.csv?days=7").content.decode("utf-8")
    assert "из неё вход, $;чтение кэша, $;ответ, $" in text and "0,060000;0,004000;0,100000" in text
    assert spend.split([("google/gemini-2.5-flash", 0, 10, 10, 0)]) == {"in": 0.0, "cache": 0.0, "out": 0.0, "unknown": 0.0}


def test_the_catalog_gives_the_price_of_a_cached_token(monkeypatch):
    from app.net import gateway

    class Reply:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "a/with-cache", "pricing": {"prompt": "0.000002", "completion": "0.00001", "input_cache_read": "0.0000002"}},
                             {"id": "b/no-cache", "pricing": {"prompt": "0.000001", "completion": "0.000004"}}]}
    monkeypatch.setattr(gateway, "_get", lambda *a, **k: Reply())
    monkeypatch.setitem(gateway._state, "prices", {})
    monkeypatch.setitem(gateway._state, "prices_at", 0.0)
    monkeypatch.setitem(gateway._state, "prices_failed", 0.0)
    assert gateway.price_parts("a/with-cache") == (0.000002, 0.0000002, 0.00001)
    assert gateway.price_parts("b/no-cache") == (0.000001, 0.000001, 0.000004)      # no cache price: the full one
    assert gateway.price_parts("c/unknown") is None
