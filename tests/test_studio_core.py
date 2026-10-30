"""Studio-Grundlagen: Zustandsmaschine (nur du gibst frei), Qualitaetspruefung, Paket, Grenzen."""
import zipfile
import io

import pytest

from src.firm.bus.event_store import EventStore
from src.firm.studio import checks, package
from src.firm.studio.catalog import Catalog, TransitionError
from src.firm.studio.config import DEFAULT_LIMITS, load_limits, load_treasury

GOOD_BODY = "# Lernplan\n\n## Woche 1\n- [ ] Kapitel lesen\n- [ ] Karten schreiben\n\n## Woche 2\n" + \
            ("Wiederholen und ueben mit klaren Schritten. " * 300) + "\n\n## Pruefung\n1. Ruhig bleiben\n| A | B |\n|---|---|\n| 1 | 2 |\n"


def cat(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    return Catalog(str(tmp_path / "e.db"), store), store


def test_studio_can_only_reach_awaiting_approval_operator_decides(tmp_path):
    c, store = cat(tmp_path)
    p = c.create("Lernplan Lehrabschluss", "Lehre", "Planer", "de", 9.0, {"why": "Nachfrage"}, "MarketResearcher")
    c.transition(p["id"], "DRAFTED", "ProductBuilder", data={"body": GOOD_BODY})
    c.transition(p["id"], "AWAITING_APPROVAL", "QualityAuditor")
    with pytest.raises(TransitionError):
        c.transition(p["id"], "APPROVED", "CEO")                    # kein Agent darf freigeben
    c.transition(p["id"], "APPROVED", "OPERATOR", operator=True)
    with pytest.raises(TransitionError):
        c.transition(p["id"], "AWAITING_APPROVAL", "OPERATOR", operator=True)   # kein Rueckweg
    done = c.transition(p["id"], "PUBLISHED", "OPERATOR", operator=True, platform_ref="abc123")
    assert done["status"] == "PUBLISHED" and done["platform_ref"] == "abc123"
    assert c.by_platform_ref("abc123")["id"] == p["id"]
    types = [e["type"] for e in store.get_events(correlation_id=p["id"])]
    assert types == ["STUDIO_IDEA", "STUDIO_DRAFTED", "STUDIO_AWAITING_APPROVAL", "STUDIO_APPROVED", "STUDIO_PUBLISHED"]


def test_audit_failed_counts_revisions(tmp_path):
    c, _ = cat(tmp_path)
    p = c.create("X Guide", "n", "f", "de", 5, {}, "A")
    c.transition(p["id"], "DRAFTED", "B", data={"body": "x"})
    c.transition(p["id"], "AUDIT_FAILED", "C", note="zu kurz")
    assert c.transition(p["id"], "DRAFTED", "B")["revisions"] == 1
    assert c.count(["DRAFTED"]) == 1


def test_checks_pass_good_draft_and_catch_problems():
    lim = dict(DEFAULT_LIMITS)
    assert checks.check_draft("Lernplan fuer die LAP", GOOD_BODY, 9.0, lim) == []
    bad = "# A\nGarantiert reich werden! Schreib an max@example.com oder +41 79 123 45 67. [Insert name] https://x.y\n"
    issues = " | ".join(checks.check_draft("Notion Planer", bad, 500, lim))
    for part in ("zu kurz", "Struktur", "Preis", "garantiert", "fremde Marke", "Platzhalter", "persoenliche", "Links"):
        assert part in issues, part


def test_package_has_files_ai_note_and_escapes_html(tmp_path):
    c, _ = cat(tmp_path)
    p = c.create("Planer <script>", "n", "f", "de", 7, {}, "A")
    p = c.transition(p["id"], "DRAFTED", "B", data={"body": GOOD_BODY + "\n<script>alert(1)</script>",
                                                   "listing": {"description": "Kurz", "tags": ["lehre"]}})
    folder = package.build(p, tmp_path / "pkg")
    names = sorted(f.name for f in folder.iterdir())
    assert names == ["UPLOAD.md", "listing.txt", "product.html", "product.md", "product.pdf"]
    assert (folder / "product.pdf").read_bytes()[:5] == b"%PDF-" and "product.pdf ist fertig" in (folder / "UPLOAD.md").read_text(encoding="utf-8")
    html_text = (folder / "product.html").read_text(encoding="utf-8")
    assert "<script>" not in html_text and "KI-Unterstützung" in html_text and "<table>" in html_text
    assert "KI-Unterstützung" in (folder / "listing.txt").read_text(encoding="utf-8")
    z = zipfile.ZipFile(io.BytesIO(package.zip_bytes(folder)))
    assert sorted(z.namelist()) == names


def test_limits_default_and_ceiling(tmp_path):
    assert load_limits(tmp_path / "missing.toml") == DEFAULT_LIMITS
    f = tmp_path / "l.toml"
    f.write_text("[studio]\nmax_products_per_week = 500\nmax_monthly_ai_usd = 5\n")
    lim = load_limits(f)
    assert lim["max_products_per_week"] == 14 and lim["max_monthly_ai_usd"] == 5.0
    t = tmp_path / "t.toml"
    t.write_text("[policy]\ntax_reserve_pct = 5\ntrading_topup_pct = -1\n")
    tr = load_treasury(t)
    assert tr["tax_reserve_pct"] == 0.9 and tr["trading_topup_pct"] == 0.0


def test_pdf_survives_umlauts_typography_and_long_words(tmp_path):
    md = "# Lernplan für Lehrlinge – „Prüfung“ ✓\n\n## Übersicht\n- [ ] Größe…\n" + "Donaudampfschifffahrtsgesellschaftskapitän" * 8 + "\n| a | b |\n|---|---|\n| ü | € |\n"
    assert package.markdown_to_pdf(md, "Lernplan", "Hinweis", tmp_path / "x.pdf")
    assert (tmp_path / "x.pdf").stat().st_size > 800
