"""Studio-Ablauf mit Attrappen-LLM und Attrappen-Shop: Idee -> Entwurf -> Pruefung -> Freigabe-Warteschlange,
Budgets, Verkaeufe einmalig verbuchen, Schatzkammer empfiehlt nur - und nur bei PASS ins Trading."""
import asyncio
import json
from datetime import datetime

from src.firm.bus.event_store import EventStore
from src.firm.llm.base import LLMResponse
from src.firm.studio import sales, treasury
from src.firm.studio.catalog import Catalog
from src.firm.studio.config import DEFAULT_LIMITS, load_config
from src.firm.studio.workshop import Workshop

BODY = "# Lernplan LAP\n\n## Woche 1\n- [ ] Lesen\n\n## Woche 2\n" + ("Konkrete Schritte zum Ueben. " * 320) + "\n## Pruefung\nRuhig bleiben.\n"


class FakeLLM:
    provider_name, model = "anthropic", "claude-sonnet-5-5"

    def __init__(self, audit_pass=True, body=BODY):
        self.audit_pass, self.body, self.calls = audit_pass, body, []

    async def generate(self, system, messages, max_tokens=None, temperature=None, json_mode=False):
        self.calls.append(system[:20])
        if "market researcher" in system:
            out = json.dumps({"title": "Lernplan fuer die Lehrabschlusspruefung", "audience": "Lernende",
                              "problem": "Planlos", "outline": ["A", "B", "C", "D", "E", "F"], "price_usd": 500,
                              "why": "LAP"})
        elif "quality auditor" in system:
            out = json.dumps({"score": 8 if self.audit_pass else 3, "pass": self.audit_pass, "issues": [] if self.audit_pass else ["zu oberflaechlich"]})
        elif "OWN social channels" in system:
            out = json.dumps({"posts": [{"channel": "TikTok", "text": "Tipp: 3 Lernbloecke pro Woche reichen. Mein Plan dazu im Shop."},
                                        {"channel": "LinkedIn", "text": "Garantiert bestanden mit diesem Plan!"},
                                        {"channel": "Forum", "text": "Mehr unter https://spam.example"}],
                              "hooks": ["Mein Lernplan in 30 Sekunden"]})
        elif "shop listing" in system:
            out = json.dumps({"title": "Lernplan LAP", "description": "Garantiert bestanden!", "tags": ["lehre"]})
        else:
            out = self.body
        return LLMResponse(content=out, model=self.model, provider="anthropic",
                           usage={"input_tokens": 1000, "output_tokens": 2000})


def setup(tmp_path, llm, **lim):
    store = EventStore(str(tmp_path / "e.db"))
    cat = Catalog(str(tmp_path / "e.db"), store)
    cfg = load_config()
    w = Workshop(llm, cat, store, cfg, {**DEFAULT_LIMITS, **lim}, tmp_path / "pkg")
    return w, cat, store


def run(w, n):
    return [asyncio.run(w.tick()) for _ in range(n)]


def test_full_flow_ends_waiting_for_operator_with_package_and_costs(tmp_path):
    w, cat, store = setup(tmp_path, FakeLLM())
    assert run(w, 3) == ["idea", "draft", "awaiting_approval"]
    p = cat.list(["AWAITING_APPROVAL"])[0]
    assert p["price_usd"] == DEFAULT_LIMITS["max_price_usd"]              # LLM-Preis 500 begrenzt
    assert p["data"]["listing"]["description"] == ""                     # "Garantiert" im Shop-Text verworfen
    assert (tmp_path / "pkg" / p["id"] / "product.html").exists()
    res = [json.loads(e["payload_json"]) for e in store.get_events(types=["RESULT"])]
    assert all(r["cost_usd"] > 0 for r in res) and {e["agent_id"] for e in store.get_events(types=["RESULT"])} == \
        {"StudioResearcher", "StudioBuilder", "StudioAuditor"}


def test_failed_audit_gets_one_revision_then_stops(tmp_path):
    w, cat, _ = setup(tmp_path, FakeLLM(audit_pass=False), max_products_per_week=1)
    out = run(w, 6)
    assert out[:5] == ["idea", "draft", "audit_failed", "draft", "audit_failed"] and out[5] is None
    p = cat.list()[0]
    assert p["status"] == "AUDIT_FAILED" and p["data"]["final_reject"] is True


def test_short_draft_fails_deterministic_check_without_auditor_call(tmp_path):
    llm = FakeLLM(body="# Kurz\nzu wenig")
    w, cat, _ = setup(tmp_path, llm)
    assert run(w, 3) == ["idea", "draft", "audit_failed"]
    assert not any(c.startswith("You are a strict") for c in llm.calls)


def test_studio_pauses_for_budget_and_waiting_queue(tmp_path):
    w, cat, store = setup(tmp_path, FakeLLM(), max_awaiting_approval=1)
    run(w, 3)
    assert asyncio.run(w.tick()) is None and "warten auf dich" in json.loads(
        store.get_events(types=["STUDIO_PAUSED"])[-1]["payload_json"])["reason"]
    (tmp_path / "b").mkdir()
    w2, _, store2 = setup(tmp_path / "b", FakeLLM(), max_monthly_ai_usd=0.0001)
    run(w2, 1)
    assert asyncio.run(w2.tick()) is None and "KI-Budget" in json.loads(store2.get_events(types=["STUDIO_PAUSED"])[-1]["payload_json"])["reason"]


def test_niche_choice_follows_real_sales(tmp_path):
    w, cat, store = setup(tmp_path, FakeLLM())
    niches = w.cfg["product"]["niches"]
    p = cat.create("A", niches[2], "f", "de", 5, {}, "x")
    for to in ("DRAFTED", "AWAITING_APPROVAL"):
        cat.transition(p["id"], to, "x")
    cat.transition(p["id"], "APPROVED", "OP", operator=True)
    cat.transition(p["id"], "PUBLISHED", "OP", operator=True, platform_ref="g1")
    store.append("STUDIO", "STUDIO_SALE", {"sale_id": "s1", "product_ref": "g1", "net_usd": 8.0})
    assert w.choose_niche() == niches[2]


class FakeResp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeHTTP:
    def __init__(self, pages):
        self.pages, self.seen = pages, []

    async def get(self, url, params=None, headers=None, timeout=None):
        self.seen.append((params, headers))
        return FakeResp(self.pages[len(self.seen) - 1])


def test_sales_are_booked_once_and_refunds_separately(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    s1 = {"id": "a", "product_id": "g1", "product_name": "P", "price": 900, "gumroad_fee": 140, "currency": "usd",
          "refunded": False}
    http = FakeHTTP([{"success": True, "sales": [s1], "next_page_key": "k"},
                     {"success": True, "sales": [{"id": "b", "price": 500, "currency": "usd"}, {"bogus": 1}]},
                     {"success": True, "sales": [dict(s1, refunded=True)]}])
    sync = sales.SalesSync(store, http, token="tok")
    assert asyncio.run(sync.sync()) == {"new": 2, "refunds": 0, "skipped": 1}
    assert http.seen[1][0]["page_key"] == "k" and http.seen[0][1]["Authorization"] == "Bearer tok"
    assert asyncio.run(sync.sync()) == {"new": 0, "refunds": 1, "skipped": 0}
    rev = sales.revenue(store)
    assert rev["sales"] == 2 and rev["net_usd"] == 5.0          # 7.60 + 5.00 - 7.60 (Rueckerstattung)


def test_bad_api_answer_books_nothing(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    sync = sales.SalesSync(store, FakeHTTP([{"success": False}]), token="tok")
    assert asyncio.run(sync.sync())["error"] == 1 and not store.get_events(types=["STUDIO_SALE"])
    assert "tok" not in json.dumps([e["payload_json"] for e in store.get_events()])


def test_treasury_recommends_trading_only_with_pass_and_never_moves_money(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    for i in range(40):
        store.append("STUDIO", "STUDIO_SALE", {"sale_id": str(i), "net_usd": 10.0, "gross_usd": 11.0})
    store.append("StudioBuilder", "RESULT", {"tokens": 1000, "cost_usd": 2.0, "model": "claude-sonnet-5-5"})
    policy = {"tax_reserve_pct": 0.25, "runway_months": 3, "trading_topup_pct": 0.5, "require_pass_strategy": True,
              "min_transfer_usd": 50}
    r = treasury.report(store, {}, policy, {"verdicts": {"GOLD": {"verdict": "FORWARD_TEST"}}})
    assert r["profit_usd"] == 398.0 and r["tax_reserve_usd"] == 100.0 and r["runway_reserve_usd"] == 2.0
    assert r["free_usd"] == 296.0 and not r["trading_ready"]
    assert any("NICHT ins Trading" in x for x in r["recommendations"])
    r2 = treasury.report(store, {}, policy, {"verdicts": {"GOLD": {"verdict": "PASS"}}})
    assert any("148.00 USD aufs Trading-Konto" in x for x in r2["recommendations"])
    store.append("OPERATOR", "TREASURY_TRANSFER_RECORDED", {"usd": 148.0})
    assert treasury.report(store, {}, policy, {"verdicts": {"GOLD": {"verdict": "PASS"}}})["free_usd"] == 148.0
    rep = treasury.TreasuryReporter(store, {}, policy, lambda: {"verdicts": {"X": {"verdict": "PASS"}}})
    rep.tick(datetime(2026, 11, 1))
    rep.tick(datetime(2026, 11, 1, 12))
    assert len(store.get_events(types=["TREASURY_REPORT"])) == 1
    assert len(store.get_events(types=["TREASURY_RECOMMENDATION"])) == 1


def test_duplicate_title_is_not_made_twice(tmp_path):
    w, cat, _ = setup(tmp_path, FakeLLM())
    assert run(w, 4) == ["idea", "draft", "awaiting_approval", "idea_duplicate"]
    assert len(cat.list()) == 1


def test_bad_idea_backs_off_instead_of_burning_tokens(tmp_path):
    w, cat, _ = setup(tmp_path, FakeLLM())
    run(w, 3)
    calls = len(w.llm.calls)
    assert asyncio.run(w.tick()) == "idea_duplicate"
    assert asyncio.run(w.tick()) is None and len(w.llm.calls) == calls + 1     # Wartezeit: kein neuer LLM-Aufruf


def test_empty_drafts_give_up_after_three(tmp_path):
    w, cat, _ = setup(tmp_path, FakeLLM(body=""))
    assert run(w, 4) == ["idea", "draft_empty", "draft_empty", "draft_empty"]
    p = cat.list()[0]
    assert p["status"] == "AUDIT_FAILED" and p["data"]["final_reject"] is True


def test_auditor_score_as_text_is_understood(tmp_path):
    class TextScore(FakeLLM):
        async def generate(self, system, messages, **kw):
            r = await super().generate(system, messages, **kw)
            if "quality auditor" in system:
                r.content = json.dumps({"score": "8/10", "pass": "true", "issues": []})
            return r
    w, _, _ = setup(tmp_path, TextScore())
    assert run(w, 3)[-1] == "awaiting_approval"


def test_sale_matches_product_by_link_or_title(tmp_path):
    w, cat, _ = setup(tmp_path, FakeLLM())
    p = cat.create("Bewerbungs-Checkliste", "n", "f", "de", 5, {}, "x")
    for to in ("DRAFTED", "AWAITING_APPROVAL"):
        cat.transition(p["id"], to, "x")
    cat.transition(p["id"], "APPROVED", "OP", operator=True)
    cat.transition(p["id"], "PUBLISHED", "OP", operator=True, platform_ref="https://me.gumroad.com/l/bewerb")
    assert cat.match_sale({"product_ref": "zzz==", "product_permalink": "bewerb"})["id"] == p["id"]
    assert cat.match_sale({"product_ref": "yyy", "product_name": "Bewerbungs-Checkliste"})["id"] == p["id"]
    assert cat.match_sale({"product_ref": "nope", "product_name": "anderes"}) is None


def test_marketing_kit_is_honest_and_packaged(tmp_path):
    w, cat, _ = setup(tmp_path, FakeLLM())
    run(w, 3)
    p = cat.list(["AWAITING_APPROVAL"])[0]
    mk = p["data"]["marketing"]
    assert [x["channel"] for x in mk["posts"]] == ["TikTok"]          # Versprechen + Link aussortiert
    assert "Leseprobe" in mk["sample"] and len(mk["sample"].split()) < 400
    folder = tmp_path / "pkg" / p["id"]
    assert "Tipp: 3 Lernbloecke" in (folder / "marketing.txt").read_text(encoding="utf-8")
    assert (folder / "leseprobe.pdf").read_bytes()[:5] == b"%PDF-"


def test_unsold_published_products_are_flagged_once(tmp_path):
    from datetime import timedelta
    from src.firm.studio.review import review_products
    w, cat, store = setup(tmp_path, FakeLLM())
    ids = []
    for t in ("A Planer", "B Planer"):
        p = cat.create(t, "n", "f", "de", 5, {}, "x")
        for to in ("DRAFTED", "AWAITING_APPROVAL"):
            cat.transition(p["id"], to, "x")
        cat.transition(p["id"], "APPROVED", "OP", operator=True)
        cat.transition(p["id"], "PUBLISHED", "OP", operator=True, platform_ref="ref-" + t[0])
        ids.append(p["id"])
    store.append("STUDIO", "STUDIO_SALE", {"sale_id": "1", "product_ref": "ref-B", "net_usd": 4.0})
    later = datetime.utcnow() + timedelta(days=46)
    assert review_products(cat, store, datetime.utcnow()) == []
    assert review_products(cat, store, later) == [ids[0]]
    assert review_products(cat, store, later) == []
