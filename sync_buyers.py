#!/usr/bin/env python3
"""Cash-buyer Instant Form leads -> the investor Notion database.

    python sync_buyers.py --schema     # create any missing columns, then stop
    python sync_buyers.py --status     # in Meta vs synced vs pending
    python sync_buyers.py --dry-run    # show what would be written
    python sync_buyers.py --once       # one pass (what a scheduled task runs)

SEPARATE TOKEN, SEPARATE DATABASE
This does not touch the construction pipeline. `sync_leads.py` owns the Sentri
Lead Database under the "Sentri Construction" integration; this owns the
**Investor Lead Database** under "Julians Claude Code" in the Sentri Homes Space.
Different tokens, different databases, no shared state file.

Moved 2026-11-04 off the old "CC" database in Julian H's Space. All 70 rows were
copied by `migrate_investor_db.py`; the old database is intact but no longer
written to. Dedupe still keys on `Meta Lead ID`, which came across with the rows,
so nothing re-imports.

WHAT GETS WRITTEN
Every field the form collects gets its own column: name, address, city, email,
phone, rentals, whether they have flipped before, and how fast they can close.
Status is always "New" on insert - the other options are for a human working the
list. ARV Required, Info Required and Notes stay empty for manual use.

An earlier version wrote only name/city/email/phone and silently dropped the
qualifying answers, which made the form pointless. Do not reintroduce that.

DEDUPE
`Meta Lead ID` is a real column here, not a line hidden inside a text field like
the construction sync had to use - this token can create columns. Two layers, both
must say "new": the local state file (fast) and a Notion query on that column
(durable, survives losing the file).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "synced_buyers.json"
# Both forms are polled. Meta forms are immutable, so replacing the questions
# meant a new form - and the old one still holds real leads that must keep
# syncing. Newest first; order does not matter, dedupe is per lead id.
FORMS = [
    "1357988736506534",   # Sentri Macon Deal Buyer v2 - the only live form
]
# Trimmed to one 2026-11-04. The three retired forms (1080216748035838,
# 1737133661151467, 1061398799608011) are archived in Meta and carry no new
# leads; their historical leads are already in Notion and keyed by Meta Lead ID,
# so dropping them cannot lose anything. All four ads point at the form above -
# if an ad is ever pointed somewhere else, add that id here or its leads vanish
# silently.
NOTION_VERSION = "2025-09-03"
META_API = "https://graph.facebook.com/v23.0"

# Column -> Notion property type. Order matches the layout Julian asked for.
# Julian deleted City / ARV Required / Info Required / Notes in Notion. They are
# NOT listed here, so not even an explicit --schema run brings them back. The
# database schema is his; this file follows it, never the other way round.
SCHEMA = {
    "Investor": "title",
    "Address": "rich_text",
    "Email": "email",
    "Phone Number": "phone_number",
    "Rentals": "select",
    "Flipped Before": "select",
    "Close Speed": "select",
    "Status": "select",
    "Created": "date",
    "Meta Lead ID": "rich_text",       # dedupe key
}
# Notion rejects commas inside select option names outright ("Invalid select
# option, commas not allowed"), so the sheet's "No answer, texted and VM" becomes
# an en-dash version. Same meaning, and it is the only spelling Notion accepts.
STATUS_OPTIONS = ["New", "No answer - texted and VM", "Answered"]

# Pre-created so the columns render as proper pickers rather than filling up with
# whatever the first lead happened to answer.
SELECT_OPTIONS = {
    "Rentals": ["No", "Yes - 1 to 5", "Yes - 6 to 20", "Yes - 20+"],
    "Flipped Before": ["No - would be my first", "Yes - 1 or 2",
                       "Yes - 3 to 10", "Yes - 10+"],
    "Close Speed": ["Under 14 days", "14 - 30 days", "30+ days"],
}
# The retired form asked "Do you own rentals or flips now?" with different wording.
# Map it onto the new Rentals options so old and new leads read the same.
LEGACY_RENTALS = {"Not yet": "No", "1 - 5": "Yes - 1 to 5",
                  "6 - 20": "Yes - 6 to 20", "20+": "Yes - 20+"}
# The form's first flip option reads "No, this would be my first" - and Notion
# refuses commas in select option names. The form is immutable, so the answer is
# translated on the way in rather than fixed at source.
FLIPS_MAP = {"No, this would be my first": "No - would be my first"}


def env():
    """Real environment first, then a local .env.

    On GitHub Actions the values arrive as repository secrets and there is no
    .env file at all; on Julian's PC the .env is the source. Both work.
    """
    e = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("SENTRI_META_TOKEN", "SENTRI_PAGE_ID",
              "NOTION_INVESTOR2_TOKEN", "NOTION_INVESTOR2_DB"):
        if os.environ.get(k):
            e[k] = os.environ[k]
    for k in ("SENTRI_META_TOKEN", "SENTRI_PAGE_ID",
              "NOTION_INVESTOR2_TOKEN", "NOTION_INVESTOR2_DB"):
        if not e.get(k):
            sys.exit(f"{k} missing from .env")
    return e


ENV = env()


def notion(path, payload=None, method="GET"):
    url = f"https://api.notion.com/v1/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {ENV['NOTION_INVESTOR2_TOKEN']}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    })
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            err = json.loads(body)
            msg = f"{err.get('code')}: {err.get('message')}"
        except Exception:
            msg = body[:400]
        if e.code == 404:
            sys.exit(
                f"\nNotion says: {msg}\n\n"
                "The database is not shared with the 'CC' integration. There is no\n"
                "API for granting that - it has to be done in the Notion UI:\n"
                "  open the database as a full page -> ... (top right)\n"
                "  -> Connections -> Add connections -> CC\n")
        sys.exit(f"\nNotion API error ({e.code}) on {method} {path}:\n  {msg}\n")


def meta(path, params=None, token=None):
    p = dict(params or {})
    p["access_token"] = token or ENV["SENTRI_META_TOKEN"]
    url = f"{META_API}/{path.lstrip('/')}?{urllib.parse.urlencode(p)}"
    try:
        return json.load(urllib.request.urlopen(url))
    except urllib.error.HTTPError as e:
        sys.exit(f"\nMeta API error ({e.code}) on {path}:\n  {e.read().decode()[:400]}\n")


_PAGE_TOKEN = []
_OPTION_MAPS = {}


def page_token():
    """Form definitions are Page-owned; the system-user token cannot read them."""
    if not _PAGE_TOKEN:
        t = meta(ENV["SENTRI_PAGE_ID"], {"fields": "access_token"}).get("access_token")
        if not t:
            sys.exit("could not mint a Page token - is the Page assigned to the system user?")
        _PAGE_TOKEN.append(t)
    return _PAGE_TOKEN[0]


def option_map(form_id):
    """Meta returns the OPTION KEY for multiple-choice answers, not the label.

    A lead reads `rentals: opt1`, never `rentals: Yes - 1 to 5`. Writing that
    straight through puts literal "opt1" in Notion and quietly invents junk select
    options. Found by pushing a real test lead through the live form - it is not
    visible from the lead payload alone. So fetch each form's question definitions
    once and translate every answer back to its label.
    """
    if form_id not in _OPTION_MAPS:
        d = meta(form_id, {"fields": "questions"}, token=page_token())
        m = {}
        for q in d.get("questions", []):
            key = q.get("key")
            opts = {o.get("key"): o.get("value") for o in (q.get("options") or [])}
            if key and opts:
                m[key] = opts
        _OPTION_MAPS[form_id] = m
    return _OPTION_MAPS[form_id]


def data_source_id():
    """Columns live on the data source, not the database, since API 2025-09-03.

    databases.retrieve returns no `properties` at all on this version - reading or
    editing the schema means going through the data source, and new pages parent
    to a data_source_id rather than a database_id.
    """
    db = notion(f"databases/{ENV['NOTION_INVESTOR2_DB']}")
    sources = db.get("data_sources") or []
    if not sources:
        sys.exit("database has no data sources - is that id actually a database?")
    return sources[0]["id"]


# --- schema -------------------------------------------------------------------
def ensure_schema(ds_id, verbose=True):
    ds = notion(f"data_sources/{ds_id}")
    have = ds.get("properties", {})
    add = {}
    for name, kind in SCHEMA.items():
        if name in have:
            continue
        if kind == "select":
            opts = SELECT_OPTIONS.get(name, STATUS_OPTIONS)
            add[name] = {"select": {"options": [{"name": o} for o in opts]}}
        elif kind == "title":
            add[name] = {"title": {}}
        else:
            add[name] = {kind: {}}
    # A database always has exactly one title column and a second cannot be added,
    # so an existing title named something else (Notion's default is "Name") has to
    # be RENAMED rather than supplemented.
    title_name = next((n for n, p in have.items() if p.get("type") == "title"), None)
    if title_name and title_name != "Investor":
        add.pop("Investor", None)
        notion(f"data_sources/{ds_id}",
               {"properties": {title_name: {"name": "Investor"}}}, method="PATCH")
        if verbose:
            print(f"  renamed title column '{title_name}' -> 'Investor'")
        title_name = "Investor"
    if add:
        notion(f"data_sources/{ds_id}", {"properties": add}, method="PATCH")
        if verbose:
            for n in add:
                print(f"  added  {n}")
    elif verbose:
        print("  schema already complete")

    # Make sure Status carries all three options even if the column pre-existed.
    ds = notion(f"data_sources/{ds_id}")
    st = ds.get("properties", {}).get("Status", {})
    if st.get("type") == "select":
        existing = [o["name"] for o in st["select"].get("options", [])]
        missing = [o for o in STATUS_OPTIONS if o not in existing]
        if missing:
            notion(f"data_sources/{ds_id}", {"properties": {"Status": {"select": {
                "options": [{"name": o} for o in existing + missing]}}}}, method="PATCH")
            if verbose:
                print(f"  added  Status options: {', '.join(missing)}")
    return title_name or "Investor"


# --- leads --------------------------------------------------------------------
def fetch_leads():
    out = []
    for form_id in FORMS:
        out += _fetch_one(form_id)
    return out


def _fetch_one(form_id):
    out, after = [], None
    # remembered so flatten() can translate this form's option keys
    while True:
        params = {"fields": "id,created_time,field_data", "limit": 100}
        if after:
            params["after"] = after
        res = meta(f"{form_id}/leads", params)
        for lead in res.get("data", []):
            lead["_form_id"] = form_id
            out.append(lead)
        after = (res.get("paging", {}).get("cursors", {}) or {}).get("after")
        if not after or not res.get("data"):
            break
    return out


# Julian reads these timestamps, so they are pinned to HIS clock: MST, UTC-7.
#
# Hard-coded on purpose. The previous version called dt.astimezone() with no
# argument, which means "whatever machine is running this". That was fine while
# the sync ran on his PC and silently wrong the moment it moved to GitHub
# Actions, whose runners are UTC - every lead was landing in Notion stamped
# +00:00 and reading 7 hours into the future.
#
# A fixed offset rather than a DST-aware zone: his machine is on Yukon Standard
# Time, which is UTC-7 all year, so a fixed -7 tracks his own clock in every
# month. Swap to ZoneInfo("America/Denver") only if he moves somewhere that
# observes DST.
MST = timezone(timedelta(hours=-7))


def created_local(lead):
    """Meta returns created_time in UTC ("+0000"). Convert the same instant to
    MST so Notion shows Julian the wall-clock time the lead actually arrived."""
    raw = lead.get("created_time")
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return dt.astimezone(MST).isoformat(timespec="seconds")


def flatten(lead):
    opts = option_map(lead["_form_id"]) if lead.get("_form_id") else {}
    f = {}
    for item in lead.get("field_data", []):
        name = item["name"]
        val = (item.get("values") or [""])[0]
        # Translate opt0/opt1/... back to the label the person actually picked.
        f[name] = opts.get(name, {}).get(val, val)
    return f


def load_state():
    if STATE.exists():
        return set(json.loads(STATE.read_text(encoding="utf-8")))
    return set()


def save_state(ids):
    STATE.write_text(json.dumps(sorted(ids), indent=1), encoding="utf-8")


def existing_lead_ids(ds_id):
    """Every Meta Lead ID already in Notion, in one paginated sweep.

    The local state file is a convenience, not the source of truth - on GitHub
    Actions there is no disk between runs, so without this the sync would treat
    every lead as new and fall back to one Notion query per lead. Reading the
    column once is both stateless-safe and far cheaper.
    """
    out, cur = set(), None
    while True:
        q = {"page_size": 100}
        if cur:
            q["start_cursor"] = cur
        r = notion(f"data_sources/{ds_id}/query", q, method="POST")
        for page in r.get("results", []):
            v = page["properties"].get("Meta Lead ID", {})
            txt = "".join(s["plain_text"] for s in v.get("rich_text", []))
            if txt:
                out.add(txt)
        cur = r.get("next_cursor")
        if not cur:
            return out


def already_in_notion(ds_id, lead_id):
    res = notion(f"data_sources/{ds_id}/query", {
        "filter": {"property": "Meta Lead ID", "rich_text": {"equals": lead_id}},
        "page_size": 1,
    }, method="POST")
    return bool(res.get("results"))


def live_properties(ds_id):
    return notion(f"data_sources/{ds_id}").get("properties", {})


def write_lead(ds_id, title_prop, lead, fields, props=None):
    """Every answer the form collects lands in a column. Nothing is discarded.

    Handles both form schemas: the current `Sentri Deal Enquiry` and the retired
    `Sentri Cash Buyer List`, whose rentals question used different wording.
    """
    def text(v):
        return {"rich_text": [{"text": {"content": v}}]} if v else {"rich_text": []}

    street = fields.get("street_address", "")
    city = fields.get("city", "")
    state = fields.get("state", "")
    zipc = fields.get("zip_code", "") or fields.get("zip", "")
    full_addr = fields.get("address", "") or ", ".join(
        x for x in (street, city, f"{state} {zipc}".strip()) if x)

    # Retired form had no address at all - it asked which markets they buy in.
    # That is still worth keeping, so it goes to City when there is no real city.
    markets = fields.get("markets", "")

    rentals = fields.get("rentals") or LEGACY_RENTALS.get(fields.get("portfolio", ""), "")

    props_out = {
        title_prop: {"title": [{"text": {"content": fields.get("full_name") or "Unknown"}}]},
        "Address": text(full_addr),
        "City": text(city or markets),
        "Status": {"select": {"name": "New"}},
        "Meta Lead ID": text(lead["id"]),
    }
    if fields.get("email"):
        props_out["Email"] = {"email": fields["email"]}
    if fields.get("phone_number"):
        props_out["Phone Number"] = {"phone_number": fields["phone_number"]}
    if rentals:
        props_out["Rentals"] = {"select": {"name": rentals}}
    if fields.get("flips"):
        v = FLIPS_MAP.get(fields["flips"], fields["flips"])
        props_out["Flipped Before"] = {"select": {"name": v}}
    if fields.get("close_speed"):
        props_out["Close Speed"] = {"select": {"name": fields["close_speed"]}}
    when = created_local(lead)
    if when:
        props_out["Created"] = {"date": {"start": when}}

    # Drop anything whose column no longer exists - a deleted column stays
    # deleted, and Notion 400s on an unknown property name anyway.
    if props is None:
        props = live_properties(ds_id)
    payload = {k: v for k, v in props_out.items() if k in props}

    notion("pages", {"parent": {"type": "data_source_id", "data_source_id": ds_id},
                     "properties": payload}, method="POST")


# --- commands -----------------------------------------------------------------
EXTRA = ("rentals", "flips", "close_speed")


def run(dry=False, status_only=False):
    """NEVER creates columns. `ensure_schema` runs only under --schema.

    It used to run on every pass, which resurrected columns Julian had deleted in
    Notion - every five minutes, forever. The database schema is his to decide;
    this script fills whatever columns exist and ignores the rest.
    """
    ds_id = data_source_id()
    props = live_properties(ds_id)
    title_prop = next((n for n, v in props.items() if v.get("type") == "title"),
                      "Investor")
    leads = fetch_leads()
    # Notion is authoritative; the state file only saves a round trip.
    seen = load_state() | existing_lead_ids(ds_id)
    pending = [l for l in leads if l["id"] not in seen]

    print(f"\nin Meta {len(leads)}   already synced {len(seen)}   pending {len(pending)}")
    if status_only:
        return

    written = 0
    for lead in pending:
        f = flatten(lead)
        extra = "  ".join(f"{k}={f.get(k, '-')}" for k in EXTRA)
        # NEVER print names, emails or phones. This repo is PUBLIC and Actions
        # logs on a public repo are readable by any signed-in GitHub user. The
        # lead id is enough to find the person in Notion or Meta.
        if dry:
            print(f"  WOULD WRITE  lead {lead['id']} | {extra}")
            continue
        if already_in_notion(ds_id, lead["id"]):
            seen.add(lead["id"])
            continue
        write_lead(ds_id, title_prop, lead, f, props)
        seen.add(lead["id"])
        written += 1
        print(f"  wrote  lead {lead['id']}")

    if not dry:
        save_state(seen)
        print(f"\n{written} new lead(s) written.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schema", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if a.schema:
        ds = data_source_id()
        print(f"data source {ds}")
        ensure_schema(ds)
    elif a.status:
        run(status_only=True)
    elif a.dry_run:
        run(dry=True)
    elif a.once:
        run()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
