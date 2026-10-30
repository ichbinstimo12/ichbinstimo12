"""Veroeffentlichungs-Paket: alles, was du zum Hochladen brauchst (5 Minuten pro Produkt).

  product.md     das Produkt im Rohformat
  product.html   druckfertige Fassung (im Browser oeffnen -> Drucken -> "Als PDF speichern")
  listing.txt    Titel, Preis, Beschreibung, Schlagworte zum Kopieren in den Shop
  UPLOAD.md      Schritt-fuer-Schritt-Anleitung
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Dict

AI_NOTE = {"de": "Hinweis: Dieses Produkt wurde mit KI-Unterstuetzung erstellt und von einem Menschen geprueft.",
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
    listing = d.get("listing") or {}
    (folder / "listing.txt").write_text(
        f"TITEL:\n{listing.get('title') or product['title']}\n\nPREIS (USD):\n{product['price_usd']:.2f}\n\n"
        f"BESCHREIBUNG:\n{listing.get('description') or ''}\n\n{note}\n\n"
        f"SCHLAGWORTE:\n{', '.join(listing.get('tags') or [])}\n", encoding="utf-8")
    (folder / "UPLOAD.md").write_text(
        "# So veroeffentlichst du dieses Produkt (ca. 5 Minuten)\n\n"
        "1. product.html im Browser oeffnen -> Drucken -> \"Als PDF speichern\" -> product.pdf\n"
        "2. Shop oeffnen (z. B. gumroad.com) -> New product -> Digital product\n"
        "3. Titel, Preis, Beschreibung und Schlagworte aus listing.txt kopieren\n"
        "4. product.pdf hochladen, Vorschau pruefen, veroeffentlichen\n"
        "5. Im Dashboard der Firma beim Produkt \"VEROEFFENTLICHT\" klicken und die Produkt-ID/Link eintragen "
        "(damit die Firma die Verkaeufe zuordnen kann)\n", encoding="utf-8")
    return folder


def zip_bytes(folder: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path(folder).iterdir()):
            if f.is_file():
                z.write(f, f.name)
    return buf.getvalue()
