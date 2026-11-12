#!/usr/bin/env python3
"""Send the qualified verdict back to Meta so it can optimise on lead QUALITY.

    python qualify_leads.py --dry-run   # who qualifies, nothing sent
    python qualify_leads.py --once      # send any new qualified leads

QUALIFIED, per Julian 2026-11-05 - BOTH must hold:
    Rentals        != No            (owns at least one rental)
    Flipped Before != No ...first   (has done at least one flip)

This replaced a four-condition rule that also required `Status == Answered` and
`Close Speed == Under 14 days`. Dropping `Status` is the important part: Status
is set by a HUMAN after calling the lead, so the old rule gated the whole signal
behind manual follow-up. That is why only 2 events ever fired in two months.
Rentals and Flipped Before are answered on the form itself, so the verdict is
known the instant a lead arrives and fires with no human in the loop.

Measured on 81 Meta leads: the old rule qualified 3.7%. This one qualifies
24.7%. Same funnel, ~7x the signal, because nothing waits on a phone call.

WHERE THIS DOES AND DOES NOT WORK YET
Events land in the pixel dataset keyed on `lead_id`, which is how Meta ties a
conversion back to a specific Instant Form submission and so to the ad that
produced it. `action_source` must be `system_generated` for lead events.

But NO live ad set consumes them yet. Setting an ad set to
`optimization_goal: QUALITY_LEAD` is refused:
  - on an existing campaign: error 1885760, the campaign is locked to the goal
    its first ad set used, so it needs a NEW campaign;
  - on a new campaign with page_id alone: error 1815169 "Invalid performance
    goal";
  - on a new campaign with pixel + page: error 2490291 "Not a Valid CRM Data
    Source".
That last one is the real blocker. Conversion Leads needs the dataset registered
as a CRM data source for the Page, and there is NO Graph API path for it - every
candidate endpoint returns "Unknown path components". It is an Events Manager UI
setup. Until that is done this script is building history, not steering spend.

State lives in qualified_sent.json so a lead is never counted twice - a repeated
event would tell Meta the same person converted again and corrupt optimisation.
"""
import argparse
import json
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import sync_buyers as sb  # shared env + notion client

ROOT = Path(__file__).resolve().parent
SENT = ROOT / "qualified_sent.json"
PIXEL = "1882336675806125"          # Sentrihomes.com pixel
EVENT_NAME = "QualifiedLead"

NOT_RENTALS = {"No", None, ""}
NOT_FLIPPED = {"No - would be my first", None, ""}


def val(props, key):
    v = props.get(key, {})
    t = v.get("type")
    if t == "title":
        return "".join(s["plain_text"] for s in v["title"])
    if t == "rich_text":
        return "".join(s["plain_text"] for s in v["rich_text"])
    if t == "select":
        return (v["select"] or {}).get("name")
    return v.get(t)


def qualifies(p):
    """At least one rental AND at least one flip. Both come straight off the
    form, so this needs no human step - see the module docstring."""
    return (val(p, "Rentals") not in NOT_RENTALS
            and val(p, "Flipped Before") not in NOT_FLIPPED)


# Meta rejects conversion events older than 7 days, and an unattributable event
# is worse than none: it cannot help optimisation but it can still be retried
# forever. Anything older is recorded as handled and never sent.
STALE_AFTER_DAYS = 7


def created_of(props):
    v = props.get("Created", {})
    d = v.get("date") or {}
    return d.get("start")


def is_stale(created):
    if not created:
        return False
    try:
        dt = datetime.fromisoformat(created)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days > STALE_AFTER_DAYS


def send(lead_id):
    payload = {"data": [{
        "event_name": EVENT_NAME,
        "event_time": int(time.time()),
        "action_source": "system_generated",
        "user_data": {"lead_id": int(lead_id)},
    }]}
    url = f"https://graph.facebook.com/v23.0/{PIXEL}/events"
    body = urllib.parse.urlencode({
        "data": json.dumps(payload["data"]),
        "access_token": sb.ENV["SENTRI_META_TOKEN"],
    }).encode()
    try:
        return json.load(urllib.request.urlopen(
            urllib.request.Request(url, data=body, method="POST")))
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read().decode()).get("error", {})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    ds = sb.data_source_id()
    # Paginated. The previous version asked for one page of 100 and used it as
    # if it were the whole table, so the moment the DB passed 100 rows it would
    # have silently stopped qualifying anyone past that point.
    rows, cursor = [], None
    while True:
        q = {"page_size": 100}
        if cursor:
            q["start_cursor"] = cursor
        page = sb.notion(f"data_sources/{ds}/query", q, method="POST")
        rows += page.get("results", [])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    sent = set(json.loads(SENT.read_text(encoding="utf-8"))) if SENT.exists() else set()

    hits = []
    for r in rows:
        p = r["properties"]
        if not qualifies(p):
            continue
        lead_id = val(p, "Meta Lead ID")
        hits.append((lead_id, created_of(p)))

    print(f"qualified in Notion: {len(hits)}   already sent: {len(sent)}")
    # NEVER print investor names here. This repo is PUBLIC and Actions logs on a
    # public repo are readable by any signed-in GitHub user. Lead ids are enough.
    for lead_id, created in hits:
        if not lead_id:
            print("  (row with no Meta Lead ID) - skipped")
            continue
        if lead_id in sent:
            continue                      # silent: this is the common case
        if is_stale(created):
            # Record as handled so it stops being retried on every pass.
            print(f"  lead {lead_id}: older than {STALE_AFTER_DAYS}d "
                  f"({created[:10]}) - too late to attribute, marking handled")
            if not a.dry_run:
                sent.add(lead_id)
            continue
        if a.dry_run:
            print(f"  WOULD SEND  lead {lead_id}")
            continue
        res = send(lead_id)
        if res.get("error"):
            e = res["error"]
            print(f"  lead {lead_id}: FAILED - {e.get('message')}")
            continue
        sent.add(lead_id)
        print(f"  lead {lead_id}: sent  (received={res.get('events_received')})")

    if not a.dry_run:
        SENT.write_text(json.dumps(sorted(sent), indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
