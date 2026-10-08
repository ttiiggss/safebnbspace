#!/usr/bin/env python3
"""
Pull every accommodation provider from Visit Mornington Peninsula's ATDW collection API,
then fetch each provider's detail page and extract phone/email/website.
This is the official tourism-board-registered list: real businesses with real contacts.
"""
import csv
import json
import random
import re
import sqlite3
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUT = Path(__file__).resolve().parent / "harvest"
DB = OUT / "atdw.db"
API = "https://www.visitmorningtonpeninsula.org/DesktopModules/WMCollectionMapper/API/Operators"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# Accommodation category pages -> their collection ids (discovered from page HTML)
COLLECTIONS = {
    "1141": "Cottages + Holiday Houses",
    "bed-breakfast": None, "apartments": None, "self-contained": None,
}
PHONE_RE = re.compile(r"(?:\+61|\(0\d\)|0)[\s-]?\d{4}[\s-]?\d{3,4}|0?4\d{2}[\s-]?\d{3}[\s-]?\d{3}")
EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")


def cf_decode(hexc):
    """Cloudflare email-protection decoder."""
    r = int(hexc[:2], 16)
    return "".join(chr(int(hexc[i:i + 2], 16) ^ r) for i in range(2, len(hexc), 2))


def extract_contacts(html, text):
    """tel: links, cloudflare-decoded mailtos, then regex fallbacks."""
    tels = re.findall(r'href="tel:([^"]+)"', html)
    phone = re.sub(r"\s+", " ", tels[0]).strip() if tels else (PHONE_RE.findall(text) or [None])[0]
    emails = [cf_decode(m) for m in re.findall(r"email-protection#([0-9a-f]+)", html)]
    emails = [e for e in emails if EMAIL_RE.fullmatch(e)]
    if not emails:
        mailtos = re.findall(r"mailto:([^\"'?]+)", html)
        emails = [e for e in mailtos if EMAIL_RE.fullmatch(e.strip())] or EMAIL_RE.findall(text)
    site = None
    m = re.search(r'ucDetails_urlContact" href="(https?://(?!www\.visitmorningtonpeninsula)[^"]+)"', html)
    if m:
        site = m.group(1)
    return phone, (emails[0].strip() if emails else None), site


def fetch(url, timeout=15):
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        return r if r.status_code == 200 else None
    except Exception:
        return None


def main():
    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS providers (
        provider_id TEXT PRIMARY KEY, name TEXT, category TEXT, url TEXT, lat REAL, lng REAL,
        phone TEXT, email TEXT, website TEXT, status TEXT DEFAULT 'pending',
        checked_at TEXT DEFAULT (datetime('now')))""")
    conn.commit()

    # discover collection ids from each accommodation category page
    pages = ["Cottages-Holiday-Houses", "Bed-Breakfast", "Apartments-Units",
             "Self-Contained", "Retreats-Lodges", "Vineyard-Farm-Stay", "Resorts"]
    colls = dict(COLLECTIONS)
    for p in pages:
        r = fetch(f"https://www.visitmorningtonpeninsula.org/Accommodation/{p}")
        if not r:
            continue
        m = re.search(r'data-grid-app-collection="(\d+)"', r.text)
        if m and m.group(1) not in colls:
            colls[m.group(1)] = p
            print(f"collection {m.group(1)} -> {p}")

    # pull listings per collection
    for cid, label in colls.items():
        if cid is None or not cid.isdigit():
            continue
        r = fetch(f"{API}?p=0&ps=1000&f=&c={cid}&pid=0&o=6")
        if not r:
            print(f"!! collection {cid} failed")
            continue
        for item in r.json().get("data", []):
            conn.execute("INSERT OR IGNORE INTO providers (provider_id, name, category, url, lat, lng) VALUES (?,?,?,?,?,?)",
                         (item["id"], item["title"], label, item["url"],
                          item.get("location", {}).get("lat"), item.get("location", {}).get("lng")))
        n = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
        print(f"collection {cid} ({label}): cumulative {n} providers")
        time.sleep(random.uniform(1, 2))
    conn.commit()

    # enrich: each provider detail page has contact info
    todo = conn.execute("SELECT provider_id, name, url FROM providers WHERE status='pending'").fetchall()
    print(f"\nEnriching {len(todo)} provider detail pages...")
    ok = 0
    for pid, name, url in todo:
        r = fetch(url)
        if not r:
            conn.execute("UPDATE providers SET status='fetch_failed' WHERE provider_id=?", (pid,))
            conn.commit()
            continue
        text = BeautifulSoup(r.text, "html.parser").get_text(separator=" ")
        phone, email, site = extract_contacts(r.text, text)
        if phone or email:
            conn.execute("UPDATE providers SET phone=?, email=?, website=?, status='contact' WHERE provider_id=?",
                         (phone, email, site, pid))
            ok += 1
            print(f"  [OK] {name}: {phone or '-'} {email or ''}")
        else:
            conn.execute("UPDATE providers SET status='no_contact' WHERE provider_id=?", (pid,))
        conn.commit()
        time.sleep(random.uniform(1.5, 3))  # ponytail: fixed delay; they're a small tourism board, be polite

    rows = conn.execute("SELECT name, category, phone, email, website, url, status FROM providers ORDER BY category, name").fetchall()
    with open(OUT / "providers.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "category", "phone", "email", "website", "atdw_url", "status"])
        w.writerows(rows)
    conn.close()
    print(f"\nDone: {ok} with contacts of {len(todo)}. CSV: {OUT / 'providers.csv'}")


if __name__ == "__main__":
    main()
