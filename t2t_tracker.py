"""
T2T Exit Tracker
Reads NSE / BSE circulars about stocks leaving the Trade-for-Trade segment and
writes ONE ROW PER STOCK to t2t_data.csv in this format:

Exchange, Listing Date, Co. Name, Symbol, Out of T2T   (+ Type, Circular)

Run:  python t2t_tracker.py
"""
import csv, io, os, re, time
from datetime import datetime, timedelta

import requests
from pypdf import PdfReader

import config

NSE_BASE = "https://nsearchives.nseindia.com/content/circulars/"
CSV_FILE = "t2t_data.csv"
OVERRIDE_FILE = "manual_overrides.csv"
COLUMNS = ["Exchange", "Listing Date", "Co. Name", "Symbol", "Out of T2T", "Type", "Circular"]
DATE_FMT = "%d-%m-%Y"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# ---------------------------------------------------------------
# 1. DATES
# ---------------------------------------------------------------
DATE_PATTERNS = [
    r"(?P<d>\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]+(?P<m>[A-Za-z]{3,9})[\s\-/.,]+(?P<y>\d{4})",   # 23 Oct 2026
    r"(?P<m2>[A-Za-z]{3,9})\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{4})",            # October 23, 2026
    r"(?P<d3>\d{1,2})[/\-.](?P<m3>\d{1,2})[/\-.](?P<y3>\d{4})",                               # 23-10-2026
]

def find_dates(text):
    found = []
    for pat in DATE_PATTERNS:
        for m in re.finditer(pat, text):
            g = m.groupdict()
            try:
                if g.get("m"):
                    mon = MONTHS.get(g["m"][:3].lower())
                    if not mon: continue
                    dt = datetime(int(g["y"]), mon, int(g["d"]))
                elif g.get("m2"):
                    mon = MONTHS.get(g["m2"][:3].lower())
                    if not mon: continue
                    dt = datetime(int(g["y2"]), mon, int(g["d2"]))
                else:
                    dt = datetime(int(g["y3"]), int(g["m3"]), int(g["d3"]))
                found.append((m.start(), dt))
            except ValueError:
                continue
    return sorted(set(found))

def date_after(text, phrases, window=60):
    """First date that appears right after any of the given phrases."""
    text = re.sub(r"\s+", " ", text)      # phrases may be split across lines
    low = text.lower()
    best = None
    for ph in phrases:
        for m in re.finditer(re.escape(ph), low):
            d = find_dates(text[m.end(): m.end() + window])
            if d and (best is None or m.start() < best[0]):
                best = (m.start(), d[0][1])
    return best[1] if best else None

# ---------------------------------------------------------------
# 2. HELPERS
# ---------------------------------------------------------------
def clean(text):
    return re.sub(r"[ \t]+", " ", text.replace("\r", ""))

def pdf_text(content):
    try:
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(content)).pages)
    except Exception as e:
        print("   PDF read failed:", e); return ""

def norm_name(name):
    n = name.lower()
    n = re.sub(r"\b(limited|ltd|pvt|private)\b", "", n)
    return re.sub(r"[^a-z0-9]", "", n)

def stock_type(subject, text):
    blob = (subject + " " + text[:1500]).lower()
    if "demerg" in blob or "scheme of arrangement" in blob: return "Demerger"
    if "sme" in blob or "emerge" in blob: return "SME"
    return "Mainboard"

def matches(subject, keywords):
    return any(k.lower() in subject.lower() for k in keywords)

def fmt(d):
    return d.strftime(DATE_FMT) if d else ""

# ---------------------------------------------------------------
# 3. READING THE TABLE INSIDE A CIRCULAR  (one row per stock)
# ---------------------------------------------------------------
def parse_nse(text):
    """
    NSE layout (seen in a real circular):
      '... transferred from trade for trade segment (series: ST) to rolling segment
       (Series: SM) with effect from October 23, 2026.'
      Table:  Symbol | Name of the Company
              EVENTIONS  Eventions Limited
    """
    text = clean(text)
    if not any(p in text.lower() for p in config.NSE_EXIT_PHRASES):
        return None, []
    exit_date = date_after(text, ["with effect from", "w.e.f", "wef", "effective from"])
    rows = []
    started = False
    for line in text.split("\n"):
        line = line.strip()
        if re.search(r"name of the company", line, re.I):
            started = True; continue
        if not started: continue
        if re.search(r"for and on behalf|national stock exchange", line, re.I): break
        m = re.match(r"^([A-Z0-9&\-]{2,20})\s+(.+)$", line)
        if m:
            rows.append((m.group(1), m.group(2).strip()))
    return exit_date, rows

def parse_bse(text):
    """
    BSE notice layout (seen in a real notice): one company per notice
      'Change in Group of Equity Shares of PARAMOUNT SYNTEX LIMITED'
      '... equity shares of PARAMOUNT SYNTEX LIMITED - SME IPO (Scrip Code: 544982) shall be
       transferred from Trade for Trade segment (MT Group) to Rolling segment with effect from
       Monday, October 26, 2026.'
    """
    flat = re.sub(r"\s+", " ", clean(text))
    low = flat.lower()
    if "trade for trade" not in low or "rolling" not in low:
        return None, []
    exit_date = date_after(flat, ["with effect from", "w.e.f", "wef", "effective from"])
    m = re.search(r"shares of\s+((?:(?!shares of).)+?)\s+[-\u2013]\s+[^()]*?\(\s*scrip code\s*:?\s*(\d{5,6})\s*\)", flat, re.I)
    if not m:
        return exit_date, []
    return exit_date, [(m.group(2), m.group(1).strip())]

# ---------------------------------------------------------------
# 4. FETCHING
# ---------------------------------------------------------------
def nse_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json,text/plain,*/*",
                      "Accept-Language": "en-US,en;q=0.9",
                      "Referer": "https://www.nseindia.com/resources/exchange-communication-circulars"})
    s.get("https://www.nseindia.com", timeout=20); time.sleep(1)
    return s

def nse_list(s, days):
    to_d = datetime.now(); from_d = to_d - timedelta(days=days)
    f, t = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    r = s.get("https://www.nseindia.com/api/circulars",
              params={"fromDate": f, "toDate": t, "from_date": f, "to_date": t}, timeout=30)
    r.raise_for_status()
    raw = r.json()
    items = raw.get("data", []) if isinstance(raw, dict) else raw
    out, seen_urls = [], set()
    for x in items:
        url = x.get("circFilename") or x.get("fileURL") or x.get("url") or ""
        if url and not url.startswith("http"):
            url = NSE_BASE + url
        if not url.lower().endswith(".pdf") or url in seen_urls:
            continue                      # skip zip files and repeated entries
        seen_urls.add(url)
        out.append({"subject": x.get("sub") or x.get("subject") or "", "url": url})
    return out

def html_to_text(raw):
    import html as _h
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", "\n", raw)
    return _h.unescape(raw)

def get_text(s, url):
    r = s.get(url, timeout=40)
    if r.content[:4] == b"%PDF":
        return pdf_text(r.content)
    return html_to_text(r.text)

def load_lockin():
    """name -> listing date, from your lock-in CSV."""
    try:
        r = requests.get(config.LOCKIN_CSV_URL, timeout=30); r.raise_for_status()
        out = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            try:
                out[norm_name(row["Name"])] = datetime.strptime(row["Listing Date"], "%Y-%m-%d")
            except Exception:
                pass
        print("Lock-in sheet loaded:", len(out), "companies")
        return out
    except Exception as e:
        print("Could not load lock-in sheet:", e); return {}

def listing_from_circular(s, text):
    """Fallback: open the earlier circular mentioned in the text (e.g. NSE/CML/76763)
    and read the listing date from it. Address pattern is a guess - may return None."""
    m = re.search(r"NSE/CML/(\d+)", text)
    if not m: return None
    try:
        t = get_text(s, f"{NSE_BASE}CML{m.group(1)}.pdf")
        return date_after(clean(t), ["with effect from", "w.e.f", "listed", "dealings"])
    except Exception:
        return None

BSE_NOTICE_PAGE = "https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page="
SEEN_FILE = "bse_seen.txt"

def bse_scan(days):
    """BSE notices are numbered like 20261009-4 (date-number). We open them one by one
    for the last few days and keep the ones whose subject matches your keyword."""
    seen = set(open(SEEN_FILE).read().split()) if os.path.exists(SEEN_FILE) else set()
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": "https://www.bseindia.com/",
                         "Accept-Language": "en-US,en;q=0.9"})
    try: sess.get("https://www.bseindia.com/", timeout=20)
    except Exception: pass
    hits, ok, bad, shown = [], 0, 0, False
    for back in range(days):
        day = (datetime.now() - timedelta(days=back)).strftime("%Y%m%d")
        n, misses = 1, 0
        while misses < 4 and n < 300:
            nid = f"{day}-{n}"; n += 1
            if nid in seen:
                misses = 0; continue
            try:
                r = sess.get(BSE_NOTICE_PAGE + nid, timeout=20)
                text = html_to_text(r.text)
            except Exception as e:
                misses += 1; bad += 1
                if not shown: print("   BSE debug: request error:", e); shown = True
                continue
            if "notice no" not in text.lower():
                misses += 1; bad += 1
                if not shown:
                    print("   BSE debug: page", nid, "status", r.status_code, "text start:",
                          re.sub(r"\s+", " ", text)[:200]); shown = True
                continue
            misses = 0; ok += 1; seen.add(nid)
            if matches(text, config.BSE_SUBJECT_KEYWORDS):
                hits.append({"subject": f"BSE notice {nid}", "url": BSE_NOTICE_PAGE + nid, "text": text})
            time.sleep(0.2)
    open(SEEN_FILE, "w").write("\n".join(sorted(seen)))
    print(f"BSE: opened {ok} notices ({bad} empty/failed), {len(hits)} match your keyword")
    return sess, hits

# ---------------------------------------------------------------
# 5. CSV
# ---------------------------------------------------------------
def load_csv(path):
    if not os.path.exists(path): return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def sort_key(r):
    try: return datetime.strptime(r["Out of T2T"], DATE_FMT)
    except Exception: return datetime(2100, 1, 1)

def save_csv(rows):
    rows.sort(key=sort_key, reverse=True)
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

# ---------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------
def main():
    rows = load_csv(CSV_FILE)
    have = {(r["Exchange"], r["Symbol"], r["Out of T2T"]) for r in rows}
    lockin = load_lockin()
    added = 0

    jobs = []
    try:
        s = nse_session()
        lst = nse_list(s, config.LOOKBACK_DAYS)
        hit = [x for x in lst if matches(x["subject"], config.NSE_SUBJECT_KEYWORDS)
               and matches(x["subject"], config.NSE_SUBJECT_MUST_ALSO_HAVE)]
        print(f"NSE: {len(lst)} circulars, {len(hit)} match your keyword")
        jobs += [("NSE", s, x, parse_nse) for x in hit]
    except Exception as e:
        print("NSE failed:", e)

    try:
        bs, hits = bse_scan(config.BSE_SCAN_DAYS)
        jobs += [("BSE", bs, x, parse_bse) for x in hits]
    except Exception as e:
        print("BSE failed:", e)

    for exch, sess, item, parser in jobs:
        print("Reading:", item["subject"][:80])
        try:
            text = item.get("text") or get_text(sess, item["url"])
        except Exception as e:
            print("   download failed:", e); continue
        exit_date, stocks = parser(text)
        if not exit_date or not stocks:
            print("   no exit date / stock rows found - skipped"); continue
        typ = stock_type(item["subject"], text)
        for sym, name in stocks:
            if (exch, sym, fmt(exit_date)) in have: continue
            ld = lockin.get(norm_name(name))
            if not ld and exch == "NSE":
                ld = listing_from_circular(sess, text)
            rows.append({"Exchange": exch, "Listing Date": fmt(ld), "Co. Name": name,
                         "Symbol": sym, "Out of T2T": fmt(exit_date), "Type": typ,
                         "Circular": item["url"]})
            have.add((exch, sym, fmt(exit_date))); added += 1
        time.sleep(1)

    # manual fixes win (matched on Exchange + Symbol)
    for o in load_csv(OVERRIDE_FILE):
        hit = False
        for r in rows:
            if r["Exchange"] == o.get("Exchange") and r["Symbol"] == o.get("Symbol"):
                r.update({k: v for k, v in o.items() if v}); hit = True
        if not hit and o.get("Symbol"):
            rows.append(o)

    save_csv(rows)
    print(f"Done. {added} new rows, {len(rows)} total -> {CSV_FILE}")

if __name__ == "__main__":
    main()
