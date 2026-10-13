#!/usr/bin/env python3
"""Bevakning av nya domar från Arbetsdomstolen.

För varje NY dom skickas ett mejl med:
  1) att domen kommit (AD-nummer, datum, länk)
  2) vilket/vilka fackförbund som är inblandade
  3) vilken fråga domen gäller

Hur det fungerar:
  - Listsidan https://arbetsdomstolen.se/sv/meddelade-domar/ ger AD-nummer,
    länk och (för refererade domar) en sammanfattning av frågan.
  - Domsidan länkar till domen som PDF. Vi läser PDF:ens första sidor och
    letar efter fackförbund och rubriken "Saken".
  - Förbunden hittas dels via en lista med kända förbund, dels via
    formuleringar som "X (förbundet)". Det är en heuristik: kontrollera
    gärna mot PDF:en i mejlet.

Första körningen sparar bara nuläget i seen.json (inga mejl).

Miljövariabler (GitHub Secrets/Variables):
  SMTP_HOST, SMTP_PORT (standard 587), SMTP_USER, SMTP_PASS
  MAIL_TO     mottagare (flera avgränsas med komma)
  MAIL_FROM   avsändare (standard = SMTP_USER)
  KEYWORDS    valfritt, kommaseparerat filter. Tomt = alla domar.
"""
import io
import json
import os
import re
import smtplib
import sys
from collections import Counter
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

URL = "https://arbetsdomstolen.se/sv/meddelade-domar/"
STATE = Path(__file__).parent / "seen.json"
HEADERS = {"User-Agent": "ad-bevakning/1.0"}
AD_RE = re.compile(r"AD\s+(\d{4})\s+nr\s+(\d+)", re.I)

# Kända fackförbund (utöka efter behov). Matchas med ordgränser, skiftlägeskänsligt.
KNOWN_UNIONS = [
    "Industrifacket Metall", "IF Metall", "Unionen", "Kommunal", "Handels",
    "Handelsanställdas förbund", "Byggnads", "Svenska Byggnadsarbetareförbundet",
    "Svenska Kommunalarbetareförbundet", "Seko", "SEKO", "Vision",
    "Fackförbundet ST", "Sveriges Ingenjörer", "Akademikerförbundet SSR",
    "Lärarförbundet", "Lärarnas Riksförbund", "Vårdförbundet",
    "Sveriges Läkarförbund", "Svenska Transportarbetareförbundet", "Transport",
    "Svenska Elektrikerförbundet", "Elektrikerna", "GS-facket", "GS",
    "Hotell- och restaurangfacket", "Fastighetsanställdas Förbund",
    "Svenska Livsmedelsarbetareförbundet", "Livs", "Pappers",
    "Svenska Målareförbundet", "Svenska Journalistförbundet",
    "Polisförbundet", "Svensk Pilotförening", "Svenska Pilotförbundet",
    "Finansförbundet", "Ledarna", "Jusek", "Naturvetarna", "Civilekonomerna",
    "Sveriges Skolledare", "Skolledarna", "Fysioterapeuterna",
    "Sveriges Arbetsterapeuter", "Sveriges Psykologförbund",
    "Svenska Teaterförbundet", "Teaterförbundet", "Svenska Musikerförbundet",
    "Försvarsförbundet", "Sjöbefälsföreningen", "Maskinbefälsförbundet",
    "Svensk Flygteknikerförening", "Svenska Lokförare", "Saco-S", "OFR",
    "Kyrkans Akademikerförbund", "Tull-Kust", "Sveriges Farmaceuter",
    "Svenska Brandmannaförbundet", "Fackligt Forum",
]
_union_res = [(u, re.compile(r"(?<![\wåäöÅÄÖ])" + re.escape(u) + r"(?![\wåäöÅÄÖ])")) for u in KNOWN_UNIONS]
DEFINED_RE = re.compile(
    r"\(\s*(?:förbundet|organisationen|föreningen|arbetstagarorganisationen|facket)\s*\)", re.I)
TRAILING_CAPS = re.compile(
    r"((?:[A-ZÅÄÖ][\wåäöÅÄÖ\-]*\s+){0,5}[A-ZÅÄÖ][\wåäöÅÄÖ\-]*)\s*$")


def get(url):
    r = requests.get(url, timeout=30, headers=HEADERS)
    r.raise_for_status()
    return r


def fetch_list():
    soup = BeautifulSoup(get(URL).text, "html.parser")
    out = []
    for h in soup.find_all("h3"):
        a = h.find("a", href=True)
        if not a:
            continue
        m = AD_RE.search(a.get_text(" ", strip=True))
        if not m:
            continue
        parts = []
        for sib in h.find_next_siblings():
            if sib.name in ("h1", "h2", "h3"):
                break
            parts.append(sib.get_text(" ", strip=True))
        out.append({
            "id": f"AD {m.group(1)} nr {m.group(2)}",  # AD-numret är nyckeln, inte datumet
            "title": a.get_text(" ", strip=True),
            "url": urljoin(URL, a["href"]),
            "summary": " ".join(p for p in parts if p).strip(),
        })
    return out


def find_pdf_text(page_url):
    """Returnerar (pdf_url, text) eller (None, '') om ingen PDF hittas."""
    soup = BeautifulSoup(get(page_url).text, "html.parser")
    links = [urljoin(page_url, a["href"]) for a in soup.find_all("a", href=True)
             if a["href"].lower().split("?")[0].endswith(".pdf")]
    if not links:
        return None, ""
    links.sort(key=lambda u: "ad-" not in u.lower())  # föredra filer med "ad-" i namnet
    pdf_url = links[0]
    reader = PdfReader(io.BytesIO(get(pdf_url).content))
    text = "\n".join((p.extract_text() or "") for p in reader.pages[:8])
    return pdf_url, text


def find_unions(text):
    flat = re.sub(r"\s+", " ", text)
    counts = Counter()
    for name, rx in _union_res:
        n = len(rx.findall(flat))
        if n:
            counts[name] += n
    found = [n for n, _ in counts.most_common(3)]
    # Komplettera med "X (förbundet)"-formuleringar för förbund som inte finns i listan
    for m in DEFINED_RE.finditer(flat):
        prefix = flat[max(0, m.start() - 80):m.start()]
        t = TRAILING_CAPS.search(prefix)
        if t:
            name = t.group(1).strip()
            if name and name.lower() not in {"bolaget", "staten"} and \
                    not any(name in f or f in name for f in found):
                found.append(name)
                break
    return found


def find_issue(text):
    flat = re.sub(r"\s+", " ", text)
    m = re.search(r"SAKEN\s*(.{5,300}?)\s*(?:PARTER|KLAGANDE|KÄRANDE|MOTPART|SVARANDE|DOMSLUT)",
                  flat, re.I)
    return m.group(1).strip() if m else ""


def find_date(text, url):
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+Mål nr", text) or re.search(r"/(\d{4}-\d{2}-\d{2})-ad-", url)
    return m.group(1) if m else ""


def enrich(j):
    try:
        pdf_url, text = find_pdf_text(j["url"])
    except Exception as e:  # nätverksfel, trasig PDF osv. ska inte stoppa mejlet
        print(f"Kunde inte läsa PDF för {j['id']}: {e}", file=sys.stderr)
        pdf_url, text = None, ""
    j["pdf"] = pdf_url
    j["unions"] = find_unions(text) if text else []
    j["saken"] = find_issue(text) if text else ""
    j["date"] = find_date(text, j["url"])
    return j


def matches(j, keywords):
    if not keywords:
        return True
    hay = " ".join([j["title"], j["summary"], j.get("saken", ""), " ".join(j.get("unions", []))]).lower()
    return any(k in hay for k in keywords)


def send_mail(j):
    msg = EmailMessage()
    unions = ", ".join(j["unions"]) if j["unions"] else "ej identifierat"
    msg["Subject"] = f"Ny AD-dom: {j['id']} – {unions}"
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    issue = j["summary"] or "Domen refereras inte (ingen sammanfattning publicerad)."
    lines = [
        f"{j['id']}" + (f"  ({j['date']})" if j["date"] else ""),
        "",
        f"FACKFÖRBUND: {unions}",
        "",
        "FRÅGA:",
        issue,
    ]
    if j["saken"]:
        lines += ["", f"Saken enligt domen: {j['saken']}"]
    lines += ["", f"Domsida: {j['url']}"]
    if j["pdf"]:
        lines.append(f"PDF: {j['pdf']}")
    lines += ["", "(Förbund hittas automatiskt ur domtexten – kontrollera mot PDF:en.)"]
    msg.set_content("\n".join(lines))
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 587)) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def main():
    judgments = fetch_list()
    if not judgments:
        sys.exit("Hittade inga domar – sidans struktur kan ha ändrats.")

    first_run = not STATE.exists()
    seen = set(json.loads(STATE.read_text())) if not first_run else set()
    new = [j for j in judgments if j["id"] not in seen]
    keywords = [k.strip().lower() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]

    if first_run:
        print(f"Första körningen: sparar {len(judgments)} befintliga domar utan att mejla.")
    else:
        sent = 0
        for j in reversed(new):  # äldst först
            enrich(j)
            if matches(j, keywords):
                send_mail(j)
                sent += 1
        print(f"{len(new)} ny(a) dom(ar), {sent} mejl skickade.")

    STATE.write_text(json.dumps(sorted(seen | {j["id"] for j in judgments}), indent=1))


if __name__ == "__main__":
    main()
