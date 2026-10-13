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
COLUMNS = ["Exchange", "Listing Date", "Co. Name", "Symbol", "Out of T2T", "Type", "Circular", "Note"]
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
    for bad, good in (("\u01af", "ff"), ("\ufb00", "ff"), ("\ufb01", "fi"), ("\ufb02", "fl"), ("\ufb03", "ffi")):
        text = text.replace(bad, good)       # PDF ligatures: 'e\u01afect' -> 'effect'
    text = text.replace("^", "")              # '8^th October' -> '8th October'
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

def nse_subject_ok(subject):
    """Take every circular that has 'trade for trade' anywhere in its subject."""
    return "trade for trade" in subject.lower()

def nse_type(subject, text=""):
    low = subject.lower()
    if "sme" in low: return "SME"
    if "ipo" in low: return "Mainboard"
    if re.search(r"demerg|scheme of arrangement|resulting company", text.lower()): return "Demerger"
    return "Other"

def fallback_row_info(subject, text):
    """When the circular has no 'moved to rolling segment' table: build a row from the subject."""
    body = re.sub(r"\s+", " ", text)
    m = re.search(r"([^.]*trade for trade[^.]*\.)", body, re.I)
    note = ("Check circular: " + (m.group(1).strip() if m else body[:160]))[:220]
    name = re.sub(r"^\s*trade for trade\s*[-\u2013:]*\s*", "", subject, flags=re.I)
    sym = ""
    sm = re.search(r"\(\s*([A-Z0-9&\-]{2,20})\s*\)\s*$", name)
    if sm and sm.group(1) != "IPO":
        sym = sm.group(1)
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip() or subject
    return name, sym, note

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
    m = re.search(r"shares of\s+((?:(?!shares of).)+?)\s*(?:[-\u2013]\s*(?:SME\s+)?IPO\s*)?\(\s*scrip code\s*:?\s*(\d{5,6})\s*\)", flat, re.I)
    if not m:
        return exit_date, []
    nm = re.sub(r"\s*[-\u2013]\s*(SME\s+)?IPO\s*$", "", m.group(1).strip(), flags=re.I).strip()
    return exit_date, [(m.group(2), nm)]

TGRP = r"(?:[\u2018\u2019'\"`]\s*(?:t|xt|mt|ts)\s*[\u2018\u2019'\"`]\s*group|\b(?:t|xt|mt|ts)\s+group\b)"

def bse_fallback_info(text):
    """Other BSE group-change wordings. Returns (name, scrip_code, exit_date_or_None, note) or None
    if the notice is not about T / MT group."""
    f = re.sub(r"\s+", " ", clean(text))
    low = f.lower()
    tpat = TGRP + r"|trade[\s-]*(?:for|to)[\s-]*trade|\bt2t\b"
    if not re.search(tpat, low):
        return None
    m = re.search(r"shares of\s+((?:(?!shares of).)+?)\s*\(\s*scrip\s*code\s*:?\s*(\d{5,6})\s*\)", f, re.I)
    name, code = (m.group(1), m.group(2)) if m else ("", "")
    if not code:
        mc = re.search(r"scrip\s*code\s*:?\s*(\d{5,6})", f, re.I)
        code = mc.group(1) if mc else ""
    if not name:
        mn = re.search(r"shares of\s+((?:(?!shares of).)+?(?:Limited|Ltd\.?))", f, re.I)
        name = mn.group(1) if mn else "BSE scrip " + code
    name = re.sub(r"\s*[-\u2013]\s*(SME\s+)?IPO\s*$", "", name.strip(), flags=re.I).strip()

    # the sentence that says what happens: starts at transferred / shifted / moved
    mv = re.search(r"(transferred|shifted|moved|migrat\w*)", low)
    seg = f[mv.start(): mv.start() + 320] if mv else f[:320]
    d = None
    found = find_dates(seg)
    if found: d = found[0][1]
    if d is None:
        d = date_after(f, ["w.e.f", "wef", "with effect from", "effective from", "effective date"])
    # direction: is the T / MT group before or after the word 'to'?
    mt = re.search(r"\bto\b", seg)
    left, right = (seg[:mt.start()], seg[mt.start():]) if mt else (seg, "")
    lt = bool(re.search(tpat, left.lower())); rt = bool(re.search(tpat, right.lower()))
    if not mt:
        lt = rt = False                      # no 'from ... to ...' wording: direction unknown
    if lt and not rt:
        return name, code, d, "Moves OUT of T group" + (" (check notice)" if not d else "")
    if rt and not lt:
        return name, code, None, "Moves INTO T group" + (" w.e.f. " + fmt(d) if d else "")
    return name, code, None, "T group notice - check: " + seg[:150]

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

LISTING_PHRASES = ["admitted to dealings", "dealings on", "commence", "listed", "with effect from", "w.e.f"]

def read_listing_date(s, url, exit_dt):
    """Open a listing circular and read the listing date. Returns None if it looks wrong."""
    try:
        t = get_text(s, url)
    except Exception:
        return None
    d = date_after(clean(t), LISTING_PHRASES, window=130)
    if d and exit_dt and d < exit_dt and (exit_dt - d).days <= 30:
        return d
    return None

def find_listing(s, lst, name, exit_dt, circ_url, circ_text=None):
    """1) circulars mentioned inside this circular (not itself)  2) a 'Listing of ...' circular for the company"""
    own = re.search(r"CML(\d+)", circ_url or "")
    own = own.group(1) if own else ""
    try:
        text = circ_text if circ_text is not None else get_text(s, circ_url)
    except Exception:
        text = ""
    for num in re.findall(r"NSE/CML/(\d+)", text):
        if num == own: continue
        d = read_listing_date(s, f"{NSE_BASE}CML{num}.pdf", exit_dt)
        if d: return d
    key = norm_name(name)
    for x in lst:
        sub = x["subject"].lower()
        if key and key in norm_name(x["subject"]) and "listing" in sub and x["url"] != circ_url:
            d = read_listing_date(s, x["url"], exit_dt)
            if d: return d
    return None

BSE_PDF = "https://www.bseindia.com/downloads/UploadDocs/Notices/{id}/{id}.pdf"
BSE_SEEN = "bse_seen2.txt"   # new name: ignores the old cache that skipped unread notices
BSE_LIST_FILE = "bse_listings.csv"
BSE_LISTINGS = {}      # normalised company name -> listing date (from BSE listing notices)

def load_bse_listings():
    for r in (csv.DictReader(open(BSE_LIST_FILE, encoding="utf-8")) if os.path.exists(BSE_LIST_FILE) else []):
        try: BSE_LISTINGS[r["key"]] = datetime.strptime(r["listing"], DATE_FMT)
        except Exception: pass

def save_bse_listings():
    with open(BSE_LIST_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["key", "listing"])
        for k, d in sorted(BSE_LISTINGS.items()): w.writerow([k, fmt(d)])

def note_bse_listing(text):
    f = re.sub(r"\s+", " ", text)
    m = re.search(r"Listing of Equity Shares of\s+(.+?)\s+(?:Ltd\.?|Limited)", f[:600], re.I)
    if not m: return
    d = date_after(f, ["effective from", "w.e.f", "with effect from"])
    if d: BSE_LISTINGS[norm_name(m.group(1))] = d

def bse_candidate(text):
    """A BSE notice we should read: subject says 'Change in Group of Equity Shares', OR the body itself
    says a scrip moves from Trade for Trade segment to Rolling segment (some PDFs have no header text)."""
    f = flat(text).lower()
    if matches(f, config.BSE_SUBJECT_KEYWORDS):
        return True
    return "scrip code" in f and "trade for trade segment" in f and "rolling segment" in f

def bse_pdf_scan(days):
    """BSE notices are numbered like 20260602-3 (date-number) and each one is a PDF at a fixed address.
    We try the numbers one by one for the last few days, keep PDFs whose text says
    'Change in Group of Equity Shares', and remember which numbers we already checked."""
    seen = set(open(BSE_SEEN).read().split()) if os.path.exists(BSE_SEEN) else set()
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": "https://www.bseindia.com/",
                         "Accept": "application/pdf,*/*"})
    hits, ok, codes, shown = [], 0, {}, False
    today = datetime.now().strftime("%Y%m%d")
    for back in range(days):
        day = (datetime.now() - timedelta(days=back)).strftime("%Y%m%d")
        n, misses = 1, 0
        while misses < 15 and n < 300:
            nid = f"{day}-{n}"; n += 1
            if nid in seen:
                misses = 0; continue
            url = BSE_PDF.format(id=nid)
            try:
                r = sess.get(url, timeout=25)
            except Exception as e:
                codes["error"] = codes.get("error", 0) + 1; misses += 1
                if not shown: print("   BSE debug:", nid, "request error:", e); shown = True
                continue
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                misses = 0; ok += 1
                text = clean(pdf_text(r.content))
                note_bse_listing(text)
                if bse_candidate(text):
                    print("   BSE notice found:", nid)
                    hits.append({"subject": f"BSE notice {nid}: Change in Group of Equity Shares",
                                 "url": url, "text": text})
                elif day != today:
                    seen.add(nid)            # remember only notices we do NOT need again
            else:
                misses += 1
                if not shown:
                    print("   BSE debug:", nid, "status", r.status_code, "|", flat(r.text)[:120]); shown = True
    open(BSE_SEEN, "w").write("\n".join(sorted(seen)))
    save_bse_listings()
    print(f"BSE: opened {ok} notice PDFs, responses {codes}, {len(hits)} match your keyword")
    return None, hits

BSE_NOTICE_PAGE = "https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page="
BSE_LIST_PAGE = "https://www.bseindia.com/markets/MarketInfo/NoticesCirculars.aspx?id=0"
BSE_LIST_API = "https://api.bseindia.com/BseIndiaAPI/api/getDataAdvance_New/w"

def flat(t):
    return re.sub(r"\s+", " ", t or "")

def bse_playwright(days):
    """BSE hides its notices behind JavaScript, so we open a real (headless) Chrome
    browser, ask BSE's own list for the last few days, keep notices whose subject
    matches your keyword, then open each one and read its text."""
    import json
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print("BSE: playwright not installed:", e); return None, []
    end = datetime.now(); start = end - timedelta(days=days)
    api = (f"{BSE_LIST_API}?strTxtDate={start:%Y-%m-%d}&strTxtTodate={end:%Y-%m-%d}&strSegment=")
    found = []            # (notice_no, subject)
    hits = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale="en-IN")
        page = ctx.new_page()
        try:
            page.goto(BSE_LIST_PAGE, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            res = page.evaluate("""async (u) => { try { const r = await fetch(u);
                 return {status: r.status, text: await r.text()}; }
                 catch (e) { return {status: 0, text: String(e)}; } }""", api)
            print("BSE list API status:", res["status"], "| start:", flat(res["text"])[:150])
            try:
                rows = (json.loads(res["text"]) or {}).get("Table") or []
            except Exception:
                rows = []
            for r in rows:
                no = r.get("Notice_No") or r.get("NOTICE_NO") or r.get("NoticeNo") or ""
                sub = r.get("Subject") or r.get("SUBJECT") or r.get("subject") or ""
                if no: found.append((no, flat(sub)))
            print("BSE list: notices from API:", len(found))
            for no, sub in found[:3]: print("   BSE list sample:", no, sub[:80])
            if not found:       # fallback: read the notice list page text itself
                body = flat(page.inner_text("body"))
                for m in re.finditer(r"(\d{8}-\d+)\s+(Change in Group of Equity Shares.{0,120}?)(?=\s\d{8}-\d+|$)", body, re.I):
                    found.append((m.group(1), flat(m.group(2))))
                print("BSE list: notices from page text:", len(found))
        except Exception as e:
            print("BSE list failed:", e)
        want = [(no, sub) for no, sub in found if matches(sub, config.BSE_SUBJECT_KEYWORDS)]
        print(f"BSE: {len(found)} notices, {len(want)} match your keyword")
        for no, sub in want:
            try:
                page.goto(BSE_NOTICE_PAGE + no, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_function("document.body.innerText.toLowerCase().includes('trading members')", timeout=15000)
                except Exception:
                    page.wait_for_timeout(4000)
                text = flat(page.inner_text("body"))
                print("   BSE notice", no, "->", text[:260])
                hits.append({"subject": f"BSE notice {no}: {sub}", "url": BSE_NOTICE_PAGE + no, "text": text})
            except Exception as e:
                print("   BSE notice", no, "failed:", e)
        browser.close()
    return None, hits

# ---------------------------------------------------------------
# 5. CSV
# ---------------------------------------------------------------
def load_csv(path):
    if not os.path.exists(path): return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def sort_key(r):
    try: return datetime.strptime(r["Out of T2T"], DATE_FMT)
    except Exception: return datetime(1900, 1, 1)

def save_csv(rows):
    rows.sort(key=sort_key, reverse=True)
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

# ---------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------
def process_item(exch, sess, item, parser, rows, lockin, lst, done_urls):
    """Handle ONE circular. Returns how many rows it added."""
    if item["url"] in done_urls:
        return 0
    print("Reading:", item["subject"][:80])
    try:
        text = item.get("text") or get_text(sess, item["url"])
    except Exception as e:
        print("   download failed:", e); return 0
    exit_date, stocks = parser(text)
    typ = nse_type(item["subject"], text) if exch == "NSE" else stock_type(item["subject"], text)
    if not exit_date or not stocks:
        if exch != "NSE":
            info = bse_fallback_info(text)
            if not info:
                print("   not about T / MT group - skipped"); return 0
            name, code, d2, note = info
            ld = lockin.get(norm_name(name)) or BSE_LISTINGS.get(norm_name(name))
            print("   BSE T-group notice saved:", name, code, "|", note)
            rows.append({"Exchange": "BSE", "Listing Date": fmt(ld), "Co. Name": name, "Symbol": code,
                         "Out of T2T": fmt(d2), "Type": typ, "Circular": item["url"], "Note": note})
            done_urls.add(item["url"])
            return 1
        name, sym, note = fallback_row_info(item["subject"], text)
        print("   no 'rolling segment' table - saved with a note:", name)
        rows.append({"Exchange": exch, "Listing Date": "", "Co. Name": name, "Symbol": sym,
                     "Out of T2T": "", "Type": typ, "Circular": item["url"], "Note": note})
        done_urls.add(item["url"])
        return 1
    n = 0
    for sym, name in stocks:
        ld = lockin.get(norm_name(name))
        if not ld and exch == "BSE":
            ld = BSE_LISTINGS.get(norm_name(name))
        if not ld and exch == "NSE":
            ld = find_listing(sess, lst, name, exit_date, item["url"], text)
        rows.append({"Exchange": exch, "Listing Date": fmt(ld), "Co. Name": name,
                     "Symbol": sym, "Out of T2T": fmt(exit_date), "Type": typ,
                     "Circular": item["url"], "Note": ""})
        n += 1
    done_urls.add(item["url"])
    time.sleep(1)
    return n

def main():
    rows = load_csv(CSV_FILE)
    lockin = load_lockin()
    load_bse_listings()
    added = 0

    jobs = []
    try:
        s = nse_session()
        lst = nse_list(s, config.LOOKBACK_DAYS)
        hit = [x for x in lst if nse_subject_ok(x["subject"])]
        print(f"NSE: {len(lst)} circulars, {len(hit)} match your keyword")
        jobs += [("NSE", s, x, parse_nse) for x in hit]
    except Exception as e:
        print("NSE failed:", e)
        s, lst = None, []

    # repair old rows whose Listing Date is blank or wrongly equal to the exit date
    if s:
        for r in rows:
            try:
                if r.get("Exchange") != "NSE": continue
                if not r.get("Out of T2T"): continue
                if r.get("Listing Date") and r["Listing Date"] != r.get("Out of T2T"): continue
                exit_dt = datetime.strptime(r["Out of T2T"], DATE_FMT)
                ld = lockin.get(norm_name(r["Co. Name"])) or find_listing(
                    s, lst, r["Co. Name"], exit_dt, r.get("Circular", ""))
                r["Listing Date"] = fmt(ld)
                print("Repaired listing date:", r["Co. Name"], "->", r["Listing Date"] or "blank")
            except Exception as e:
                print("   ERROR while repairing", r.get("Co. Name"), "->", repr(e))

    if getattr(config, "BSE_ENABLED", True):
        try:
            bs, hits = bse_pdf_scan(config.BSE_SCAN_DAYS)
            jobs += [("BSE", bs, x, parse_bse) for x in hits]
        except Exception as e:
            print("BSE failed:", e)

    # BSE rows made by the "other wording" path are rebuilt every run, so parser improvements apply
    rows[:] = [r for r in rows if not (r.get("Exchange") == "BSE" and
               (r.get("Note") or "").startswith(("Moves ", "T group notice")))]
    done_urls = {r.get("Circular") for r in rows if r.get("Circular")}
    for exch, sess, item, parser in jobs:
        try:
            added += process_item(exch, sess, item, parser, rows, lockin, lst, done_urls)
        except Exception as e:
            import traceback
            print("   ERROR on this circular, skipped:", item.get("subject", "")[:70], "->", repr(e))
            traceback.print_exc()

    for r in rows:                              # fill blank BSE listing dates from BSE listing notices / lock-in sheet
        if r.get("Exchange") == "BSE" and not r.get("Listing Date"):
            ld = lockin.get(norm_name(r.get("Co. Name", ""))) or BSE_LISTINGS.get(norm_name(r.get("Co. Name", "")))
            if ld: r["Listing Date"] = fmt(ld)

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
