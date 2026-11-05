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

All four must hold, per Julian:

| Field | Must be |
|---|---|
| Status | `Answered` |
| Rentals | anything except `No` |
| Flipped Before | anything except `No - would be my first` |
| Close Speed | `Under 14 days` |

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
