# Sentri investor lead sync

Moves Meta Instant Form leads into the **Investor Lead Database** in Notion, and
sends the qualified verdict back to Meta. Runs every 5 minutes on GitHub Actions
so it does not depend on Julian's PC being awake.

```
Meta Instant Form  --(poll)-->  sync_buyers.py    -->  Notion
Notion Status=Answered  -->     qualify_leads.py  -->  Meta (QualifiedLead event)
```

## Why polling and not a webhook

Meta only retries a lead webhook on a non-2xx. A handler that returns 200 and
then fails internally swallows the lead permanently — that already cost a real
lead on a sibling account. This re-reads every lead on the form every pass, so a
missed, late or failed run repairs itself on the next one.

A **Notion** webhook cannot replace the first leg either: Notion webhooks fire
*outbound* when something changes inside Notion. Nothing in Notion can reach into
Meta and pull a lead.

## This repo is public on purpose

Public repos get **unlimited** Actions minutes. A private repo on the free plan
gets 2000/month and is billed a **whole minute per run** however short it is - a
real run here is about 9 seconds. Every 5 minutes would be ~8600 min/month, over
4x that, and when the allowance runs out **runs stop silently**.

It is safe to be public: the only committed data is opaque Meta lead IDs - no
names, emails or phones - and the four secrets stay encrypted in GitHub Secrets
regardless of visibility. Verified across the full commit history before
flipping.

**If this is ever made private again, drop the cadence to `7,37 * * * *` in the
same commit** (~1440 min/month) or lead sync will die mid-month without warning.

## A new repo cold-starts its schedule

The first scheduled run on this repo did not fire until **~5 hours after it was
created**, while `workflow_dispatch` worked instantly throughout. Three leads sat
unsynced in the meantime. This is GitHub cold-starting a new schedule, not a
broken cron - don't go debugging it. If a lead is needed immediately:

```bash
gh workflow run sync.yml --repo julianhammon/sentri-lead-sync
```

## State files are load-bearing

`synced_buyers.json` and `qualified_sent.json` are committed back by the workflow
after every run. They are not a cache.

A Notion query is not a substitute, because **archived pages are not returned**.
Without the state files, any lead Julian deletes would look new on the next pass
and be re-imported — forever. Verified: deleting the state file resurrected an
archived test row within one run.

`qualified_sent.json` matters more: a repeat `QualifiedLead` event tells Meta the
same person converted twice and corrupts optimisation.

## What qualifies

Both must hold, per Julian 2026-11-05:

| Field | Must be |
|---|---|
| Rentals | anything except `No` (owns >=1 rental) |
| Flipped Before | anything except `No - would be my first` (>=1 flip) |

This replaced a four-condition rule that also required `Status == Answered` and
`Close Speed == Under 14 days`. **Dropping `Status` is the point.** Status is set
by a human after calling the lead, so the old rule gated the entire signal behind
manual follow-up - which is why only 2 events fired in two months. Rentals and
Flipped Before are answered on the form, so the verdict is known the moment a
lead lands and fires with nobody in the loop.

Measured over 81 Meta leads: old rule **3.7%**, this rule **24.7%**. Same funnel,
~7x the signal.

Events older than 7 days are recorded as handled and never sent - Meta will not
attribute them, and an unsendable event would otherwise be retried every 5
minutes forever.

## Optimising on it is BLOCKED (as of 2026-11-05)

No live ad set consumes these events yet. `optimization_goal: QUALITY_LEAD` is
refused at every turn, established by validate-only probes:

| Attempt | Error |
|---|---|
| existing campaign | `1885760` - campaign is locked to its first ad set's goal; needs a NEW campaign |
| new campaign, `page_id` only | `1815169` Invalid performance goal |
| new campaign, `pixel + page` | `2490291` **Not a Valid CRM Data Source** |

That last error is the real blocker: Conversion Leads needs the dataset
registered as a CRM data source for the Page, and there is **no Graph API path**
for it - every candidate endpoint returns `Unknown path components`. It is an
Events Manager UI setup.

Until that is done the live ad set stays on `LEAD_GENERATION` (any form fill) and
this script builds history rather than steering spend.

## Secrets

| Secret | What |
|---|---|
| `SENTRI_META_TOKEN` | Sentri system-user token (never expires) |
| `SENTRI_PAGE_ID` | Page `1070680486138551` — needed to mint a Page token, which is the only thing that can read a form's question definitions |
| `NOTION_INVESTOR2_TOKEN` | "Julians Claude Code" integration |
| `NOTION_INVESTOR2_DB` | Investor Lead Database |

## Gotchas already paid for

- Meta returns **option keys** (`opt1`), not labels, for multiple-choice answers.
  The form definition is fetched once per run and used to translate them.
- Reading a form's questions needs a **Page token**; the system-user token 400s.
- Notion **forbids commas** in select option names. The form's "No, this would be
  my first" is rewritten to "No - would be my first" on the way in.
- `sync_buyers.py --once` **never creates columns**. Schema is Julian's; the
  script fills whatever exists and ignores the rest.

## Running locally

Reads `.env` from the repo root if present, otherwise the environment. Same code
either way.

```bash
python sync_buyers.py --status     # in Meta vs synced vs pending
python sync_buyers.py --dry-run
python sync_buyers.py --once
python qualify_leads.py --dry-run
```
