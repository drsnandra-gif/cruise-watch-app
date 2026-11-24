#!/usr/bin/env python3
"""Watch cruise-deal feeds and push a notification when a family deal looks cheap.

Stdlib only (Python 3.11+). Run on a schedule: see README.md.
"""
import argparse, email.utils, hashlib, html, os, re, smtplib, sqlite3, sys, time, tomllib
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).parent
PRICE_RE = re.compile(r"\$\s?(\d{1,2},?\d{3}|\d{2,4})(?!\d)")
NIGHTS_RE = re.compile(r"(\d{1,2})[\s-]*(?:night|nt\b|day)", re.I)
ATOM = "{http://www.w3.org/2005/Atom}"


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_feed(xml_bytes, source):
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.iter("item"):
        link = clean(it.findtext("link"))
        out.append(dict(source=source, title=clean(it.findtext("title")),
                        text=clean(it.findtext("description")), link=link,
                        date=it.findtext("pubDate")))
    for en in root.iter(ATOM + "entry"):
        lk = en.find(ATOM + "link")
        out.append(dict(source=source, title=clean(en.findtext(ATOM + "title")),
                        text=clean(en.findtext(ATOM + "summary") or en.findtext(ATOM + "content")),
                        link=lk.get("href") if lk is not None else "",
                        date=en.findtext(ATOM + "updated") or en.findtext(ATOM + "published")))
    return out


def parse_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _n(n, singular, plural=None):
    word = singular if n == 1 else (plural or singular + "s")
    return f"{n} {word}"


def family_blurb(cfg, est):
    """Rough total and cabin note. preferred_cabin is named when configured."""
    p = cfg["party"]
    pref = str((cfg.get("search") or {}).get("preferred_cabin") or "").strip()
    cabin_note = f"Needs {_n(p['cabins'], 'cabin')}"
    if pref:
        cabin_note += f" ({pref} preferred)"
    cabin_note += ", so confirm quad/family rates."
    return (f"Rough family total ({_n(p['adults'], 'adult')} + "
            f"{_n(p['children'], 'child', 'children')} at "
            f"{int(p['child_price_factor']*100)}%): ~${est:,} before taxes/fees/gratuities. "
            f"{cabin_note}")


def preferred_cabin(cfg):
    return str((cfg.get("search") or {}).get("preferred_cabin") or "").strip()


def price_cap(entry, cabin, cfg):
    """Budget for this card. Preferred-cabin fares use preferred_max_cpd when set."""
    pref = preferred_cabin(cfg)
    if pref and str(cabin or "").strip().lower() == pref.lower() and entry.get("preferred_max_cpd"):
        return entry["preferred_max_cpd"]
    return entry["max_cpd"]


def fetch_cap(entry, cabin):
    """Server-side max_cpd. Wide enough to include preferred-cabin fares when that class is requested."""
    if cabin and entry.get("preferred_max_cpd"):
        return entry["preferred_max_cpd"]
    return entry["max_cpd"]


def evaluate(item, cfg, stats=None):
    """Return a deal dict if the item matches a destination and looks cheap, else None."""
    stats = stats if stats is not None else {}
    blob = f"{item['title']} {item['text']}".lower()
    dest = next((d for d in cfg["destinations"] if any(k in blob for k in d["keywords"])), None)
    if not dest:
        return None
    stats["destination"] = stats.get("destination", 0) + 1
    s = cfg["search"]
    if not any(w in blob for w in s["deal_words"]):
        return None
    stats["deal_words"] = stats.get("deal_words", 0) + 1
    prices = [int(p.replace(",", "")) for p in PRICE_RE.findall(f"{item['title']} {item['text']}")]
    prices = [p for p in prices if 100 <= p <= 30000]
    if not prices:
        return None
    stats["priced"] = stats.get("priced", 0) + 1
    price = min(prices)  # headline "from $X" fare, assumed per person, cheapest cabin
    m = NIGHTS_RE.search(blob)
    nights = int(m.group(1)) if m else None
    if nights:
        pp_night = price / nights
        if pp_night > dest["max_pp_night"]:
            return None
        verdict = f"${pp_night:.0f}/person/night (limit ${dest['max_pp_night']})"
    else:
        if price > s["unknown_nights_max_price"]:
            return None
        pp_night = None
        verdict = "night count not stated, check itinerary"
    p = cfg["party"]
    est_total = price * (p["adults"] + p["children"] * p["child_price_factor"])
    return dict(dest=dest["name"], price=price, nights=nights, pp_night=pp_night,
                verdict=verdict, est_total=round(est_total), **item)


# ---- CruiseSheet: real sailings with per-person fares and per-night prices ----
CARD_SPLIT = re.compile(r'(?=<div class="cruise-card )')
SHEET = "https://cruisesheet.com"


def _first(rx, text, flags=re.S):
    m = re.search(rx, text, flags)
    return html.unescape(m.group(1)).strip() if m else ""


def parse_cruisesheet(page):
    """Parse listing cards: title, link, date, ship, itinerary, per-person fare, per-night fare, cabin."""
    out = []
    for chunk in CARD_SPLIT.split(page):
        if not chunk.startswith('<div class="cruise-card '):
            continue
        price = _first(r'leading-none">\s*\$([\d,]+)', chunk)
        cpd = _first(r'\$([\d,]+(?:\.\d+)?) per day', chunk)
        m = re.search(r'<a href="([^"]+)"[^>]*class="text-lg[^"]*"[^>]*>\s*([^<]+?)\s*</a>', chunk)
        if not (price and cpd and m):
            continue
        route = _first(r'flex-wrap items-center gap-1 text-xs">(.*?)</div>', chunk)
        ports = [html.unescape(x).strip() for x in re.findall(r">([^<>]+)</span>", route) if x.strip() != "→"]
        out.append(dict(
            link=m.group(1), title=html.unescape(m.group(2)),
            price=int(price.replace(",", "")), cpd=float(cpd.replace(",", "")),
            date=_first(r'class="cruise-date">([^<]+)<', chunk),
            line=_first(r'data-line="([^"]+)"', chunk),
            ship=_first(r'<span class="font-normal">([^<]+)</span>', chunk),
            cabin=_first(r'cruise-class-stripe[^>]*>\s*<span[^>]*>([^<]+)</span>', chunk),
            ribbon=_first(r'class="cruise-ribbon ribbon-\w+"><span>([^<]+)</span>', chunk),
            ports=ports))
    return out


def sheet_url(entry, page, cabin=""):
    path = f"/cruises/{entry['path']}" if entry.get("path") else "/cruises"
    q = [("max_cpd", fetch_cap(entry, cabin)), ("rt", 0), ("order", "lowest_cpd"), ("page", page)]
    if cabin:
        q.append(("min_class", cabin.strip().lower()))  # CruiseSheet "Minimum Cabin Class"
    return f"{SHEET}{path}?{urllib.parse.urlencode(q)}"


def sheet_deals(entry, cards, cfg):
    kws = [k.lower() for k in entry.get("keywords", [])]
    p = cfg["party"]
    deals = []
    for c in cards:
        cap = price_cap(entry, c.get("cabin"), cfg)
        if c["cpd"] > cap:
            continue
        if kws and not any(k in f"{c['title']} {' '.join(c['ports'])}".lower() for k in kws):
            continue
        est = round(c["price"] * (p["adults"] + p["children"] * p["child_price_factor"]))
        route = " > ".join(c["ports"][:1] + (["..."] if len(c["ports"]) > 2 else []) + c["ports"][-1:]) if c["ports"] else ""
        title = f"Cruise deal: {entry['name']} ${c['price']:,}pp, ${c['cpd']:.0f}/night"
        body = (f"{c['title']}\n{c['date']} | {c['line']} {c['ship']} | {c['cabin']} cabin\n"
                f"{route}\n${c['price']:,} per person = ${c['cpd']:.0f}/person/night (your limit ${cap})"
                + (f" | {c['ribbon']}" if c["ribbon"] else "") + "\n"
                f"{family_blurb(cfg, est)}\n{c['link']}")
        nights = _first(r"(\d+)\s*Night", c["title"], re.I)
        deals.append(dict(dest=entry["name"], cpd=c["cpd"], price=c["price"], link=c["link"],
                          title_text=title, body_text=body, key=f"{c['link']}@{c['price']}",
                          info=dict(dest=entry["name"], title=c["title"], date=c["date"], line=c["line"],
                                    ship=c["ship"], cabin=c["cabin"], ribbon=c["ribbon"], ports=c["ports"],
                                    nights=int(nights) if nights else None, price=c["price"], cpd=c["cpd"],
                                    est_total=est, max_cpd=cap, link=c["link"])))
    return deals


BACKOFF = 60  # seconds to wait after a 403/429 before the single retry


def sheet_blocked(html):
    """True for a bot-check page rather than a CruiseSheet listing (empty or not)."""
    low = (html or "").lower()
    return any(s in low for s in (
        "just a moment", "attention required", "cf-browser-verification",
        "/cdn-cgi/challenge", "enable javascript and cookies"))


def _download_sheet(entry, page, cabin):
    """Return page HTML, or None after a hard failure. One 403/429 retry with BACKOFF."""
    for attempt in (1, 2):
        try:
            return fetch(sheet_url(entry, page, cabin)).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt == 1:
                time.sleep(BACKOFF)  # rate limited: back off once, then retry
                continue
            print(f"WARN CruiseSheet {entry['name']} p{page}: {e}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"WARN CruiseSheet {entry['name']} p{page}: {e}", file=sys.stderr)
            return None
    return None


def fetch_sheet_pages(entry, pause=8, cabin=""):
    """Yield page HTML for one destination, cheapest per night first, stopping at the first page with no next link.

    When cabin is set, CruiseSheet is asked for that minimum class. A real listing with no
    cards is refetched without the class filter so a source that only publishes the cheapest
    cabin still yields deals. A block page is not refetched.
    """
    for page in range(1, entry.get("pages", 2) + 1):
        data = _download_sheet(entry, page, cabin)
        if data is None:
            return
        if cabin and not sheet_blocked(data) and not parse_cruisesheet(data):
            print(f"INFO CruiseSheet {entry['name']} p{page}: no {cabin} cards under the limit, "
                  f"using cheapest cabin", file=sys.stderr)
            time.sleep(pause)  # same gap as any other extra CruiseSheet request
            data = _download_sheet(entry, page, "")
            if data is None:
                return
        yield data
        if 'rel="next"' not in data:
            return
        time.sleep(pause)  # be polite to a small site


def fetch_sheet_all(cfg, pause=None):
    """One fetch per page URL: destinations sharing a path share pages (widest limit and depth wins), each is filtered later."""
    pause = cfg["search"].get("sheet_pause_seconds", 8) if pause is None else pause
    cabin = preferred_cabin(cfg)
    entries = cfg.get("cruisesheet", [])
    groups = {}
    for e in entries:
        g = groups.setdefault(e.get("path", ""), dict(name=e.get("path") or "all", path=e.get("path", ""), max_cpd=0, pages=0))
        # max_cpd on the shared request is already the widest cap, including preferred-cabin budgets.
        # Leave preferred_max_cpd off the group so sheet_url does not narrow that shared request.
        g["max_cpd"] = max(g["max_cpd"], fetch_cap(e, cabin))
        g["pages"] = max(g["pages"], e.get("pages", 2))
    cache = {}
    for path, g in groups.items():
        cache[path] = list(fetch_sheet_pages(g, pause, cabin=cabin))
        time.sleep(pause)
    return [(e, d) for e in entries for d in cache[e.get("path", "")]]


def write_site(path, infos, ok_dests, cfg, now=None):
    """Write every current deal for the web app. Destinations that failed to load this run keep their previous deals."""
    import json
    now = now or datetime.now(timezone.utc)
    kept = []
    try:
        kept = [d for d in json.loads(Path(path).read_text())["deals"] if d["dest"] not in ok_dests]
    except (OSError, ValueError, KeyError):
        pass
    seen, deals = set(), []
    for d in kept + infos:
        try:
            if datetime.strptime(d["date"], "%B %d, %Y").date() < now.date():
                continue  # already sailed
        except ValueError:
            pass
        if d["link"] + str(d["price"]) not in seen:
            seen.add(d["link"] + str(d["price"]))
            deals.append(d)
    deals.sort(key=lambda d: d["cpd"])
    p = cfg["party"]
    out = dict(updated=now.isoformat(timespec="seconds"), deals=deals,
               preferred_cabin=preferred_cabin(cfg),
               party=dict(adults=p["adults"], children=p["children"], cabins=p["cabins"],
                          child_price_factor=p["child_price_factor"]))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=1))


def run_sheets(cfg, db, pages=None, dry_run=False, send=None, stats=None, site_json=None):
    """pages: optional list of (entry, html) for offline tests; otherwise fetched live."""
    stats = stats if stats is not None else {}
    if pages is None:
        pages = fetch_sheet_all(cfg)
    found = {}
    for entry, data in pages:
        cards = parse_cruisesheet(data)
        stats["sheet_pages"] = stats.get("sheet_pages", 0) + 1
        stats["sheet_cards"] = stats.get("sheet_cards", 0) + len(cards)
        for d in sheet_deals(entry, cards, cfg):
            found.setdefault(d["key"], d)  # same sailing can appear under several destinations
    if site_json and pages:
        write_site(site_json, [d["info"] for d in found.values()], {e["name"] for e, _ in pages}, cfg)
    fresh = [d for d in found.values()
             if not db.execute("SELECT 1 FROM seen WHERE id=?", (d["key"],)).fetchone()]
    fresh.sort(key=lambda d: d["cpd"])
    stats["sheet_new"] = len(fresh)
    cap = cfg["search"].get("max_alerts_per_run", 8)
    per_dest = cfg["search"].get("max_alerts_per_destination", 2)
    chosen, count = [], {}
    for d in fresh:  # cheapest per night first, but spread across destinations
        if len(chosen) < cap and count.get(d["dest"], 0) < per_dest:
            chosen.append(d)
            count[d["dest"]] = count.get(d["dest"], 0) + 1
    sent = []
    for d in fresh:
        if d in chosen:
            if dry_run:
                print(d["title_text"], d["body_text"], sep="\n", end="\n\n")
                continue
            try:
                send(d, cfg)
            except Exception as e:  # leave unseen so the next run retries
                print(f"WARN notify failed: {e}", file=sys.stderr)
                continue
            print(f"ALERT {d['title_text']}")
            sent.append(d)
        elif dry_run:
            continue
        # not chosen: record silently so one run doesn't flood the phone
        db.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)",
                   (d["key"], d["title_text"], datetime.now(timezone.utc).isoformat()))
        db.commit()
    return sent


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cruise-watch"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def sources(cfg):
    for d in cfg["destinations"]:
        for q in d["queries"]:
            qs = urllib.parse.quote(f"{q} when:{cfg['search']['max_age_days']}d")
            yield f"Google News: {q}", f"https://news.google.com/rss/search?q={qs}&hl=en-US&gl=US&ceid=US:en"
    for f in cfg.get("extra_feeds", []):
        yield f["name"], f["url"]


def message(deal, cfg):
    if deal.get("body_text"):
        return deal["title_text"], deal["body_text"]
    nights = f"{deal['nights']} nights" if deal["nights"] else "nights unknown"
    pref = preferred_cabin(cfg)
    pref_line = (f"\nCabin preference: {pref} (headline fares are usually the cheapest cabin)."
                 if pref else "")
    body = (f"{deal['title']}\n{deal['dest']} | from ${deal['price']}/person | {nights}\n"
            f"{deal['verdict']}\n"
            f"{family_blurb(cfg, deal['est_total'])}{pref_line}\n"
            f"Source: {deal['source']}\n{deal['link']}")
    return f"Cruise deal: {deal['dest']} ~${deal['price']}pp", body


def notify(deal, cfg):
    title, body = message(deal, cfg)
    topic = os.environ.get("NTFY_TOPIC") or cfg["notify"]["ntfy_topic"]
    if topic and not topic.startswith("CHANGE-ME"):
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=body.encode(), method="POST",
            headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "high",
                     "Tags": "ship,moneybag", "Click": deal["link"]})
        urllib.request.urlopen(req, timeout=20).read()
    if os.environ.get("SMTP_HOST") and os.environ.get("EMAIL_TO"):
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, os.environ["SMTP_USER"], os.environ["EMAIL_TO"]
        msg.set_content(body)
        with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 465))) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)


def run(cfg, db_path, feeds=None, dry_run=False, send=notify, sheets=None, site_json=None):
    """feeds: optional list of (source, xml_bytes) for offline testing."""
    if sheets is None and feeds is not None:
        sheets = []  # offline test mode: don't touch the network
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, title TEXT, first_seen TEXT)")
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["search"]["max_age_days"])
    if feeds is None:
        feeds = []
        for name, url in sources(cfg):
            try:
                feeds.append((name, fetch(url)))
            except Exception as e:
                print(f"WARN {name}: {e}", file=sys.stderr)
    found = []
    stats = {}
    for name, data in feeds:
        try:
            items = parse_feed(data, name)
        except ET.ParseError as e:
            print(f"WARN {name}: bad feed ({e})", file=sys.stderr)
            continue
        stats["feeds"] = stats.get("feeds", 0) + 1
        for it in items:
            stats["scanned"] = stats.get("scanned", 0) + 1
            d = parse_date(it["date"])
            if d and d < cutoff:
                continue
            stats["recent"] = stats.get("recent", 0) + 1
            deal = evaluate(it, cfg, stats)
            if not deal:
                continue
            key = hashlib.sha1((it["link"] or it["title"]).encode()).hexdigest()
            if db.execute("SELECT 1 FROM seen WHERE id=?", (key,)).fetchone():
                continue
            found.append(deal)
            if dry_run:
                print(*message(deal, cfg), sep="\n", end="\n\n")
                continue
            try:
                send(deal, cfg)
            except Exception as e:  # leave unseen so the next run retries
                print(f"WARN notify failed: {e}", file=sys.stderr)
                continue
            db.execute("INSERT INTO seen VALUES (?,?,?)",
                       (key, it["title"], datetime.now(timezone.utc).isoformat()))
            db.commit()
    if sheets is not None or cfg.get("cruisesheet"):
        found += run_sheets(cfg, db, pages=sheets, dry_run=dry_run, send=send, stats=stats, site_json=site_json)
    db.close()
    print("funnel: " + ", ".join(f"{k}={stats.get(k, 0)}" for k in
          ("feeds", "scanned", "recent", "destination", "deal_words", "priced",
           "sheet_pages", "sheet_cards", "sheet_new")))
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=HERE / "config.toml")
    ap.add_argument("--db", default=HERE / "seen.sqlite")
    ap.add_argument("--dry-run", action="store_true", help="print deals, don't notify or record")
    ap.add_argument("--test-notify", action="store_true", help="send one sample notification")
    a = ap.parse_args()
    cfg = tomllib.loads(Path(a.config).read_text())
    if a.test_notify:
        notify(dict(dest="Test", price=499, nights=7, pp_night=71, verdict="test", est_total=2300,
                    title="Test alert from cruise-watch", source="local", link="https://example.com"), cfg)
        return
    found = run(cfg, a.db, dry_run=a.dry_run, site_json=None if a.dry_run else HERE / "site" / "deals.json")
    print(f"{len(found)} new deal(s)")


if __name__ == "__main__":
    main()
