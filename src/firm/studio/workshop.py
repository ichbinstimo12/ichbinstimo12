"""Werkstatt des Studios: Idee -> Entwurf -> Pruefung -> Shop-Text -> wartet auf deine Freigabe.

Drei Rollen, alle ueber dasselbe LLM-Interface wie die Agenten (Brain-Zone, kein Zugriff auf Geld):
  StudioResearcher  waehlt das Thema innerhalb der Nische, die der Code aus echten Verkaeufen bestimmt
  StudioBuilder     schreibt das Produkt (Markdown) und den Shop-Text
  StudioAuditor     prueft wie ein Red Team (Fehler, Nutzen, Versprechen, Urheberrecht)

Was der Code entscheidet, nicht das LLM: welche Nische (mehr von dem, was sich verkauft), wie viele Produkte,
Preisrahmen, Budget, ob ein Entwurf die deterministische Pruefung besteht. Jeder Schritt ist ein Ereignis.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.firm.agents.actions import extract_json
from src.firm.studio import checks, costs, package
from src.firm.studio.catalog import Catalog

logger = logging.getLogger("Studio")

RESEARCHER, BUILDER, AUDITOR = "StudioResearcher", "StudioBuilder", "StudioAuditor"
MAX_REVISIONS = 1
EXPLORE_EVERY = 3            # jede 3. Idee in einer wenig getesteten Nische, sonst die mit dem meisten Umsatz

IDEA_SYSTEM = """You are the market researcher of a tiny digital-products studio. You propose ONE practical digital
product that real people would pay a few dollars for. No hype, no income promises, no medical/legal/tax/investment
advice, no brand names. Answer with ONE JSON object only:
{"title": str (max 70 chars), "audience": str, "problem": str, "outline": [6-10 section titles], "price_usd": number,
 "why": str (why this audience needs it)}"""

DRAFT_SYSTEM = """You write complete, genuinely useful digital products in Markdown. Rules: start with '# Title',
use '##' sections, concrete steps, checklists ('- [ ] ...') and tables where helpful. No links, no placeholders,
no personal data, no promises of income or success, no medical/legal/tax/investment advice, no brand names.
Write the whole product, not a summary. Output Markdown only."""

AUDIT_SYSTEM = """You are a strict quality auditor (red team) for a digital product before it is sold. Check:
factual errors, padding/filler, missing practical value, misleading promises, copied or brand content, unsafe
advice. Answer with ONE JSON object only: {"score": 1-10, "pass": bool, "issues": [str]}.
Pass only if score >= 7 and nothing misleading or unsafe remains."""

LISTING_SYSTEM = """You write an honest shop listing for a digital product. No hype, no promises. Answer with ONE
JSON object only: {"title": str (max 70 chars), "description": str (max 600 chars), "tags": [max 8 short tags]}"""


class Workshop:
    def __init__(self, llm, catalog: Catalog, event_store, cfg: Dict[str, Any], limits: Dict[str, Any],
                 pkg_root: Path, firm_token_budget: Optional[int] = None,
                 now_fn: Callable[[], datetime] = datetime.utcnow):
        self.llm, self.catalog, self.store = llm, catalog, event_store
        self.cfg, self.limits, self.pkg_root = cfg, limits, Path(pkg_root)
        self.firm_token_budget, self.now = firm_token_budget, now_fn
        self._paused_reason: Optional[str] = None

    # ------------------------------------------------------------------ guards
    def blocked(self) -> Optional[str]:
        """Warum das Studio gerade nichts Neues anfaengt (None = darf)."""
        lim = self.limits
        if not lim.get("enabled", True):
            return "Studio in limits.toml abgeschaltet"
        spent = costs.ai_cost(self.store, self.cfg.get("llm_prices", {}), costs.month_start(self.now()), "Studio")["usd"]
        if spent >= lim["max_monthly_ai_usd"]:
            return f"KI-Budget des Monats erreicht ({spent:.2f}/{lim['max_monthly_ai_usd']:.2f} USD)"
        if self.firm_token_budget:
            from src.firm.agents.metrics import tokens_today
            if tokens_today(self.store) >= self.firm_token_budget:
                return "Token-Budget der Firma fuer heute aufgebraucht"
        waiting = self.catalog.count(["AWAITING_APPROVAL", "APPROVED"])
        if waiting >= lim["max_awaiting_approval"]:
            return f"{waiting} Produkte warten auf dich (Freigabe/Hochladen) - Studio pausiert"
        return None

    def new_allowed(self) -> bool:
        week = (self.now() - timedelta(days=7)).isoformat()
        return self.catalog.created_since(week) < self.limits["max_products_per_week"]

    # ------------------------------------------------------------------ loop step
    async def tick(self) -> Optional[str]:
        """Ein Arbeitsschritt. -> was getan wurde (fuer Log/Test) oder None."""
        why = self.blocked()
        if why:
            if why != self._paused_reason:
                self.store.append("STUDIO", "STUDIO_PAUSED", {"reason": why})
                self._paused_reason = why
            return None
        self._paused_reason = None
        for p in self.catalog.list(["DRAFTED"]):
            return await self.audit(p)
        for p in self.catalog.list(["IDEA"]):
            return await self.draft(p)
        for p in self.catalog.list(["AUDIT_FAILED"]):
            if int(p["revisions"]) < MAX_REVISIONS and not p["data"].get("final_reject"):
                return await self.draft(p, revise=True)
        if self.new_allowed():
            return await self.idea()
        return None

    # ------------------------------------------------------------------ niche choice (code, from real sales)
    def niche_stats(self) -> Dict[str, Dict[str, float]]:
        niches = list(self.cfg["product"]["niches"]) or ["Allgemein"]
        stats = {n: {"products": 0, "published": 0, "revenue": 0.0} for n in niches}
        for p in self.catalog.list(limit=10_000):
            s = stats.setdefault(p.get("niche") or "?", {"products": 0, "published": 0, "revenue": 0.0})
            s["products"] += 1
            if p["status"] in ("PUBLISHED", "RETIRED"):
                s["published"] += 1
        for e in self.store.get_events(types=["STUDIO_SALE"]):
            pay = json.loads(e.get("payload_json") or "{}")
            prod = self.catalog.by_platform_ref(str(pay.get("product_ref") or ""))
            if prod and isinstance(pay.get("net_usd"), (int, float)) and not pay.get("refunded"):
                stats.setdefault(prod["niche"], {"products": 0, "published": 0, "revenue": 0.0})
                stats[prod["niche"]]["revenue"] += float(pay["net_usd"])
        return stats

    def choose_niche(self) -> str:
        stats = {n: s for n, s in self.niche_stats().items() if n in (self.cfg["product"]["niches"] or ["Allgemein"])}
        total = sum(s["products"] for s in stats.values())
        sold = {n: s["revenue"] / max(s["published"], 1) for n, s in stats.items() if s["revenue"] > 0}
        if sold and total % EXPLORE_EVERY != EXPLORE_EVERY - 1:
            return max(sold, key=sold.get)
        return min(stats, key=lambda n: (stats[n]["products"], n))

    # ------------------------------------------------------------------ LLM steps
    async def _ask(self, role: str, system: str, user: str, max_tokens: int, json_mode: bool,
                   product_id: Optional[str]) -> str:
        resp = await self.llm.generate(system, [{"role": "user", "content": user}], max_tokens=max_tokens,
                                       json_mode=json_mode)
        usage = resp.usage or {}
        tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        cost = costs.call_cost(resp.model, tin, tout, self.cfg.get("llm_prices", {}))
        self.store.append(role, "RESULT", {"status": "studio", "task": role, "model": resp.model,
                                           "provider": resp.provider, "tokens": tin + tout, "input_tokens": tin,
                                           "output_tokens": tout, "cost_usd": round(cost, 6),
                                           "product_id": product_id}, correlation_id=product_id)
        return resp.content or ""

    async def idea(self) -> str:
        niche = self.choose_niche()
        prod = self.cfg["product"]
        n = self.catalog.created_since("0000")
        fmt = prod["formats"][n % len(prod["formats"])]
        lang = prod["languages"][n % len(prod["languages"])]
        existing = [p["title"] for p in self.catalog.list(limit=500)]
        user = (f"Niche: {niche}\nFormat: {fmt}\nLanguage: {lang}\nPrice range: {self.limits['min_price_usd']}-"
                f"{self.limits['max_price_usd']} USD\nAlready made (do not repeat): {existing[:30]}\n"
                f"Sales so far by niche (net USD): { {k: round(v['revenue'], 2) for k, v in self.niche_stats().items()} }")
        raw = await self._ask(RESEARCHER, IDEA_SYSTEM, user, 900, True, None)
        obj = extract_json(raw) or {}
        title = str(obj.get("title") or "").strip()[:90]
        if title.lower() in {t.lower() for t in existing}:
            self.store.append(RESEARCHER, "STUDIO_ERROR", {"step": "idea", "error": f"Titel existiert schon: {title}"})
            return "idea_duplicate"
        if not title or not isinstance(obj.get("outline"), list):
            self.store.append(RESEARCHER, "STUDIO_ERROR", {"step": "idea", "error": "keine gueltige Idee", "raw": raw[:300]})
            return "idea_invalid"
        price = float(obj.get("price_usd") or self.limits["min_price_usd"])
        price = min(max(price, self.limits["min_price_usd"]), self.limits["max_price_usd"])
        self.catalog.create(title, niche, fmt, lang, round(price, 2),
                            {k: obj.get(k) for k in ("audience", "problem", "outline", "why")}, RESEARCHER)
        return "idea"

    async def draft(self, p: Dict[str, Any], revise: bool = False) -> str:
        d = p["data"]
        user = (f"Product: {p['title']}\nLanguage: {p['language']}\nFormat: {p['format']}\nAudience: {d.get('audience')}\n"
                f"Problem: {d.get('problem')}\nSections: {d.get('outline')}\n"
                f"Minimum length: {self.cfg['product']['min_words']} words.")
        if revise:
            user += "\nFix ALL of these review findings:\n- " + "\n- ".join(d.get("issues") or [])
        body = await self._ask(BUILDER, DRAFT_SYSTEM, user, 7000, False, p["id"])
        body = body.strip().removeprefix("```markdown").removeprefix("```").removesuffix("```").strip()
        if not body:
            self.store.append(BUILDER, "STUDIO_ERROR", {"step": "draft", "product_id": p["id"], "error": "leer"})
            return "draft_empty"
        self.catalog.transition(p["id"], "DRAFTED", BUILDER, data={"body": body, "words": checks.word_count(body)},
                                note="ueberarbeitet" if revise else "")
        return "draft"

    async def audit(self, p: Dict[str, Any]) -> str:
        body = p["data"].get("body") or ""
        issues = checks.check_draft(p["title"], body, p["price_usd"], self.limits, self.cfg["product"]["min_words"])
        if not issues:
            raw = await self._ask(AUDITOR, AUDIT_SYSTEM, f"Title: {p['title']}\n\n{body[:60000]}", 900, True, p["id"])
            verdict = extract_json(raw) or {}
            score = verdict.get("score")
            if not (verdict.get("pass") is True and isinstance(score, (int, float)) and score >= 7):
                issues = [str(i) for i in (verdict.get("issues") or [])][:8] or [f"Pruefer-Ergebnis ungueltig/zu schwach: {raw[:120]}"]
            else:
                p["data"]["audit"] = {"score": score, "issues": verdict.get("issues") or []}
        if issues:
            final = int(p["revisions"]) >= MAX_REVISIONS
            self.catalog.transition(p["id"], "AUDIT_FAILED", AUDITOR, note="; ".join(issues)[:500],
                                    data={"issues": issues, "final_reject": final})
            return "audit_failed"
        raw = await self._ask(BUILDER, LISTING_SYSTEM, f"Product: {p['title']}\nLanguage: {p['language']}\n\n{body[:6000]}",
                              700, True, p["id"])
        listing = extract_json(raw) or {}
        listing = {"title": str(listing.get("title") or p["title"])[:90],
                   "description": str(listing.get("description") or "")[:600],
                   "tags": [str(t)[:30] for t in (listing.get("tags") or [])][:8]}
        shop_text = f"{listing['title']}\n{listing['description']}".lower()
        if any(re.search(pat, shop_text) for pat in checks.FORBIDDEN_CLAIMS) or \
                any(re.search(rf"\b{re.escape(m)}\b", shop_text) for m in checks.TRADEMARKS):
            listing = {"title": p["title"], "description": "", "tags": listing["tags"]}   # du schreibst ihn dann selbst
        p = self.catalog.transition(p["id"], "AWAITING_APPROVAL", AUDITOR,
                                    note=f"Pruefung bestanden (Score {p['data'].get('audit', {}).get('score')})",
                                    data={"listing": listing, "audit": p["data"].get("audit")})
        package.build(p, self.pkg_root)
        return "awaiting_approval"
