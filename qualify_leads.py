#!/usr/bin/env python3
"""Send the qualified verdict back to Meta so QUALITY_LEAD optimisation can learn.

    python qualify_leads.py --dry-run   # who qualifies, nothing sent
    python qualify_leads.py --once      # send any new qualified leads

WHY THIS EXISTS
The ad set optimises for `QUALITY_LEAD` (Conversion Leads). Meta cannot know which
form fills were any good unless the verdict is sent back, so without this the
setting does nothing - it just spends against a signal that never arrives.

QUALIFIED, per Julian 2026-09-09 - ALL FOUR must hold:
    Status         == Answered
    Rentals        != No            (owns at least one rental)
    Flipped Before != No ...first   (has done at least one flip)
    Close Speed    == Under 14 days

Events go to the pixel dataset keyed on `lead_id`, which is how Meta ties a
conversion back to the specific Instant Form submission and therefore to the ad
that produced it. `action_source` must be `system_generated` for lead events.

State lives in qualified_sent.json so a lead is never counted twice - a repeated
event would tell Meta the same person converted again.
"""
import argparse
import json
import time
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
    return (val(p, "Status") == "Answered"
            and val(p, "Rentals") not in NOT_RENTALS
            and val(p, "Flipped Before") not in NOT_FLIPPED
            and val(p, "Close Speed") == "Under 14 days")


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
    rows = sb.notion(f"data_sources/{ds}/query", {"page_size": 100},
                     method="POST").get("results", [])
    sent = set(json.loads(SENT.read_text(encoding="utf-8"))) if SENT.exists() else set()

    hits = []
    for r in rows:
        p = r["properties"]
        if not qualifies(p):
            continue
        lead_id = val(p, "Meta Lead ID")
        hits.append((val(p, "Investor"), lead_id))

    print(f"qualified in Notion: {len(hits)}   already sent: {len(sent)}")
    for name, lead_id in hits:
        if not lead_id:
            print(f"  {name}: no Meta Lead ID - skipped")
            continue
        if lead_id in sent:
            print(f"  {name}: already sent")
            continue
        if a.dry_run:
            print(f"  WOULD SEND  {name}  lead_id={lead_id}")
            continue
        res = send(lead_id)
        if res.get("error"):
            e = res["error"]
            print(f"  {name}: FAILED - {e.get('message')} | {e.get('error_user_msg','')}")
            continue
        sent.add(lead_id)
        print(f"  {name}: sent  (received={res.get('events_received')})")

    if not a.dry_run:
        SENT.write_text(json.dumps(sorted(sent), indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
