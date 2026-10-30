"""Veroeffentlichungs-Paket: alles, was du zum Hochladen brauchst (5 Minuten pro Produkt).

  product.md     das Produkt im Rohformat
  product.pdf    fertige Datei zum Hochladen (fpdf2; fehlt es, bleibt product.html zum Selbst-Drucken)
  product.html   Fassung fuer den Browser
  listing.txt    Titel, Preis, Beschreibung, Schlagworte zum Kopieren in den Shop
  UPLOAD.md      Schritt-fuer-Schritt-Anleitung
  marketing.txt  ehrliche Posts fuer deine eigenen Kanaele + Ideen (die Firma postet nie selbst)
  leseprobe.pdf  kostenloser Auszug zum Teilen (macht neugierig, kostet nichts)
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Dict

AI_NOTE = {"de": "Hinweis: Dieses Produkt wurde mit KI-Unterstützung erstellt und von einem Menschen geprüft.",
           "en": "Note: This product was created with AI assistance and reviewed by a human."}

CSS = """body{font-family:Georgia,serif;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.55;color:#1f2933}
h1{font-size:28px;border-bottom:3px solid #2563eb;padding-bottom:6px}h2{font-size:21px;margin-top:32px;color:#1e3a8a}
h3{font-size:17px}li{margin:4px 0}.box{display:inline-block;width:12px;height:12px;border:1.5px solid #333;
margin-right:6px;vertical-align:-1px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #cbd2d9;
padding:6px}footer{margin-top:40px;font-size:12px;color:#616e7c}@media print{body{margin:0}}"""


def _inline(t: str) -> str:
    t = html.escape(t)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)


def markdown_to_html(md: str, title: str, footer: str) -> str:
    out, in_list, in_table = [], None, False

    def close():
        nonlocal in_list, in_table
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None
        if in_table:
            out.append("</table>")
            in_table = False

    for line in (md or "").splitlines():
        s = line.rstrip()
        m = re.match(r"^(#{1,3}) (.+)", s)
        if m:
            close()
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            continue
        m = re.match(r"^\s*[-*] \[[ xX]\] (.+)", s)
        if m or re.match(r"^\s*[-*] (.+)", s):
            item = m.group(1) if m else re.match(r"^\s*[-*] (.+)", s).group(1)
            if in_list != "ul":
                close()
                out.append("<ul>")
                in_list = "ul"
            out.append(f"<li>{'<span class=box></span>' if m else ''}{_inline(item)}</li>")
            continue
        m = re.match(r"^\s*\d+[.)] (.+)", s)
        if m:
            if in_list != "ol":
                close()
                out.append("<ol>")
                in_list = "ol"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if not in_table:
                close()
                out.append("<table>")
                in_table = True
            out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        close()
        if s.strip():
            out.append(f"<p>{_inline(s)}</p>")
    close()
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>{''.join(out)}"
            f"<footer>{html.escape(footer)}</footer></body></html>")


# Die Standard-PDF-Schriften kennen nur Latin-1: typografische Zeichen ersetzen, Rest als "?".
_PDF_MAP = {"\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
            "\u201e": '"', "\u2026": "...", "\u2022": "-", "\u2713": "x", "\u2714": "x", "\u2192": "->",
            "\u00a0": " ", "\u2264": "<=", "\u2265": ">=", "\u20ac": "EUR"}


def _latin1(t: str) -> str:
    t = "".join(_PDF_MAP.get(ch, ch) for ch in t)
    return t.encode("latin-1", "replace").decode("latin-1")


def markdown_to_pdf(md: str, title: str, footer: str, path: Path) -> bool:
    """Druckfertiges PDF ohne Handarbeit. -> False, wenn fpdf2 fehlt (dann bleibt die HTML-Fassung)."""
    try:
        from fpdf import FPDF
        from fpdf.fonts import FontFace
    except ImportError:
        return False

    class Doc(FPDF):
        def footer(self):
            self.set_y(-14)
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(110)
            self.cell(0, 8, _latin1(f"{title}  -  Seite {self.page_no()}"), align="C")

    pdf = Doc(format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_margins(20, 18, 20)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def text(t: str, size: float = 11, style: str = "", indent: float = 0, gap: float = 1.5):
        pdf.set_font("Helvetica", style, size)
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(width - indent, size * 0.5, _latin1(re.sub(r"\*\*(.+?)\*\*", r"\1", t)))
        pdf.ln(gap)

    rows: list = []

    def flush_table():
        if not rows:
            return
        pdf.set_font("Helvetica", "", 10)
        ncol = max(len(r) for r in rows)
        with pdf.table(width=width, line_height=6, first_row_as_headings=True,
                       headings_style=FontFace(emphasis="BOLD", fill_color=(226, 232, 240))) as tbl:
            for r in rows:
                row = tbl.row()
                for c in r + [""] * (ncol - len(r)):
                    row.cell(_latin1(c))
        pdf.ln(3)
        rows.clear()

    for line in (md or "").splitlines():
        s = line.rstrip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                rows.append([re.sub(r"\*\*(.+?)\*\*", r"\1", c) for c in cells])
            continue
        flush_table()
        m = re.match(r"^(#{1,3}) (.+)", s)
        if m:
            n = len(m.group(1))
            pdf.ln(3 if n > 1 else 0)
            pdf.set_text_color(30, 58, 138) if n > 1 else pdf.set_text_color(20, 20, 20)
            text(m.group(2), {1: 20, 2: 15, 3: 12.5}[n], "B", gap=2.5)
            pdf.set_text_color(31, 41, 51)
            continue
        m = re.match(r"^\s*[-*] \[[ xX]\] (.+)", s)
        if m:
            text("[  ]  " + m.group(1), indent=4, gap=0.8)
            continue
        m = re.match(r"^\s*[-*] (.+)", s)
        if m:
            text("-  " + m.group(1), indent=4, gap=0.8)
            continue
        m = re.match(r"^\s*(\d+)[.)] (.+)", s)
        if m:
            text(f"{m.group(1)}.  {m.group(2)}", indent=4, gap=0.8)
            continue
        if s.strip():
            text(s)
    flush_table()
    pdf.ln(4)
    text(footer, 8.5, "I")
    pdf.output(str(path))
    return True


def build(product: Dict[str, Any], root: Path) -> Path:
    """Schreibt das Paket nach root/<id>/ und liefert den Ordner."""
    d = product["data"]
    lang = product.get("language") or "de"
    note = AI_NOTE.get(lang, AI_NOTE["en"])
    body = d["body"].rstrip() + f"\n\n{note}\n"
    folder = Path(root) / product["id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "product.md").write_text(body, encoding="utf-8")
    (folder / "product.html").write_text(markdown_to_html(body, product["title"], note), encoding="utf-8")
    has_pdf = markdown_to_pdf(body, product["title"], note, folder / "product.pdf")
    listing = d.get("listing") or {}
    (folder / "listing.txt").write_text(
        f"TITEL:\n{listing.get('title') or product['title']}\n\nPREIS (USD):\n{product['price_usd']:.2f}\n\n"
        f"BESCHREIBUNG:\n{listing.get('description') or ''}\n\n{note}\n\n"
        f"SCHLAGWORTE:\n{', '.join(listing.get('tags') or [])}\n", encoding="utf-8")
    mk = d.get("marketing") or {}
    if mk:
        lines = ["MARKETING - zum Selbst-Posten auf DEINEN Kanaelen (die Firma postet nie selbst).",
                 "Regeln: nur ehrlich, keine gekauften Likes/Bewertungen, Plattformregeln beachten; bei bezahlter",
                 "Zusammenarbeit als Werbung kennzeichnen. Link zum Produkt selbst einfuegen.", ""]
        for i, post in enumerate(mk.get("posts") or [], 1):
            lines += [f"--- Post {i} ({post.get('channel')}) ---", post.get("text", ""), ""]
        if mk.get("hooks"):
            lines += ["--- Ideen fuer kurze Videos/Posts ---"] + [f"- {h}" for h in mk["hooks"]] + [""]
        (folder / "marketing.txt").write_text("\n".join(lines), encoding="utf-8")
        if mk.get("sample"):
            markdown_to_pdf(mk["sample"], f"Leseprobe: {product['title']}", note, folder / "leseprobe.pdf")
    (folder / "UPLOAD.md").write_text(
        "# So veroeffentlichst du dieses Produkt (ca. 5 Minuten)\n\n"
        + ("1. product.pdf ist fertig (zur Kontrolle einmal oeffnen)\n" if has_pdf else
           "1. product.html im Browser oeffnen -> Drucken -> \"Als PDF speichern\" -> product.pdf\n") +
        "2. Shop oeffnen (z. B. gumroad.com) -> New product -> Digital product\n"
        "3. Titel, Preis, Beschreibung und Schlagworte aus listing.txt kopieren\n"
        "4. product.pdf hochladen, Vorschau pruefen, veroeffentlichen\n"
        "5. Im Dashboard der Firma beim Produkt \"VEROEFFENTLICHT\" klicken und die Produkt-ID/Link eintragen "
        "(damit die Firma die Verkaeufe zuordnen kann)\n"
        "6. Optional, aber entscheidend fuer Verkaeufe: Posts aus marketing.txt auf deinen Kanaelen teilen, "
        "leseprobe.pdf als Gratis-Auszug anbieten\n", encoding="utf-8")
    return folder


def zip_bytes(folder: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path(folder).iterdir()):
            if f.is_file():
                z.write(f, f.name)
    return buf.getvalue()
