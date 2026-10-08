#!/usr/bin/env python3
"""Sewer Watch - Home Assistant add-on.

Watches the Spencer County, KY Fiscal Court website (agenda, minutes,
meeting detail pages, public notices), downloads every PDF it finds,
extracts the text (OCR for scans), searches it for sewer-related keywords
and pushes notifications through Home Assistant with snippets and links.
A small reader UI is served through HA ingress.

Standard library only.
"""
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
SITE = "https://spencercountyky.gov"
AGENDA_URL = SITE + "/nav/fiscal_court_agenda.php"
MINUTES_URL = SITE + "/nav/meeting_minutes.php"
NOTICES_URL = SITE + "/nav/public_notices.php"
YOUTUBE_URL = "https://www.youtube.com/channel/UCPhTFTTwDIAuYU80APrPWUw"
KDEP_URL = "https://eec.ky.gov/Environmental-Protection/Water/Pages/Water-Public-Notices-and-Hearings.aspx"

DATA_DIR = os.environ.get("SW_DATA", "/data")
OPTIONS_FILE = os.path.join(DATA_DIR, "options.json")
DB_FILE = os.path.join(DATA_DIR, "sewer_watch.db")
DRY_RUN = os.environ.get("SW_DRY") == "1"          # print instead of calling HA
def _supervisor_token():
    """The base image's s6 init can strip env vars; it also saves them to files."""
    t = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or ""
    for f in ("/run/s6/container_environment/SUPERVISOR_TOKEN",
              "/var/run/s6/container_environment/SUPERVISOR_TOKEN",
              "/run/s6/container_environment/HASSIO_TOKEN"):
        if t:
            break
        try:
            with open(f) as fh:
                t = fh.read().strip()
        except OSError:
            pass
    return t


TOKEN = _supervisor_token()
LAST_HA_ERROR = ""
HA_API = "http://supervisor/core/api"
UA = "Mozilla/5.0 (Home Assistant Sewer Watch add-on)"
MAX_PDF_BYTES = 60 * 1024 * 1024
MAX_OCR_PAGES = 40

DEFAULTS = {
    "notify_service": "",
    "check_interval_minutes": 180,
    "backfill_months": 12,
    "ocr": True,
    "notify_every_new_document": True,
    "meeting_day_reminder": True,
    "keywords": ["sewer", "sanitation", "top flight", "wastewater"],
    "extra_pages": [],
}


def load_options():
    opts = dict(DEFAULTS)
    try:
        with open(OPTIONS_FILE) as f:
            opts.update(json.load(f))
    except FileNotFoundError:
        pass
    opts["keywords"] = [k.strip() for k in opts.get("keywords", []) if k and k.strip()]
    return opts


OPTS = load_options()


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
DB_LOCK = threading.RLock()


def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    os.makedirs(DATA_DIR, exist_ok=True)
    with DB_LOCK, db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS docs (
              id INTEGER PRIMARY KEY,
              base TEXT UNIQUE,         -- url without ?t=
              url TEXT,                 -- full url incl. version
              ver TEXT,
              title TEXT,
              meeting_date TEXT,        -- ISO date or ''
              meeting_type TEXT,
              source TEXT,
              video TEXT,
              details TEXT,
              found_at TEXT,
              indexed_at TEXT,
              status TEXT,              -- listed | indexed | error
              text TEXT,
              hits TEXT,                -- json list of keywords
              snippets TEXT             -- json list of strings
            );
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """
        )


def kv_get(k, default=None):
    with DB_LOCK, db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(k, v):
    with DB_LOCK, db() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# --------------------------------------------------------------------------
# HTTP + HTML
# --------------------------------------------------------------------------
def norm_url(u):
    """Absolute URL with spaces etc. percent-encoded exactly once."""
    p = urlsplit(u)
    path = quote(unquote(p.path), safe="/()-_.~,+&'!$*;:@=")
    query = quote(unquote(p.query), safe="=&/:+,;")
    return urlunsplit((p.scheme or "https", p.netloc, path, query, ""))


def split_version(u):
    p = urlsplit(u)
    q = parse_qs(p.query)
    ver = (q.get("t") or [""])[0]
    base = urlunsplit((p.scheme, p.netloc.lower(), unquote(p.path), "", ""))
    return base, ver


def fetch(url, binary=False, limit=8 * 1024 * 1024):
    last = None
    for attempt in range(3):
        try:
            req = Request(norm_url(url), headers={"User-Agent": UA})
            with urlopen(req, timeout=90) as r:
                data = r.read(limit + 1)
            if len(data) > limit:
                raise ValueError("response too large")
            if binary:
                return data
            return data.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise last


BLOCK_TAGS = {"p", "br", "div", "li", "tr", "td", "th", "table", "ul", "ol",
              "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "hr"}


class Tokenizer(HTMLParser):
    """Turns HTML into an ordered stream of ('text', s) / ('nl',) / ('link', href, text)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.toks = []
        self.skip = 0
        self.a = None

    def handle_starttag(self, tag, attrs):
        if tag == "base" and dict(attrs).get("href"):
            self.toks.append(("base", dict(attrs)["href"]))
        if tag in ("script", "style", "noscript", "head"):
            self.skip += 1
        if tag in BLOCK_TAGS:
            self.toks.append(("nl",))
        if tag == "a":
            self.a = [dict(attrs).get("href") or "", []]

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "head"):
            self.skip = max(0, self.skip - 1)
        if tag == "a" and self.a is not None:
            self.toks.append(("link", self.a[0], " ".join(x for x in self.a[1] if x).strip()))
            self.a = None
        if tag in BLOCK_TAGS:
            self.toks.append(("nl",))

    def handle_startendtag(self, tag, attrs):
        if tag == "base" and dict(attrs).get("href"):
            self.toks.append(("base", dict(attrs)["href"]))
        if tag in BLOCK_TAGS:
            self.toks.append(("nl",))

    def handle_data(self, d):
        if self.skip:
            return
        if self.a is not None:
            self.a[1].append(d.strip())
        self.toks.append(("text", d))


def tokenize(page):
    t = Tokenizer()
    t.feed(page)
    t.close()
    return t.toks


def toks_text(toks):
    out = []
    for t in toks:
        if t[0] == "text":
            out.append(t[1])
        elif t[0] == "nl":
            out.append("\n")
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in "".join(out).split("\n")]
    return "\n".join(ln for ln in lines if ln)


def is_pdf(href):
    return bool(re.search(r"\.pdf(\?|$)", href or "", re.I))


def is_video(href):
    return bool(re.search(r"youtu\.be/|youtube\.com/watch|facebook\.com/.+/videos/", href or "", re.I))


def same_site(u):
    return urlsplit(u).netloc.lower().endswith("spencercountyky.gov")


def parse_mdy(s):
    try:
        return datetime.strptime(s, "%m/%d/%y").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Keyword search
# --------------------------------------------------------------------------
def kw_patterns():
    pats = []
    for k in OPTS["keywords"]:
        pats.append((k, re.compile(r"(?<![a-z0-9])" + re.escape(k.lower()).replace(r"\ ", r"\s+"), re.I)))
    return pats


def find_hits(text, max_snips=8, radius=170, with_pages=False):
    if not text:
        return ([], [], []) if with_pages else ([], [])
    spans, matched = [], []
    for k, rx in kw_patterns():
        for m in rx.finditer(text):
            spans.append((max(0, m.start() - radius), min(len(text), m.end() + radius)))
            if k not in matched:
                matched.append(k)
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    snips, pages = [], []
    for s, e in merged[:max_snips]:
        pages.append(text.count("\f", 0, s + radius // 2) + 1)
        # start/end on sentence boundaries when one is close by
        head = text[s:s + radius]
        m = re.search(r"[.;:]\s+(?=[A-Z0-9])", head)
        if m and s > 0:
            s += m.end()
        tail = text[max(s, e - radius):e]
        m = None
        for m in re.finditer(r"[.;]\s", tail):
            pass
        if m and e < len(text):
            e = max(s, e - radius) + m.start() + 1
        snip = re.sub(r"\s+", " ", text[s:e]).strip()
        snips.append(("…" if s > 0 else "") + snip + ("…" if e < len(text) else ""))
    return (matched, snips, pages) if with_pages else (matched, snips)


# --------------------------------------------------------------------------
# PDF text
# --------------------------------------------------------------------------
def pdf_text(data):
    with tempfile.TemporaryDirectory() as td:
        pdf = os.path.join(td, "doc.pdf")
        with open(pdf, "wb") as f:
            f.write(data)
        text = ""
        try:
            text = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True,
                                  timeout=300).stdout.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            log("pdftotext failed:", e)
        if len(re.sub(r"\s", "", text)) >= 200 or not OPTS.get("ocr", True):
            return text, "text"
        # Scanned PDF: rasterize and OCR
        try:
            subprocess.run(["pdftoppm", "-r", "200", "-gray", "-png", "-l", str(MAX_OCR_PAGES),
                            pdf, os.path.join(td, "pg")], check=True, timeout=900, capture_output=True)
            pages = sorted(p for p in os.listdir(td) if p.startswith("pg") and p.endswith(".png"))
            parts = []
            for p in pages:
                r = subprocess.run(["tesseract", os.path.join(td, p), "-", "--psm", "3"],
                                   capture_output=True, timeout=300)
                parts.append(r.stdout.decode("utf-8", "replace"))
            ocr = "\n\f\n".join(parts)
            if len(ocr.strip()) > len(text.strip()):
                return ocr, "ocr"
        except Exception as e:  # noqa: BLE001
            log("OCR failed:", e)
        return text, "text"


# --------------------------------------------------------------------------
# Page images (the real document, with keywords highlighted)
# --------------------------------------------------------------------------
PDF_DIR = os.path.join(DATA_DIR, "pdf")
PAGE_DIR = os.path.join(DATA_DIR, "pages")
WWW_DIR = os.environ.get("SW_WWW", "/homeassistant/www/sewer_watch")   # served by HA as /local/...
WWW_URL = "/local/sewer_watch"
PAGE_DPI = 150
RENDER_SLOTS = threading.Semaphore(2)    # keep a Raspberry Pi responsive


def pdf_file(row_id, url=None, data=None):
    """Local copy of the document's PDF (downloaded if missing)."""
    path = os.path.join(PDF_DIR, f"{row_id}.pdf")
    if data is None and not os.path.exists(path) and url:
        data = fetch(url, binary=True, limit=MAX_PDF_BYTES)
    if data is not None:
        os.makedirs(PDF_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        for old in os.listdir(PAGE_DIR) if os.path.isdir(PAGE_DIR) else []:
            if old.startswith(f"{row_id}_"):
                os.remove(os.path.join(PAGE_DIR, old))
    return path if os.path.exists(path) else None


def page_count(path):
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return int(m.group(1)) if m else 0
    except Exception:  # noqa: BLE001
        return 0


def page_png(path, page, dpi=PAGE_DPI):
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "p")
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi), "-png", "-singlefile",
                        path, base], check=True, timeout=300, capture_output=True)
        with open(base + ".png", "rb") as f:
            return f.read()


def word_boxes(png):
    """OCR word positions on a rendered page: [(normalized_word, (x0,y0,x1,y1))]."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        fn = f.name
    try:
        out = subprocess.run(["tesseract", fn, "-", "--psm", "3", "tsv"], capture_output=True,
                             timeout=300).stdout.decode("utf-8", "replace")
    finally:
        os.unlink(fn)
    words = []
    for line in out.splitlines()[1:]:
        p = line.split("\t")
        if len(p) >= 12 and p[0] == "5" and p[11].strip():
            x, y, w, h = (int(v) for v in p[6:10])
            words.append((re.sub(r"[^a-z0-9]", "", p[11].lower()), (x, y, x + w, y + h)))
    return words


def keyword_boxes(words, keywords=None):
    boxes = []
    for kw in keywords or OPTS["keywords"]:
        toks = re.findall(r"[a-z0-9]+", kw.lower())
        n = len(toks)
        if not n:
            continue
        for i in range(len(words) - n + 1):
            if all((words[i + j][0].startswith(toks[j]) if j == n - 1 else words[i + j][0] == toks[j])
                   for j in range(n)):
                bs = [words[i + j][1] for j in range(n)]
                boxes.append((min(b[0] for b in bs), min(b[1] for b in bs),
                              max(b[2] for b in bs), max(b[3] for b in bs)))
    return boxes


def highlighted(png, boxes):
    from PIL import Image, ImageDraw   # py3-pillow
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for x0, y0, x1, y1 in boxes:
        d.rectangle((x0 - 5, y0 - 4, x1 + 5, y1 + 4), fill=(255, 225, 0, 105), outline=(235, 110, 0, 255), width=3)
    return Image.alpha_composite(im, ov).convert("RGB")


def hit_pages(text):
    pages = set()
    for _, rx in kw_patterns():
        for m in rx.finditer(text or ""):
            pages.add(text.count("\f", 0, m.start()) + 1)
    return pages


def first_hit_page(text):
    pos = [m.start() for _, rx in kw_patterns() for m in rx.finditer(text or "")]
    return (text.count("\f", 0, min(pos)) + 1) if pos else 1


def page_image(r, page):
    """JPEG of one page for the reader, keywords highlighted. Cached on disk."""
    os.makedirs(PAGE_DIR, exist_ok=True)
    cache = os.path.join(PAGE_DIR, f"{r['id']}_{r['ver'] or 0}_{page}.jpg")
    if os.path.exists(cache):
        with open(cache, "rb") as f:
            return f.read()
    with RENDER_SLOTS:
        path = pdf_file(r["id"], r["url"])
        png = page_png(path, page)
        from PIL import Image
        if page in hit_pages(r["text"]):
            im = highlighted(png, keyword_boxes(word_boxes(png)))
        else:
            im = Image.open(io.BytesIO(png)).convert("RGB")
        im.save(cache, "JPEG", quality=72, optimize=True)
    with open(cache, "rb") as f:
        return f.read()


def alert_image(r):
    """Crop of the page with the first mention, highlighted, published under /local for the phone."""
    try:
        if not os.path.isdir(os.path.dirname(WWW_DIR)):
            return None
        with RENDER_SLOTS:
            path = pdf_file(r["id"], r["url"])
            page = first_hit_page(r["text"])
            png = page_png(path, page)
            boxes = keyword_boxes(word_boxes(png))
            im = highlighted(png, boxes)
        w, h = im.size
        band = int(PAGE_DPI * 4.5)          # ~4.5 inches of the page around the mention
        cy = (boxes[0][1] + boxes[0][3]) // 2 if boxes else band // 2
        top = max(0, min(h - band, cy - band // 2))
        im = im.crop((0, top, w, min(h, top + band)))
        os.makedirs(WWW_DIR, exist_ok=True)
        name = f"doc{r['id']}_{int(time.time())}.jpg"
        im.save(os.path.join(WWW_DIR, name), "JPEG", quality=78, optimize=True)
        # keep the folder small
        files = sorted(os.listdir(WWW_DIR), key=lambda n: os.path.getmtime(os.path.join(WWW_DIR, n)))
        for old in files[:-40]:
            os.remove(os.path.join(WWW_DIR, old))
        return f"{WWW_URL}/{name}"
    except Exception as e:  # noqa: BLE001
        log("alert image failed:", e)
        return None


# --------------------------------------------------------------------------
# Home Assistant
# --------------------------------------------------------------------------
INGRESS_PANEL = "/hassio/ingress/local_sewer_watch"


def ha_call(method, path, body=None, base=HA_API):
    global LAST_HA_ERROR
    if DRY_RUN:
        log("[DRY]", method, path, json.dumps(body, ensure_ascii=False)[:3000])
        return None
    if not TOKEN:
        LAST_HA_ERROR = "the app has no Home Assistant access token (SUPERVISOR_TOKEN missing)"
        log("HA call skipped:", LAST_HA_ERROR)
        return None
    headers = {"Authorization": "Bearer " + TOKEN}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(base + path, method=method,
                  data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urlopen(req, timeout=30) as r:
            raw = r.read()
            LAST_HA_ERROR = ""
            return json.loads(raw) if raw else None
    except Exception as e:  # noqa: BLE001
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
        LAST_HA_ERROR = f"{method} {path}: {e} {detail}".strip()
        log("HA call failed:", LAST_HA_ERROR)
        return None


def detect_panel():
    global INGRESS_PANEL
    info = ha_call("GET", "/addons/self/info", base="http://supervisor")
    try:
        slug = info["data"]["slug"]
        INGRESS_PANEL = "/hassio/ingress/" + slug
    except Exception:  # noqa: BLE001
        pass


SKIP_NOTIFY = {"notify", "send_message", "persistent_notification"}


def get_targets():
    """Notify services chosen in the panel (falls back to a legacy option)."""
    try:
        t = json.loads(kv_get("notify_targets", "") or "null")
        if isinstance(t, list):
            return t
    except ValueError:
        pass
    legacy = (OPTS.get("notify_service") or "").strip().replace("notify.", "", 1)
    return [legacy] if legacy and legacy != "mobile_app_CHANGE_ME" else []


def nice_target(svc):
    if svc.startswith("mobile_app_"):
        return svc[len("mobile_app_"):].replace("_", " ").title() + " (phone app)"
    return svc.replace("_", " ").title()


def list_notify_services():
    """[(service, label)] for every notify service HA knows, phones first."""
    if DRY_RUN:
        return [("mobile_app_pixel_8", nice_target("mobile_app_pixel_8")),
                ("mobile_app_nathans_iphone", nice_target("mobile_app_nathans_iphone")),
                ("alexa_media_kitchen", nice_target("alexa_media_kitchen"))]
    data = ha_call("GET", "/services") or []
    out = []
    for dom in data if isinstance(data, list) else []:
        if dom.get("domain") == "notify":
            for svc in dom.get("services", {}):
                if svc not in SKIP_NOTIFY:
                    out.append((svc, nice_target(svc)))
    out.sort(key=lambda x: (not x[0].startswith("mobile_app_"), x[1].lower()))
    return out


def notify(title, message, links=None, full=None, important=False, image=None):
    """Phone push (short) + persistent notification (full) + event + logbook."""
    links = [l for l in (links or []) if l and l[1]]
    phone_msg = message if len(message) <= 1000 else message[:997] + "…"
    actions = [{"action": "URI", "title": t[:24], "uri": u} for t, u in links[:3]]
    first = links[0][1] if links else INGRESS_PANEL
    targets = get_targets()
    if not targets:
        log("no alert targets chosen yet - only creating a persistent notification")
    for svc in targets:
        data = {
            "url": first, "clickAction": first,
            "actions": actions,
            "group": "sewer_watch",
            "priority": "high" if important else "normal",
            "ttl": 0,
            "push": {"interruption-level": "time-sensitive" if important else "active"},
        }
        if image:
            data["image"] = image            # picture of the page, shown in the notification
        ha_call("POST", "/services/notify/" + svc, {"title": title, "message": phone_msg, "data": data})
    md_links = "\n".join(f"- [{t}]({u})" for t, u in links)
    ha_call("POST", "/services/persistent_notification/create", {
        "title": title,
        "message": (full or message) + (f"\n\n![page]({image})" if image else "")
                   + ("\n\n" + md_links if md_links else ""),
    })
    ha_call("POST", "/services/logbook/log", {"name": "Sewer Watch", "message": title})
    ha_call("POST", "/events/sewer_watch_alert", {
        "title": title, "message": message, "links": [{"title": t, "url": u} for t, u in links],
        "important": important,
    })


def publish_status(ok, note=""):
    with DB_LOCK, db() as c:
        n = c.execute("SELECT COUNT(*) FROM docs WHERE status='indexed'").fetchone()[0]
        nh = c.execute("SELECT COUNT(*) FROM docs WHERE hits IS NOT NULL AND hits!='[]'").fetchone()[0]
        last = c.execute("SELECT * FROM docs WHERE status='indexed' ORDER BY found_at DESC, id DESC LIMIT 1").fetchone()
        lasthit = c.execute("SELECT * FROM docs WHERE hits IS NOT NULL AND hits!='[]' "
                            "ORDER BY meeting_date DESC, id DESC LIMIT 1").fetchone()
    attrs = {
        "friendly_name": "Sewer Watch",
        "icon": "mdi:pipe-leak",
        "last_check": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "agenda_date": kv_get("agenda_date", ""),
        "agenda_keywords": kv_get("agenda_hits", ""),
        "documents_indexed": n,
        "documents_mentioning_keywords": nh,
        "reader": INGRESS_PANEL,
    }
    if last:
        attrs.update({"latest_document": last["title"], "latest_url": last["url"],
                      "latest_meeting": last["meeting_date"]})
    if lasthit:
        attrs.update({"latest_mention": lasthit["title"], "latest_mention_url": lasthit["url"],
                      "latest_mention_meeting": lasthit["meeting_date"],
                      "latest_mention_keywords": ", ".join(json.loads(lasthit["hits"]))})
    ha_call("POST", "/states/sensor.sewer_watch", {"state": "ok" if ok else "error", "attributes": attrs})


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------
def join_base(page_url, toks):
    """Revize pages set <base href>; documents live at the site root, so default there."""
    for t in toks:
        if t[0] == "base":
            return urljoin(page_url, t[1])
    return SITE + "/" if same_site(page_url) else page_url


def minutes_entries(page, page_url):
    """Each entry: date, type, list of (href, text). Entries are newest-first on the site."""
    entries, cur = [], None
    toks = tokenize(page)
    jb = join_base(page_url, toks)
    for t in toks:
        if t[0] == "text":
            m = re.match(r"\s*(\d{2}/\d{2}/\d{2})\b(.*)", t[1], re.S)
            if m:
                cur = {"date": parse_mdy(m.group(1)), "type": m.group(2).strip()[:80], "links": []}
                entries.append(cur)
                continue
            if cur and not cur["type"] and re.search(r"meeting|hearing|session", t[1], re.I):
                cur["type"] = t[1].strip()[:80]
        elif t[0] == "link" and cur is not None and t[1]:
            cur["links"].append((urljoin(jb, t[1].strip()), t[2]))
    return [e for e in entries if e["date"]]


def entry_docs(entry, source):
    video = next((u for u, _ in entry["links"] if is_video(u)), "")
    details = next((u for u, _ in entry["links"] if "agenda_details" in u.lower()), "")
    docs = []
    for u, txt in entry["links"]:
        if is_pdf(u) and same_site(u):
            docs.append({"url": u, "title": txt or os.path.basename(unquote(urlsplit(u).path)),
                         "meeting_date": entry["date"].isoformat(), "meeting_type": entry["type"],
                         "video": video, "details": details, "source": source})
    return docs, details


def page_pdf_docs(page, page_url, source, meeting_date=""):
    docs = []
    toks = tokenize(page)
    jb = join_base(page_url, toks)
    for t in toks:
        if t[0] == "link" and t[1] and is_pdf(t[1]):
            u = urljoin(jb, t[1].strip())
            if same_site(u):
                docs.append({"url": u, "title": t[2] or os.path.basename(unquote(urlsplit(u).path)),
                             "meeting_date": meeting_date, "meeting_type": "", "video": "",
                             "details": "", "source": source})
    return docs


def agenda_from_page(page):
    text = toks_text(tokenize(page))
    m = (re.search(r"(FY\s*\d\d/\d\d\s*AGENDA.*?)(?:Share this page|$)", text, re.S)
         or re.search(r"((?:Regular|Special) Meeting\b.*?)(?:Share this page|$)", text, re.S))
    agenda = m.group(1).strip() if m else ""
    d = re.search(r"(January|February|March|April|May|June|July|August|September|October|"
                  r"November|December)\s+(\d{1,2}),\s*(20\d\d)", agenda)
    adate = ""
    if d:
        try:
            adate = datetime.strptime(f"{d.group(1)} {d.group(2)} {d.group(3)}", "%B %d %Y").date().isoformat()
        except ValueError:
            pass
    return agenda, adate


def agenda_items(agenda):
    items = []
    for line in agenda.split("\n"):
        for part in re.split(r"\s(?=(?:[A-L]|\d{1,2})\.\s)", line):
            part = part.strip()
            if part:
                items.append(part)
    return items


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------
def index_doc(row_id):
    with DB_LOCK, db() as c:
        r = c.execute("SELECT * FROM docs WHERE id=?", (row_id,)).fetchone()
    if not r:
        return None
    try:
        data = fetch(r["url"], binary=True, limit=MAX_PDF_BYTES)
        pdf_file(row_id, data=data)          # keep a copy so the reader can show real pages
        text, how = pdf_text(data)
        hits, snips = find_hits(text)
        with DB_LOCK, db() as c:
            c.execute("UPDATE docs SET text=?, hits=?, snippets=?, status='indexed', indexed_at=? WHERE id=?",
                      (text, json.dumps(hits), json.dumps(snips), datetime.now().isoformat(timespec="seconds"), row_id))
        log(f"indexed [{how}] {r['title']} ({r['meeting_date']}) hits={hits}")
    except Exception as e:  # noqa: BLE001
        log("index failed", r["url"], e)
        with DB_LOCK, db() as c:
            c.execute("UPDATE docs SET status='error' WHERE id=?", (row_id,))
    with DB_LOCK, db() as c:
        return c.execute("SELECT * FROM docs WHERE id=?", (row_id,)).fetchone()


def upsert(doc):
    """Returns (row_id, change) where change is 'new', 'updated' or None."""
    base, ver = split_version(doc["url"])
    now = datetime.now().isoformat(timespec="seconds")
    with DB_LOCK, db() as c:
        r = c.execute("SELECT * FROM docs WHERE base=?", (base,)).fetchone()
        if r is None:
            cur = c.execute(
                "INSERT INTO docs(base,url,ver,title,meeting_date,meeting_type,source,video,details,found_at,status)"
                " VALUES(?,?,?,?,?,?,?,?,?,?, 'listed')",
                (base, norm_url(doc["url"]), ver, doc["title"], doc["meeting_date"], doc["meeting_type"],
                 doc["source"], doc["video"], doc["details"], now))
            return cur.lastrowid, "new"
        # fill in metadata learned later (e.g. video added after the meeting)
        c.execute("UPDATE docs SET video=COALESCE(NULLIF(?,''),video), details=COALESCE(NULLIF(?,''),details),"
                  " meeting_date=COALESCE(NULLIF(meeting_date,''),?), meeting_type=COALESCE(NULLIF(meeting_type,''),?)"
                  " WHERE id=?", (doc["video"], doc["details"], doc["meeting_date"], doc["meeting_type"], r["id"]))
        if ver and ver != r["ver"]:
            c.execute("UPDATE docs SET ver=?, url=? WHERE id=?", (ver, norm_url(doc["url"]), r["id"]))
            return r["id"], "updated"
        return r["id"], None


def doc_links(r):
    links = [("Open PDF", r["url"])]
    if r["video"]:
        links.append(("Watch meeting", r["video"]))
    links.append(("Sewer Watch reader", INGRESS_PANEL))
    return links


def fmt_date(iso):
    try:
        d = date.fromisoformat(iso)
        return d.strftime("%b %d, %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return iso or ""


def alert_doc(r, change, prev_hits=None):
    hits = json.loads(r["hits"] or "[]")
    snips = json.loads(r["snippets"] or "[]")
    when = fmt_date(r["meeting_date"])
    what = f"{r['title']}" + (f" – {r['meeting_type']} {when}" if when else "")
    verb = "Re-uploaded" if change == "updated" else "New"
    if hits:
        title = f"🚨 {verb}: {what}"
        body = "Mentions: " + ", ".join(hits) + "\n\n" + "\n\n".join("• " + s for s in snips[:3])
        full = ("**Mentions:** " + ", ".join(hits) + "\n\n" + "\n\n".join("> " + s for s in snips)
                + f"\n\nSource: {r['source']}")
    else:
        title = f"{verb} county document: {what}"
        body = "No sewer keywords found in this document."
        if r["status"] == "error":
            body = "Couldn't read this PDF - open it directly."
        full = body + f"\n\nSource: {r['source']}"
    image = alert_image(r) if hits else None
    if hits:
        body = f"Page {first_hit_page(r['text'])} · " + body
    notify(title, body, doc_links(r), full=full, important=bool(hits) and hits != (prev_hits or []), image=image)


# --------------------------------------------------------------------------
# One full check
# --------------------------------------------------------------------------
CHECK_LOCK = threading.Lock()


def run_check():
    if not CHECK_LOCK.acquire(blocking=False):
        log("check already running")
        return
    try:
        _run_check()
        kv_set("last_success", datetime.now().isoformat(timespec="seconds"))
        kv_set("site_alerted", "0")
        publish_status(True)
    except Exception as e:  # noqa: BLE001
        log("CHECK FAILED:", e)
        traceback.print_exc()
        publish_status(False, str(e)[:200])
    finally:
        CHECK_LOCK.release()


def _run_check():
    initialized = kv_get("initialized") == "1"
    cutoff = date.today() - timedelta(days=30 * int(OPTS.get("backfill_months", 12)))
    found = []

    # 1) minutes page -> entries -> PDFs (+ detail pages for recent meetings)
    page = fetch(MINUTES_URL)
    entries = minutes_entries(page, MINUTES_URL)
    log(f"minutes page: {len(entries)} meetings listed")
    recent_details = date.today() - timedelta(days=60)
    for e in entries:
        docs, details = entry_docs(e, "Meeting minutes page")
        found += docs
        if details and e["date"] >= max(cutoff if not initialized else recent_details, cutoff):
            try:
                dpage = fetch(details)
                for d in page_pdf_docs(dpage, details, "Meeting details page", e["date"].isoformat()):
                    d.update({"meeting_type": e["type"], "details": details,
                              "video": next((u for u, _ in e["links"] if is_video(u)), "")})
                    found.append(d)
                time.sleep(1)
            except Exception as ex:  # noqa: BLE001
                log("details page failed", details, ex)

    # 2) agenda page (text) + any PDFs on it
    apage = fetch(AGENDA_URL)
    agenda, adate = agenda_from_page(apage)
    found += page_pdf_docs(apage, AGENDA_URL, "Fiscal Court agenda page", adate)

    # 3) public notices + any extra pages the user added
    for url, label in [(NOTICES_URL, "County public notices")] + [(u, "Extra page") for u in OPTS.get("extra_pages", [])]:
        try:
            found += page_pdf_docs(fetch(url), url, label)
        except Exception as ex:  # noqa: BLE001
            log("page failed", url, ex)

    # de-dupe by base url, keep first (minutes entries carry the most metadata)
    seen, uniq = set(), []
    for d in found:
        b = split_version(d["url"])[0]
        if b not in seen:
            seen.add(b)
            uniq.append(d)
    uniq.sort(key=lambda d: d["meeting_date"] or "9999", reverse=True)

    new_alerts = 0
    for d in uniq:
        rid, change = upsert(d)
        with DB_LOCK, db() as c:
            r = c.execute("SELECT * FROM docs WHERE id=?", (rid,)).fetchone()
        md = date.fromisoformat(r["meeting_date"]) if r["meeting_date"] else date.today()
        in_window = md >= cutoff
        if change is None and not (r["status"] == "listed" and in_window):
            continue
        if not in_window and change == "new" and not initialized:
            continue                      # old history: list only, read on demand
        prev_hits = json.loads(r["hits"] or "[]")
        r = index_doc(rid)
        time.sleep(1)
        if not initialized or r is None or change is None:
            continue
        # skip the agenda PDF for the meeting whose agenda-page alert covers it
        if change == "new" and "agenda" in (r["title"] or "").lower() and r["meeting_date"] == adate \
                and not json.loads(r["hits"] or "[]"):
            continue
        hits = json.loads(r["hits"] or "[]")
        if change == "updated" and hits == prev_hits:
            continue                      # re-upload with nothing new to say
        if hits or OPTS.get("notify_every_new_document", True) or r["status"] == "error":
            alert_doc(r, change, prev_hits)
            new_alerts += 1

    # agenda text alert
    if agenda:
        h = hashlib.sha1(agenda.encode()).hexdigest()
        hits, _ = find_hits(agenda)
        kv_set("agenda_date", adate)
        kv_set("agenda_hits", ", ".join(hits) or "none")
        kv_set("agenda_text", agenda)
        if h != kv_get("agenda_hash"):
            kv_set("agenda_hash", h)
            if initialized:
                alert_agenda(agenda, adate, hits)
    else:
        log("WARNING: could not find agenda text on agenda page")

    if not initialized:
        kv_set("initialized", "1")
        with DB_LOCK, db() as c:
            n = c.execute("SELECT COUNT(*) FROM docs WHERE status='indexed'").fetchone()[0]
            hitrows = c.execute("SELECT title, meeting_date, hits FROM docs WHERE hits IS NOT NULL AND hits!='[]'"
                                " ORDER BY meeting_date DESC LIMIT 5").fetchall()
        lines = [f"• {fmt_date(x['meeting_date'])}: {x['title']} ({', '.join(json.loads(x['hits']))})" for x in hitrows]
        notify("Sewer Watch is running",
               f"Read {n} county documents from the last {OPTS.get('backfill_months')} months. "
               f"{len(hitrows) and 'Recent mentions:' or 'No sewer mentions found yet.'}\n" + "\n".join(lines)
               + f"\n\nCurrent agenda: {fmt_date(adate)} – sewer keywords: {kv_get('agenda_hits')}",
               [("Sewer Watch reader", INGRESS_PANEL), ("Fiscal Court agenda", AGENDA_URL)])
    log(f"check done; {new_alerts} document alerts")


def alert_agenda(agenda, adate, hits):
    items = agenda_items(agenda)
    rx = [p for _, p in kw_patterns()]
    sewer_items = [i for i in items if any(p.search(i) for p in rx)]
    when = fmt_date(adate)
    links = [("Read agenda", AGENDA_URL), ("Livestream", YOUTUBE_URL), ("Sewer Watch reader", INGRESS_PANEL)]
    with DB_LOCK, db() as c:
        pdf = c.execute("SELECT url FROM docs WHERE meeting_date=? AND lower(title) LIKE '%agenda%' LIMIT 1",
                        (adate,)).fetchone()
    if pdf:
        links.insert(1, ("Agenda PDF", pdf["url"]))
    if sewer_items:
        title = f"🚨 Sewer on the Fiscal Court agenda – {when}"
        body = "\n".join("• " + i for i in sewer_items)
    else:
        title = f"New Fiscal Court agenda – {when}"
        body = "No sewer items. On the agenda:\n" + "\n".join(
            "• " + i for i in items if re.match(r"(?:\d{1,2})\.\s", i))[:700]
    full = ("**Sewer-related items:**\n" + "\n".join("- " + i for i in sewer_items) + "\n\n" if sewer_items else "") \
        + "**Full agenda:**\n\n" + agenda.replace("\n", "  \n")
    notify(title, body, links[:4], full=full, important=bool(sewer_items))


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------
CHECK_NOW = threading.Event()


def scheduler():
    interval = max(30, int(OPTS.get("check_interval_minutes", 180))) * 60
    next_run = 0
    while True:
        now = time.time()
        if CHECK_NOW.is_set() or now >= next_run:
            CHECK_NOW.clear()
            run_check()
            next_run = time.time() + interval
        housekeeping()
        CHECK_NOW.wait(60)


def housekeeping():
    today = date.today().isoformat()
    # meeting-day reminder
    if OPTS.get("meeting_day_reminder", True) and kv_get("agenda_date") == today \
            and datetime.now().hour >= 7 and kv_get("reminded") != today:
        kv_set("reminded", today)
        hits = kv_get("agenda_hits", "none")
        notify("Fiscal Court meets today",
               f"Sewer keywords on today's agenda: {hits}. Regular meetings are 1st Monday 9:00am and "
               "3rd Monday 7:00pm at 28 E. Main St, Taylorsville, and are livestreamed.",
               [("Livestream", YOUTUBE_URL), ("Read agenda", AGENDA_URL)], important=hits != "none")
    # site health
    last = kv_get("last_success")
    if last and kv_get("site_alerted") != "1":
        try:
            if datetime.now() - datetime.fromisoformat(last) > timedelta(hours=48):
                kv_set("site_alerted", "1")
                notify("Sewer Watch can't read the county site",
                       "No successful check in 48 hours. The site may be down or redesigned - check the add-on log.",
                       [("County site", SITE)])
        except ValueError:
            pass


# --------------------------------------------------------------------------
# Reader UI (ingress)
# --------------------------------------------------------------------------
CSS = """
:root{--bg:#f6f7f9;--fg:#1c1f24;--mut:#667085;--card:#fff;--line:#e4e7ec;--acc:#0b6bcb;--hit:#fff3b0;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--fg:#e6e8eb;--mut:#98a2b3;--card:#1a1e24;--line:#2b313a;--acc:#5aa9ff;--hit:#5c4a00;--bad:#ff6b5b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:20px;margin:4px 0 12px}h2{font-size:16px;margin:20px 0 8px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
input[type=search]{flex:1;min-width:180px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
button,.btn{padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);cursor:pointer;font:inherit}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0}
.meta{color:var(--mut);font-size:13px}.tag{display:inline-block;background:var(--hit);border-radius:6px;padding:0 6px;margin:2px 4px 2px 0;font-size:12px}
.links a{margin-right:12px;font-size:13px}mark{background:var(--hit);color:inherit;padding:0 2px;border-radius:3px}
blockquote{margin:8px 0;padding:6px 10px;border-left:3px solid var(--acc);background:var(--bg);border-radius:4px}
pre{white-space:pre-wrap;word-wrap:break-word;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;font-size:13px}
.err{color:var(--bad)}label{font-size:14px;color:var(--mut)}
.picks{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:8px}.pick{color:var(--fg);font-size:15px;cursor:pointer}
.pick input{width:18px;height:18px;vertical-align:-3px}
.pg{margin:14px 0}.pg img{display:block;width:100%;max-width:900px;min-height:200px;background:#fff;border:1px solid var(--line);border-radius:6px;margin-top:4px}
details{margin:20px 0}summary{cursor:pointer;color:var(--mut)}
"""


def hl(text):
    esc = html.escape(text or "")
    for _, rx in kw_patterns():
        pat = re.compile(rx.pattern.replace(r"\s+", r"(?:\s|&nbsp;)+"), re.I)
        esc = pat.sub(lambda m: f"<mark>{m.group(0)}</mark>", esc)
    return esc


def page_shell(title, body):
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"""


def render_list(q, only_hits, flash=""):
    sql = "SELECT id,title,meeting_date,meeting_type,url,video,details,status,hits,snippets,source FROM docs WHERE 1=1"
    args = []
    if only_hits:
        sql += " AND hits IS NOT NULL AND hits!='[]'"
    if q:
        sql += " AND (text LIKE ? OR title LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY meeting_date DESC, id DESC LIMIT 150"
    with DB_LOCK, db() as c:
        rows = c.execute(sql, args).fetchall()
        total = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    adate, ahits = kv_get("agenda_date", ""), kv_get("agenda_hits", "")
    out = [f"<h1>Sewer Watch</h1>"]
    if flash:
        out.append(f'<div class="card">{html.escape(flash)}</div>')
    out.append(render_targets())
    out.append(f"""<div class="card"><b>Current agenda:</b> {html.escape(fmt_date(adate))} &nbsp;
<span class="meta">sewer keywords: {html.escape(ahits or '—')}</span> &nbsp; <a href="agenda">Read it</a> ·
<a href="{AGENDA_URL}" target="_blank">County page</a> · <a href="{YOUTUBE_URL}" target="_blank">Livestream</a> ·
<a href="{KDEP_URL}" target="_blank">State water permits</a>
<div class="meta">Last check: {html.escape(kv_get('last_success', 'never') or 'never')} · {total} documents tracked</div></div>""")
    out.append(f"""<form class="bar" method="get" action="./"><input type="search" name="q" value="{html.escape(q)}" placeholder="Search all document text (e.g. Top Flight, easement, draw)">
<label><input type="checkbox" name="hits" value="1" {'checked' if only_hits else ''} onchange="this.form.submit()"> only sewer mentions</label>
<button>Search</button></form>
<div class="bar"><form method="post" action="check"><button>Check county site now</button></form>
<form method="post" action="test"><button>Send test notification</button></form></div>""")
    if not rows:
        out.append('<div class="card meta">Nothing yet. The first check reads the last months of documents and can take a while (scanned PDFs are OCR\'d).</div>')
    for r in rows:
        hits = json.loads(r["hits"] or "[]")
        snips = json.loads(r["snippets"] or "[]")
        tags = "".join(f'<span class="tag">{html.escape(h)}</span>' for h in hits)
        status = "" if r["status"] == "indexed" else (
            '<span class="meta">not read yet (older than backfill) - open to read it</span>' if r["status"] == "listed"
            else '<span class="err">could not read PDF</span>')
        first = f"<blockquote>{hl(snips[0])}</blockquote>" if snips else ""
        links = f'<a href="doc?id={r["id"]}">Read</a><a href="{html.escape(r["url"])}" target="_blank">PDF</a>'
        if r["video"]:
            links += f'<a href="{html.escape(r["video"])}" target="_blank">Video</a>'
        if r["details"]:
            links += f'<a href="{html.escape(r["details"])}" target="_blank">Meeting page</a>'
        out.append(f"""<div class="card"><div><b><a href="doc?id={r['id']}">{html.escape(r['title'] or 'Document')}</a></b></div>
<div class="meta">{html.escape(fmt_date(r['meeting_date']))} {html.escape(r['meeting_type'] or '')} · {html.escape(r['source'] or '')}</div>
<div>{tags} {status}</div>{first}<div class="links">{links}</div></div>""")
    return page_shell("Sewer Watch", "".join(out))


def render_targets():
    chosen = set(get_targets())
    services = list_notify_services()
    known = {s for s, _ in services}
    services += [(s, nice_target(s) + " – not found in HA") for s in chosen if s not in known]
    if not services and LAST_HA_ERROR:
        boxes = (f'<div class="err">Couldn\'t get the list of phones from Home Assistant: '
                 f'{html.escape(LAST_HA_ERROR)}</div>')
    elif not services:
        boxes = '<div class="meta">Home Assistant reported no notify targets. Install the HA Companion app on your phone and log in, then reload this page.</div>'
    else:
        boxes = "".join(
            f'<label class="pick"><input type="checkbox" name="target" value="{html.escape(s)}"'
            f'{" checked" if s in chosen else ""}> {html.escape(label)}</label>'
            for s, label in services)
    warn = "" if chosen else '<div class="err"><b>Pick at least one phone, or you\'ll only get alerts inside Home Assistant.</b></div>'
    return f"""<form class="card" method="post" action="targets"><b>Send alerts to</b>{warn}
<div class="picks">{boxes}</div>
<div class="bar" style="margin:8px 0 0"><label>Or type one:</label>
<input type="text" name="manual" placeholder="e.g. mobile_app_pixel_8" style="padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);min-width:220px"></div>
<div class="bar" style="margin:8px 0 0">
<button>Save</button><button formaction="targets?test=1">Save &amp; send test</button></div></form>"""


def render_doc(doc_id):
    with DB_LOCK, db() as c:
        r = c.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
    if r is None:
        return page_shell("Not found", '<p><a href="./">← back</a></p><p>Not found.</p>')
    if r["status"] in ("listed", "error"):
        r = index_doc(doc_id) or r
    hits, snips, spages = find_hits(r["text"] or "", with_pages=True)
    links = f'<a href="{html.escape(r["url"])}" target="_blank">Open original PDF</a>'
    if r["video"]:
        links += f' · <a href="{html.escape(r["video"])}" target="_blank">Meeting video</a>'
    if r["details"]:
        links += f' · <a href="{html.escape(r["details"])}" target="_blank">Meeting page</a>'
    body = [f'<p><a href="./">← all documents</a></p><h1>{html.escape(r["title"] or "Document")}</h1>',
            f'<div class="meta">{html.escape(fmt_date(r["meeting_date"]))} {html.escape(r["meeting_type"] or "")} · '
            f'{html.escape(r["source"] or "")} · found {html.escape(r["found_at"] or "")}</div><p class="links">{links}</p>']
    if hits:
        body.append("<h2>Sewer-related mentions</h2>" + "".join(f'<span class="tag">{html.escape(h)}</span>' for h in hits))
        body += [f'<blockquote><a href="#p{p}"><b>Page {p}</b></a> &nbsp;{hl(s)}</blockquote>' for s, p in zip(snips, spages)]
    elif r["status"] == "indexed":
        body.append('<div class="card meta">No sewer keywords in this document.</div>')
    if r["status"] == "error":
        body.append('<div class="card err">Could not read this PDF. Use "Open original PDF".</div>')
    try:
        path = pdf_file(r["id"], r["url"])
        n = page_count(path) if path else 0
    except Exception as e:  # noqa: BLE001
        log("could not get PDF for reader:", e)
        n = 0
    if n:
        marked = hit_pages(r["text"])
        body.append(f"<h2>Document ({n} page{'s' if n != 1 else ''})</h2>")
        if marked:
            body.append('<div class="meta">Mentions are highlighted in yellow. Pages with mentions take a few seconds to prepare the first time.</div>')
        for p in range(1, min(n, 80) + 1):
            tag = ' <span class="tag">mentions</span>' if p in marked else ""
            body.append(f'<div class="pg" id="p{p}"><div class="meta">Page {p}{tag}</div>'
                        f'<img loading="lazy" src="page?id={r["id"]}&amp;p={p}&amp;v={html.escape(r["ver"] or "0")}" '
                        f'alt="Page {p}"></div>')
        if n > 80:
            body.append('<div class="meta">Only the first 80 pages are shown – open the original PDF for the rest.</div>')
    body.append('<details><summary>Searchable text (as read by OCR – tables and stamps can come out garbled)</summary>'
                f"<pre>{hl(r['text'] or '')}</pre></details>")
    return page_shell(r["title"] or "Document", "".join(body))


def render_agenda():
    agenda = kv_get("agenda_text", "")
    return page_shell("Current agenda", f"""<p><a href="./">← all documents</a></p><h1>Fiscal Court agenda – {html.escape(fmt_date(kv_get('agenda_date','')))}</h1>
<p class="links"><a href="{AGENDA_URL}" target="_blank">County agenda page</a> · <a href="{YOUTUBE_URL}" target="_blank">Livestream</a></p>
<pre>{hl(agenda) if agenda else 'No agenda read yet.'}</pre>""")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, flash):
        self.send_response(303)
        self.send_header("Location", "./?flash=" + quote(flash))
        self.end_headers()

    def do_GET(self):
        p = urlsplit(self.path)
        q = parse_qs(p.query)
        path = p.path.rstrip("/").rsplit("/", 1)[-1]
        try:
            if path == "doc":
                self._send(render_doc(int(q.get("id", ["0"])[0])))
            elif path == "page":
                with DB_LOCK, db() as c:
                    r = c.execute("SELECT * FROM docs WHERE id=?", (int(q.get("id", ["0"])[0]),)).fetchone()
                if r is None:
                    self._send("not found", 404, "text/plain")
                    return
                data = page_image(r, int(q.get("p", ["1"])[0]))
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            elif path == "agenda":
                self._send(render_agenda())
            elif path == "api":
                self._send(json.dumps({"agenda_date": kv_get("agenda_date"), "agenda_hits": kv_get("agenda_hits")}),
                           ctype="application/json")
            else:
                self._send(render_list(q.get("q", [""])[0].strip(), q.get("hits", ["0"])[0] == "1",
                                       q.get("flash", [""])[0]))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(page_shell("Error", f"<pre>{html.escape(str(e))}</pre>"), 500)

    def do_POST(self):
        sp = urlsplit(self.path)
        path = sp.path.rstrip("/").rsplit("/", 1)[-1]
        if path == "targets":
            n = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(n).decode() if n else "")
            raw = form.get("target", []) + [m.strip().lower().replace("notify.", "", 1)
                                            for m in form.get("manual", []) if m.strip()]
            chosen = list(dict.fromkeys(t for t in raw if re.fullmatch(r"[a-z0-9_]+", t)))
            kv_set("notify_targets", json.dumps(chosen))
            log("alert targets:", chosen)
            if chosen:
                ha_call("POST", "/services/persistent_notification/dismiss", {"notification_id": "sewer_watch_setup"})
            if parse_qs(sp.query).get("test") and chosen:
                threading.Thread(target=send_test, daemon=True).start()
                self._redirect(f"Saved. Test sent to {len(chosen)} target(s) - check your phone.")
            else:
                self._redirect("Saved." if chosen else "Saved - no phone selected, alerts will only appear inside Home Assistant.")
            return
        if path == "check":
            CHECK_NOW.set()
            self._redirect("Checking the county site now - refresh in a minute.")
        elif path == "test":
            threading.Thread(target=send_test, daemon=True).start()
            self._redirect("Test notification sent. If your phone didn't buzz, check notify_service in the add-on options.")
        else:
            self._send("not found", 404, "text/plain")


def send_test():
    with DB_LOCK, db() as c:
        r = c.execute("SELECT * FROM docs WHERE hits IS NOT NULL AND hits!='[]' ORDER BY meeting_date DESC LIMIT 1").fetchone()
    if r:
        hits = json.loads(r["hits"])
        snips = json.loads(r["snippets"] or "[]")
        notify(f"TEST – latest mention: {r['title']} ({fmt_date(r['meeting_date'])})",
               f"Page {first_hit_page(r['text'])} · Mentions: " + ", ".join(hits) + ("\n\n• " + snips[0] if snips else ""),
               doc_links(r), important=True, image=alert_image(r))
    else:
        notify("TEST – Sewer Watch works", "No sewer mentions found in the documents read so far.",
               [("Sewer Watch reader", INGRESS_PANEL), ("Fiscal Court agenda", AGENDA_URL)], important=True)


# --------------------------------------------------------------------------
def main():
    db_init()
    log("Sewer Watch starting; keywords:", ", ".join(OPTS["keywords"]))
    log("Home Assistant access token:", "present" if TOKEN else "MISSING - alerts and phone list will not work")
    if not DRY_RUN:
        detect_panel()
    port = int(os.environ.get("SW_PORT", "8099"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"reader UI on :{port} (panel {INGRESS_PANEL})")
    if not get_targets():
        ha_call("POST", "/services/persistent_notification/create", {
            "notification_id": "sewer_watch_setup",
            "title": "Sewer Watch: choose where alerts go",
            "message": f"Open **[Sewer Watch]({INGRESS_PANEL})** in the sidebar and tick your phone under "
                       "**Send alerts to**. It's already reading county documents in the meantime.",
        })
    if os.environ.get("SW_ONCE") == "1":
        run_check()
        return
    scheduler()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
