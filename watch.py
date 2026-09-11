#!/usr/bin/env python3
"""
simwatch - watches Slovenian classifieds for a used PC sim racing wheel
with pedals (handbrake = bonus), judges each new listing with Claude,
and pings you on Telegram and/or email.

Run it on a schedule (cron / GitHub Actions). It remembers what it has
already seen in seen.db, so you only ever get told about new stuff.
"""

import os
import re
import json
import time
import html
import sqlite3
import smtplib
import urllib.parse
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup

import trip

# ----------------------------------------------------------------------
# CONFIG - edit this bit
# ----------------------------------------------------------------------

# What you're hunting. This is fed to Claude verbatim, so write it like
# you'd tell a friend. Change it any time.
WANT = """
A used sim racing steering wheel setup for PC.

MUST have:
- works on PC (USB / Windows). A wheel that is console-only
  (PlayStation-only or Xbox-only, e.g. some Logitech G29 PS-only bundles
  are fine since G29 also works on PC, but a Thrustmaster T80 PS-only or
  an Xbox-only Fanatec CSL is not) must be REJECTED.
- comes WITH pedals. Wheel alone, or pedals alone, is a REJECT.
- is an actual force-feedback or at least proper sim racing wheel, not a
  toy/kids wheel, not a car steering wheel, not a boat/tractor wheel,
  not a bicycle handlebar.

NICE to have (raises the score, not required):
- a handbrake included
- a shifter included
- a wheel stand / rig / cockpit included
- load cell pedals
- known good brands: Logitech (G25/G27/G29/G920/G923/Pro), Thrustmaster
  (T150/T300/TMX/T248/T500/TS-XW), Fanatec (CSL, DD, Clubsport), Moza,
  Simagic, Simucube, Cammus, VRS, Asetek

REJECT if:
- it is clearly a shop/dealer selling new stock at retail price
- it is broken, "za dele", "ne dela", "okvarjen", "ne deluje"
- the price is obviously a placeholder (1 EUR, 123 EUR) with no detail
- it is a wanted ad ("kupim", "iščem") rather than someone selling
"""

MAX_PRICE_EUR = 500        # anything above this is dropped before Claude sees it
MIN_PRICE_EUR = 50         # below this it's an accessory, a scam, or a typo
NOTIFY_MIN_SCORE = 3       # only ping me for deal_score >= this (1-5)

# Price-vs-market rule. A listing that matches all requirements still only
# earns a ping if it undercuts the running median for its tier of wheel.
# Comparing a G29 against a Fanatec DD would be meaningless, so prices are
# tracked separately per tier.
UNDERCUT_FACTOR = 1.00     # 1.00 = at or below median. 0.90 = 10% below median.
MIN_SAMPLES_FOR_MEDIAN = 5 # below this, no baseline exists yet -> notify anyway
ALWAYS_NOTIFY_SCORE = 5    # a 5/5 gets through even if it's above median

# Don't buzz the phone in the middle of the night. Message still arrives,
# it just lands silently. Local hours, 24h.
QUIET_FROM, QUIET_TO = 23, 8

# Search pages to poll. Add or remove freely.
# Bolha search URL format: https://www.bolha.com/?ctl=search_ads&keywords=XXX
SEARCHES = [
    ("bolha", "https://www.bolha.com/?ctl=search_ads&keywords=volan+pedala"),
    ("bolha", "https://www.bolha.com/?ctl=search_ads&keywords=sim+racing"),
    ("bolha", "https://www.bolha.com/?ctl=search_ads&keywords=logitech+g29"),
    ("bolha", "https://www.bolha.com/?ctl=search_ads&keywords=logitech+g27"),
    ("bolha", "https://www.bolha.com/?ctl=search_ads&keywords=thrustmaster"),
    ("bolha", "https://www.bolha.com/?ctl=search_ads&keywords=fanatec"),
    ("bolha", "https://www.bolha.com/?ctl=search_ads&keywords=igralni+volan"),
    # salomon.si is deliberately absent: its robots.txt disallows automated
    # access, so the polite move is to check that one by hand. Their site has
    # its own search you can bookmark.
    # Croatia. Same company as Bolha, same page template. With the cap at
    # 150 km the whole Zagreb area is in range, which is where the actual
    # sim racing market is. Croatian hits get toll money added on top.
    ("njuskalo", "https://www.njuskalo.hr/?ctl=search_ads&keywords=volan+pedale"),
    ("njuskalo", "https://www.njuskalo.hr/?ctl=search_ads&keywords=sim+racing"),
    ("njuskalo", "https://www.njuskalo.hr/?ctl=search_ads&keywords=logitech+g29"),
    ("njuskalo", "https://www.njuskalo.hr/?ctl=search_ads&keywords=thrustmaster"),
    ("njuskalo", "https://www.njuskalo.hr/?ctl=search_ads&keywords=fanatec"),
]

# Secrets come from environment variables. Never hardcode them here.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# ntfy.sh - free push, no account, no phone number. Pick an unguessable topic
# name: anyone who knows it can read your alerts.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")           # e.g. smtp.gmail.com
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")           # app password, not your real one
EMAIL_TO = os.environ.get("EMAIL_TO", "")

DB_PATH = os.environ.get("SIMWATCH_DB", "seen.db")
MODEL = "claude-haiku-4-5-20251001"

# --- Any other LLM -----------------------------------------------------
# Almost every provider speaks the OpenAI chat-completions format, so one
# code path covers Mistral, Groq, OpenRouter, DeepSeek, OpenAI, Ollama,
# LM Studio, llama.cpp and anything else with a /v1/chat/completions route.
# Set these three and the Anthropic path is skipped entirely.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")   # e.g. https://api.mistral.ai/v1
LLM_MODEL = os.environ.get("LLM_MODEL", "")         # e.g. mistral-small-latest
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")     # local servers: any string

HEADERS = {
    "User-Agent": "simwatch/1.0 (personal listing watcher; low volume)",
    "Accept-Language": "sl-SI,sl;q=0.9,en;q=0.8",
}

# ----------------------------------------------------------------------
# storage
# ----------------------------------------------------------------------


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS seen ("
        " id TEXT PRIMARY KEY, site TEXT, title TEXT, price REAL,"
        " url TEXT, score INTEGER, ts INTEGER)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS prices ("
        " id TEXT PRIMARY KEY, tier TEXT, price REAL, ts INTEGER)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS health (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS reposts ("
                " fp TEXT PRIMARY KEY, url TEXT, ts INTEGER)")
    trip.init(con)
    return con


def fingerprint(title, price):
    """Same wheel relisted next week gets a fresh listing id but keeps its
    wording. Normalise hard and bucket the price so a 210 -> 200 price drop
    still counts as the same ad."""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    stop = {"prodam", "prodajem", "nov", "novo", "rabljen", "rabljeno", "kot",
            "zelo", "malo", "in", "z", "s", "za", "the", "komplet", "set"}
    words = sorted(w for w in words if w not in stop and len(w) > 2)
    bucket = int(price // 25) if price else -1
    return " ".join(words[:8]) + f"|{bucket}"


def is_repost(con, listing, days=60):
    fp = fingerprint(listing["title"], listing["price"])
    cutoff = int(time.time()) - days * 86400
    row = con.execute("SELECT url, ts FROM reposts WHERE fp=? AND ts>?",
                      (fp, cutoff)).fetchone()
    if row and row[0] != listing["url"]:
        return row[0]
    return None


def remember_fingerprint(con, listing):
    con.execute("INSERT OR REPLACE INTO reposts VALUES (?,?,?)",
                (fingerprint(listing["title"], listing["price"]),
                 listing["url"], int(time.time())))
    con.commit()


def record_price(con, listing, tier):
    """Every listing that meets the requirements feeds the baseline,
    whether or not it was worth a notification."""
    if listing["price"]:
        con.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)",
                    (listing["id"], tier, listing["price"], int(time.time())))
        con.commit()


def median_price(con, tier, days=120):
    """Median asking price for this tier over the last few months.
    Median, not mean - one €900 outlier shouldn't drag the bar up."""
    cutoff = int(time.time()) - days * 86400
    rows = [r[0] for r in con.execute(
        "SELECT price FROM prices WHERE tier=? AND ts>? ORDER BY price", (tier, cutoff))]
    if len(rows) < MIN_SAMPLES_FOR_MEDIAN:
        return None, len(rows)
    n = len(rows)
    mid = n // 2
    med = rows[mid] if n % 2 else (rows[mid - 1] + rows[mid]) / 2
    return med, n


def already_seen(con, listing_id):
    return con.execute("SELECT 1 FROM seen WHERE id=?", (listing_id,)).fetchone() is not None


def mark_seen(con, listing, score):
    con.execute(
        "INSERT OR REPLACE INTO seen VALUES (?,?,?,?,?,?,?)",
        (listing["id"], listing["site"], listing["title"], listing["price"],
         listing["url"], score, int(time.time())),
    )
    con.commit()


# ----------------------------------------------------------------------
# scraping
# ----------------------------------------------------------------------

PRICE_RE = re.compile(r"(\d[\d\.\s]*(?:,\d{1,2})?)\s*(?:€|EUR)", re.I)


def parse_price(text):
    """'1.250,00 €' -> 1250.0"""
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace("\xa0", "").replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def extract_listings(site, page_html, base_url):
    """
    Deliberately dumb + tolerant parser: find every anchor that looks like a
    listing, take its text as the title, and look for a price in the nearest
    enclosing block. Survives most markup changes.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    out = {}

    # site-specific hint for what a listing link looks like
    # Must end in -oglas-<digits>. Plain "/oglas" also matched /oglasevanje,
    # /oglasi-skupnost and every other footer link on the page.
    patterns = {
        "bolha": re.compile(r"-oglas-\d{4,}"),
        "salomon": re.compile(r"/oglas|/oglasi/|/artikel/"),
        "njuskalo": re.compile(r"-oglas-\d{4,}"),
    }
    pat = patterns.get(site, re.compile(r"/oglas"))

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not pat.search(href):
            continue
        title = " ".join(a.get_text(" ", strip=True).split())
        if len(title) < 8:
            continue
        url = urllib.parse.urljoin(base_url, href)

        # listing id = numeric chunk in the url, else the url itself
        m = re.search(r"(\d{5,})", url)
        listing_id = f"{site}:{m.group(1)}" if m else f"{site}:{url}"

        # price: walk up a few parents looking for a EUR figure
        price = None
        node = a
        for _ in range(4):
            node = node.parent
            if node is None:
                break
            price = parse_price(node.get_text(" ", strip=True))
            if price:
                break

        prev = out.get(listing_id)
        if prev and prev["price"] and not price:
            continue
        out[listing_id] = {
            "id": listing_id,
            "site": site,
            "title": html.unescape(title)[:300],
            "price": price,
            "url": url,
        }

    return list(out.values())


# ----------------------------------------------------------------------
# the smart bit
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# offline filter - used automatically when no ANTHROPIC_API_KEY is set.
# Dumber than Claude, free, and needs no account. Catches the obvious stuff.
# ----------------------------------------------------------------------

DEALBREAKERS = re.compile(
    r"\b(za dele|ne dela|ne deluje|okvarjen|pokvarjen|neispravan|za dijelove|"
    r"kupim|iscem|iščem|isc?em|tražim|trazim|povprasevanje)\b", re.I)

CONSOLE_ONLY = re.compile(
    r"\b(samo za (ps[45]|playstation|xbox)|ps[45] only|xbox only|"
    r"ne dela na pc|ni za pc|samo playstation|samo xbox)\b", re.I)

PEDALS = re.compile(r"\b(stopalk\w*|pedal\w*|papučic\w*)\b", re.I)
# "za xbox one in xbox series" never says "only", but if a listing names a
# console and never mentions PC, and the brand is not a known PC-compatible
# one, it is a console wheel.
CONSOLE_WORD = re.compile(r"\b(xbox|playstation|ps ?[12345])\b", re.I)
PC_WORD = re.compile(r"\b(pc|windows|računalnik\w*|racunalnik\w*|steam)\b", re.I)
HANDBRAKE = re.compile(r"\b(ro[cč]n\w* zavor\w*|ru[cč]n\w* ko[cč]nic\w*|handbrake|hand brake)\b", re.I)
SHIFTER = re.compile(r"\b(menjalnik\w*|shifter|mjenja[cč]\w*)\b", re.I)
# Slovenian says "brez stopalk" = WITHOUT pedals. Without this the word
# "stopalk" alone would score it as if the pedals were included.
NO_PEDALS = re.compile(r"\b(brez|bez)\s+(stopalk\w*|pedal\w*|papu[cč]ic\w*)", re.I)
WANTED_JUNK = re.compile(r"\b(otro[sš]k\w+|igra[cč]\w*|traktor\w*|avtodom\w*|"
                         r"bicikl\w*|kosilnic\w*|[cč]oln\w*)\b", re.I)

TIERS = [
    ("high", re.compile(r"\b(fanatec (dd|dd\+|podium|csl dd)|dd1|dd2|moza (r3|r5|r9|r12|r16|r21)|"
                        r"simucube|simagic|asetek|vrs direct|cammus|t598|t818|"
                        r"direct drive|dirketni pogon)\b", re.I)),
    ("mid",  re.compile(r"\b(t300|t500|ts-?xw|ts-?pc|csl elite|clubsport|csr|"
                        r"logitech pro|g pro (wheel|racing)|t-?gt)\b", re.I)),
    ("entry", re.compile(r"\b(g25|g27|g29|g920|g923|t150|t128|t248|tmx|"
                         r"driving force|force feedback|ffb)\b", re.I)),
]

# Cheap non-force-feedback toys. They technically have a wheel and pedals but
# they are not what anyone means by a sim rig.
JUNK_BRANDS = re.compile(r"\b(speedlink|tracer|overdrive|esperanza|genesis seaborg|"
                         r"trust gxt|natec|defender|subsonic|ff380)\b", re.I)

# A stand or cockpit is not a wheel, even though the words appear.
STAND_ONLY = re.compile(r"\b(stalak|držač|drzac|stojalo|nosilec|držalo|drzalo|"
                        r"playseat|rig|kokpit|cockpit)\b", re.I)

TOWNS = re.compile(
    r"\b(Ljubljana|Maribor|Celje|Kranj|Koper|Velenje|Novo mesto|Ptuj|Trbovlje|"
    r"Kamnik|Jesenice|Nova Gorica|Domžale|Škofja Loka|Murska Sobota|Postojna|"
    r"Grosuplje|Vrhnika|Litija|Krško|Brežice|Slovenj Gradec|Ravne|Idrija|Ajdovščina|"
    r"Sežana|Izola|Piran|Portorož|Ilirska Bistrica|Logatec|Cerknica|Ribnica|Kočevje|"
    r"Trebnje|Zagorje|Hrastnik|Sevnica|Lendava|Ormož|Slovenska Bistrica|Radovljica|"
    r"Bled|Bohinj|Tolmin|Bovec|Zagreb|Karlovac|Varaždin|Rijeka|Samobor|Sisak|Krapina)\b")


def local_judge(listing, detail_text=""):
    """Keyword filter. No API key, no account, no ID scan. Roughly 80% as good
    as Claude on obvious listings and noticeably worse on vague ones, so it
    leans cautious: anything it can't confirm has pedals gets rejected."""
    blob = f"{listing['title']} {detail_text}"

    if DEALBREAKERS.search(blob):
        return {"match": False, "deal_score": 1, "reason": "broken / for parts / wanted ad"}
    if CONSOLE_ONLY.search(blob):
        return {"match": False, "deal_score": 1, "reason": "console only"}
    if WANTED_JUNK.search(blob) or JUNK_BRANDS.search(blob):
        return {"match": False, "deal_score": 1, "reason": "toy / wrong kind of wheel"}
    named_model = any(rx.search(blob) for _, rx in TIERS)
    if STAND_ONLY.search(blob) and not named_model:
        return {"match": False, "deal_score": 1, "reason": "stand or rig, not a wheel"}
    if NO_PEDALS.search(blob):
        return {"match": False, "deal_score": 1, "reason": "explicitly sold without pedals"}

    has_pedals = bool(PEDALS.search(blob))
    if not has_pedals:
        return {"match": False, "deal_score": 1, "reason": "no pedals mentioned"}

    tier, model = "unknown", None
    if (CONSOLE_WORD.search(blob) and not PC_WORD.search(blob)
            and not any(rx.search(blob) for _, rx in TIERS)):
        return {"match": False, "deal_score": 1, "reason": "console only"}
    for name, rx in TIERS:
        m = rx.search(blob)
        if m:
            tier, model = name, m.group(0)
            break

    has_hb = bool(HANDBRAKE.search(blob))
    score = 3                      # a wheel with pedals clears the bar by default
    if has_hb:
        score += 1
    if SHIFTER.search(blob):
        score += 1
    if tier == "high":
        score += 1                 # direct drive with pedals is always worth a look
    score = max(1, min(5, score))

    town = TOWNS.search(blob)
    msg = ("Zivjo, me zanima ce je volan se na voljo? Ali stopalke delujejo brez "
           "tezav in ali komplet deluje na PC? Lahko pridem osebno po njega in "
           "placam z gotovino. Hvala za odgovor!")

    return {"match": True, "deal_score": score, "has_pedals": True,
            "has_handbrake": has_hb, "pc_compatible": None,
            "guessed_model": model, "tier": tier,
            "location": town.group(0) if town else None,
            "opening_message": msg,
            "reason": f"keyword match{' + handbrake' if has_hb else ''} (offline filter)"}


SYSTEM = """You screen used-marketplace listings for a buyer in Slovenia.
Listings are in Slovenian. You will be given what the buyer wants, then one listing.

Reply with ONLY a JSON object, no markdown fences, no preamble:
{"match": true|false,
 "deal_score": 1-5,
 "has_pedals": true|false|null,
 "has_handbrake": true|false|null,
 "pc_compatible": true|false|null,
 "guessed_model": "string or null",
 "tier": "entry"|"mid"|"high"|"unknown",
 "location": "town/city named in the listing, or null",
 "opening_message": "short message to send the seller, see below",
 "reason": "one short sentence in English"}

deal_score meaning: 1 = not what they want, 2 = matches but bad price,
3 = solid fair-priced match, 4 = good deal, 5 = drop everything and message now.
If required things are missing or unclear, match=false.
tier: "entry" = gear/geared FFB, Logitech G25/27/29/920/923, Thrustmaster T150/TMX/T248.
"mid" = belt driven, Thrustmaster T300/T500/TS-XW, Fanatec CSL Elite, Logitech Pro entry.
"high" = direct drive, Fanatec DD/Podium, Moza R9/R12/R16, Simucube, Simagic, Asetek.
Judge tier from the wheel itself, ignoring what else is bundled with it.

location: the town or municipality the item is in. Slovenian listings usually
state it. Give the bare town name, no region, no "okolica". null if absent.

opening_message: 3-4 sentences the buyer can send as-is, in the same language
as the listing (Slovenian for bolha/salomon, Croatian for njuskalo). Informal
but polite, "ti" form. It should: say the item is still wanted, ask if it is
still available, ask the one most useful missing question about THIS listing
(does it include pedals / does it work on PC / why selling / how worn are the
pedals), and offer to collect in person and pay cash. Do not invent a lower
price offer. Do not use emoji. Do not sign a name.
Useful Slovenian: 'volan'=wheel, 'stopalke'/'pedala'=pedals, 'ročna zavora'=handbrake,
'menjalnik'=shifter, 'ohranjen'=well kept, 'za dele'=for parts, 'ne dela'=broken,
'kupim'/'iščem'=wanted ad, 'komplet'=set/bundle."""


def build_prompt(listing, detail_text):
    return (
        f"BUYER WANTS:\n{WANT}\n\n"
        f"LISTING:\nsite: {listing['site']}\n"
        f"title: {listing['title']}\n"
        f"price: {listing['price']} EUR\n"
        f"url: {listing['url']}\n"
        f"description: {detail_text[:3000] or '(none fetched)'}"
    )


def parse_verdict(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    # some models wrap the JSON in chatter - grab the outermost object
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"match": False, "deal_score": 1,
                "reason": f"unparseable model reply: {text[:120]}"}


def judge_anthropic(listing, detail_text):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 400, "system": SYSTEM,
              "messages": [{"role": "user",
                            "content": build_prompt(listing, detail_text)}]},
        timeout=60)
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", [])
                   if b.get("type") == "text")
    return parse_verdict(text)


def post_with_retry(url, **kw):
    """Free API tiers rate-limit hard. Honour Retry-After, back off, try again."""
    delay = 4
    for attempt in range(4):
        r = requests.post(url, **kw)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        wait = int(r.headers.get("Retry-After") or delay)
        print(f"  rate limited, waiting {wait}s (attempt {attempt + 1}/4)")
        time.sleep(wait)
        delay *= 2
    r.raise_for_status()
    return r


def judge_openai_compatible(listing, detail_text):
    r = post_with_retry(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY or 'none'}",
                 "Content-Type": "application/json"},
        json={"model": LLM_MODEL,
              "max_tokens": 400,
              "temperature": 0,
              "messages": [{"role": "system", "content": SYSTEM},
                           {"role": "user",
                            "content": build_prompt(listing, detail_text)}]},
        timeout=120)  # local models on a laptop can be slow
    return parse_verdict(r.json()["choices"][0]["message"]["content"])


def sanity_check(verdict, listing, detail_text):
    """Small models omit fields and miss obvious rejects. Fill the gaps from
    the keyword filter and veto anything that breaks a hard requirement,
    whatever the model claimed."""
    blob = f"{listing['title']} {detail_text}"

    if not verdict.get("tier") or verdict.get("tier") == "unknown":
        verdict["tier"] = "unknown"
        for name, rx in TIERS:
            if rx.search(blob):
                verdict["tier"] = name
                if not verdict.get("guessed_model"):
                    verdict["guessed_model"] = rx.search(blob).group(0)
                break

    console_no_pc = (CONSOLE_WORD.search(blob) and not PC_WORD.search(blob)
                     and verdict["tier"] == "unknown")
    if CONSOLE_ONLY.search(blob) or console_no_pc:
        verdict.update(match=False, deal_score=1, reason="console only (veto)")
    elif NO_PEDALS.search(blob):
        verdict.update(match=False, deal_score=1, reason="sold without pedals (veto)")
    elif DEALBREAKERS.search(blob):
        verdict.update(match=False, deal_score=1, reason="broken / wanted ad (veto)")

    if verdict.get("has_handbrake") is None:
        verdict["has_handbrake"] = bool(HANDBRAKE.search(blob))
    if verdict.get("location") is None:
        m = TOWNS.search(blob)
        verdict["location"] = m.group(0) if m else None
    return verdict


def judge(listing, detail_text=""):
    """Whichever brain is configured. Falls back to the offline keyword
    filter rather than crashing, so a dead API never stops the watcher."""
    try:
        if LLM_BASE_URL and LLM_MODEL:
            return sanity_check(judge_openai_compatible(listing, detail_text),
                                listing, detail_text)
        if ANTHROPIC_API_KEY:
            return sanity_check(judge_anthropic(listing, detail_text),
                                listing, detail_text)
    except Exception as e:
        print(f"  LLM call failed ({e}) - using offline filter for this one")
    return local_judge(listing, detail_text)


# ----------------------------------------------------------------------
# notifications
# ----------------------------------------------------------------------


def notify(listing, verdict, med, n_samples, ride):
    stars = "*" * int(verdict.get("deal_score", 0))
    price = f"{listing['price']:.0f} EUR" if listing["price"] else "price not listed"

    if med and listing["price"]:
        delta = (listing["price"] - med) / med * 100
        market = (f"{abs(delta):.0f}% {'BELOW' if delta < 0 else 'above'} the "
                  f"{med:.0f} EUR median for {verdict.get('tier','?')}-tier "
                  f"(from {n_samples} listings)")
    else:
        market = f"no price baseline yet ({n_samples} {verdict.get('tier','?')}-tier samples so far)"

    kit = []
    if verdict.get("has_pedals"):
        kit.append("pedals")
    if verdict.get("has_handbrake"):
        kit.append("HANDBRAKE")
    kit = ", ".join(kit) or "?"

    if ride:
        if ride["extra_eur"]:
            cost_line = (f"= {ride['fuel_eur']:.2f} EUR fuel + {ride['extra_eur']:.0f} EUR tolls "
                         f"= {ride['total_eur']:.2f} EUR")
        else:
            cost_line = f"= {ride['fuel_eur']:.2f} EUR"
        commute_line = (
            f"COMMUTE {ride['place']}, {ride['km_one_way']:.0f} km each way\n"
            f"        {ride['km_round']:.0f} km round trip = {ride['litres']:.1f} L "
            f"{cost_line}\n"
            f"        diesel at {ride['price_per_litre']:.3f} EUR/l ({ride['source']})\n"
        )
        if listing["price"]:
            landed = listing["price"] + ride["total_eur"]
            commute_line += f"LANDED  {landed:.0f} EUR all in\n"
    else:
        commute_line = "COMMUTE location unknown - ask the seller\n"

    subject = f"[{stars}] {price} - {listing['title'][:60]}"
    text = (
        f"{listing['title']}\n\n"
        f"PRICE   {price}  ({market})\n"
        f"MODEL   {verdict.get('guessed_model') or '?'}\n"
        f"INCLUDES {kit}\n"
        f"{commute_line}"
        f"SCORE   {verdict.get('deal_score')}/5 - {verdict.get('reason', '')}\n"
        f"SITE    {listing['site']}\n\n"
        f"{listing['url']}\n\n"
        f"--- copy-paste to seller ---\n"
        f"{verdict.get('opening_message') or '(none generated)'}"
    )

    hour = time.localtime().tm_hour
    quiet = hour >= QUIET_FROM or hour < QUIET_TO
    push(text, title=subject, url=listing["url"], quiet=quiet)

    print(f"  NOTIFIED: {listing['title'][:50]} | {price} | {market}")


def push(text, title=None, url=None, quiet=False):
    """Fan the message out to whichever channels are configured."""
    sent = False

    if NTFY_TOPIC:
        try:
            headers = {"Priority": "low" if quiet else "default"}
            if title:
                headers["Title"] = title.encode("ascii", "replace").decode()
            if url:
                headers["Click"] = url
            requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}",
                          data=text.encode("utf-8"), headers=headers, timeout=20)
            sent = True
        except Exception as e:
            print(f"  ntfy failed: {e}")

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                      "disable_web_page_preview": False,
                      "disable_notification": quiet},
                timeout=20)
            sent = True
        except Exception as e:
            print(f"  telegram failed: {e}")

    if DISCORD_WEBHOOK:
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": text[:1900]}, timeout=20)
            sent = True
        except Exception as e:
            print(f"  discord failed: {e}")

    if SMTP_HOST and EMAIL_TO:
        try:
            msg = EmailMessage()
            msg["Subject"] = title or "simwatch"
            msg["From"] = SMTP_USER or EMAIL_TO
            msg["To"] = EMAIL_TO
            msg.set_content(text)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.starttls()
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
            sent = True
        except Exception as e:
            print(f"  email failed: {e}")

    if not sent:
        print("  !! no notification channel configured - set NTFY_TOPIC")


def alert_plain(text):
    """Out-of-band message for 'your scraper is broken' type news."""
    push(text, title="simwatch")


# ----------------------------------------------------------------------


def main():
    con = db()
    total_new = 0
    total_hits = 0
    parsed_anything = False

    # First ever run: learn what the market looks like without setting the
    # phone on fire. Everything gets graded and recorded, nothing is sent.
    seeding = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
    if seeding:
        print("first run - building the price baseline, no alerts this time\n")

    for site, url in SEARCHES:
        try:
            page = fetch(url)
        except Exception as e:
            print(f"[{site}] fetch failed {url}: {e}")
            continue

        listings = extract_listings(site, page, url)
        if not listings:
            # an empty result mid-run usually means throttling, not an empty
            # category. Wait it out and ask once more before giving up.
            print(f"[{site}] empty result, backing off 20s and retrying")
            time.sleep(20)
            try:
                page = fetch(url)
                listings = extract_listings(site, page, url)
            except Exception as e:
                print(f"[{site}] retry failed: {e}")
        print(f"[{site}] {len(listings)} listings on {url}")
        if listings:
            parsed_anything = True
        else:
            fn = f"debug_{site}_{abs(hash(url)) % 10000}.html"
            with open(fn, "w", encoding="utf-8") as f:
                f.write(page)
            print(f"  nothing parsed - saved {fn} for inspection")

        for l in listings:
            if already_seen(con, l["id"]):
                continue
            total_new += 1

            if l["price"] is not None and not (MIN_PRICE_EUR <= l["price"] <= MAX_PRICE_EUR):
                mark_seen(con, l, 0)
                continue

            detail = ""
            try:
                time.sleep(2)
                detail_html = fetch(l["url"])
                dsoup = BeautifulSoup(detail_html, "html.parser")
                for tag in dsoup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                detail = " ".join(dsoup.get_text(" ", strip=True).split())[:4000]
                if l["price"] is None:
                    l["price"] = parse_price(detail)
            except Exception as e:
                print(f"  detail fetch failed: {e}")

            # price could only be read from the detail page - re-check the floor
            if l["price"] is not None and not (MIN_PRICE_EUR <= l["price"] <= MAX_PRICE_EUR):
                mark_seen(con, l, 0)
                continue

            # relisted by the same seller? already told you about it once
            old_url = is_repost(con, l)
            if old_url:
                mark_seen(con, l, 0)
                print(f"  repost of {old_url} - skipping")
                continue

            verdict = judge(l, detail)
            score = int(verdict.get("deal_score", 0) or 0)
            tier = verdict.get("tier") or "unknown"
            mark_seen(con, l, score)

            if not verdict.get("match") or score < NOTIFY_MIN_SCORE:
                print(f"  skip ({score}/5): {l['title'][:60]} - {verdict.get('reason','')}")
                time.sleep(2)
                continue

            # it meets the requirements, so its price is market data either way
            record_price(con, l, tier)
            remember_fingerprint(con, l)
            med, n = median_price(con, tier)

            ride = trip.commute(con, verdict.get("location"), site)
            if ride and ride["too_far"]:
                print(f"  too far: {ride['place']} is {ride['km_one_way']:.0f} km away")
                time.sleep(2)
                continue

            if l["price"] is None:
                print(f"  no price listed, skipping: {l['title'][:50]}")
                time.sleep(2)
                continue

            if seeding:
                print(f"  seeded {tier:7} {l['price']:.0f} EUR  {l['title'][:45]}")
                time.sleep(2)
                continue

            cheap = med is None or l["price"] <= med * UNDERCUT_FACTOR
            if cheap or score >= ALWAYS_NOTIFY_SCORE:
                total_hits += 1
                notify(l, verdict, med, n, ride)
            else:
                shown = f"{l['price']:.0f}" if l["price"] else "no price"
                print(f"  matched but pricey: {l['title'][:50]} "
                      f"{shown} vs {med:.0f} median ({tier})")

            time.sleep(2)

        time.sleep(10)   # be gentle between search pages

    # fail loudly instead of going quiet for weeks after a site redesign
    prev = con.execute("SELECT v FROM health WHERE k='dry_runs'").fetchone()
    dry = 0 if parsed_anything else int(prev[0]) + 1 if prev else 1
    con.execute("INSERT OR REPLACE INTO health VALUES ('dry_runs', ?)", (str(dry),))
    con.commit()
    if dry == 3:
        alert_plain("simwatch: parsed 0 listings 3 runs in a row. "
                    "Site markup probably changed - check debug_*.html")

    if seeding:
        n = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        print(f"\nbaseline seeded with {n} listings. Next run starts alerting.")
    else:
        print(f"\ndone: {total_new} new listings, {total_hits} worth your time")


if __name__ == "__main__":
    main()
