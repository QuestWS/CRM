# Talking to the shop apps

The CRM knows what customers say. Two other systems know what is open on them:

* **`QuestWS/servicetracker`** — repair jobs on the floor.
* **`QuestWS/winter-quotes_26-27`** — winter services quotes, storage and
  season close-out.

This connects both without either one having to change how it works.

## What crosses the wire

```
   servicetracker (Apps Script /exec)
        │  listJobs           ────▶  WorkItem mirror, linked to contacts
        ◀── addWriterNote     ────   a staged note, after somebody clicks send

   winter-quotes (Apps Script /exec)
        │  storageView + lookup ──▶  WorkItem mirror, linked to contacts
        ◀── staffNote         ────   a staged note, after somebody clicks send
```

Four calls in, two out. Both mirrors land in the same `WorkItem` table with a
`system` discriminator, so matching, staging, approval and pushing are one
mechanism rather than two near-copies.

Ids are namespaced — `servicetracker:WO-10432`, `winter:QW-26-1255` — because
the two apps number independently and "they probably won't collide" is not a
guarantee worth building on.

## Rules carried over from those repos

These are not optional — they are the reason this integration is safe to add.

### From the service tracker

**BiT is never integrated with.** A job id *is* a BiT invoice number, so the CRM
cannot create a job. Walk-in intake produces a draft that a person keys into
BiT; nothing here invents a ticket.

**Nothing customer-facing is ever sent.** The only write is `addWriterNote`,
which creates a `writer_note` — "From the office". Over there, `customerView_`
returns `customer_note` and nothing else, so a `writer_note` cannot surface to a
customer even if the tracking page is switched back on. The CRM's write path is
inside that boundary by construction, not by remembering to stay there.

### From the winter system

**Never send email to a customer.** That repo's §4b is explicit: every email is
a human-clicked action by Quest staff. So the winter client wraps *no* send
function at all — not `sendEmail`, not `bulkSend`, not even the preview. A
capability that is not there cannot be called by mistake, and there is a test
asserting the client's public surface contains nothing matching "send" or
"email".

**Treat the sheet as read-only** unless the task is to change a named quote.
The only write is `staffNote`, against a quote number a person approved.

**The staff note replaces, it does not append.** So a push reads the existing
note first and adds underneath it. Losing what a staff member typed by hand
because a call came in afterwards would be far worse than a note that runs long
— and a note already present is detected and skipped rather than duplicated.

**Never put customer PII in the repo.** Nothing here logs a name, phone or
email; quotes are identified by number in every log line, and the tests use
invented people.

### Both

**Nothing leaves on a timer.** Staged notes wait for a click, matching both
shops' rule that customer-facing mail only goes when a person presses send.
`SERVICETRACKER_AUTOPUSH=true` covers both systems if you decide otherwise; it
is off.

## Setup

```bash
# .env
SERVICETRACKER_EXEC_URL=https://script.google.com/macros/s/AKfy…/exec
SERVICETRACKER_PASSWORD=<the ADMIN_PASSWORD script property over there>
SERVICETRACKER_SYNC_MINUTES=10
SERVICETRACKER_AUTOPUSH=false

WINTER_EXEC_URL=https://script.google.com/macros/s/AKfy…/exec
WINTER_PIN=<a staff console PIN with the "keys" permission>
```

Both `/exec` URLs are the ones those apps already use — the service tracker's
is in `assets/lib/config.js`, the winter system's is `API_URL` in
`admin/index.html`. Do not mint a new deployment to get either: the service
tracker's orphans every QR code already printed on paper, and the winter one is
shared by three front ends.

The winter PIN needs the **`keys`** permission, which is what
`adminSetStaffNote` requires over there. A PIN with only `view` will sync fine
and fail on push with a clear message.

```bash
python -m crm.cli sync-work   # pull open jobs and quotes, link them to contacts
```

Then open `/tickets`. Either system being unconfigured or down is survivable —
`sync_all` reports per-system and the other still runs.

## How a call finds its work order

0. **Routing** happens first. Only `service`, `parts`, `storage` and `billing`
   conversations are eligible at all — a sales enquiry, a club booking or a rep
   chasing an order never reaches step 4, even from a customer with an open job.
1. **Sync** mirrors every job into `ServiceTicket`, normalising the phone and
   email the shop holds so they match the CRM's format.
2. **Linking** attaches each ticket to a contact — by an existing link first,
   then by the phone or email on the job. A ticket created before the contact
   existed still finds them.
3. **Analysis** is given the person's open tickets as context. The model is told
   to copy a work order number from that list and never to invent one.
4. **Staging** accepts a returned ticket id only if it exists *and* is still
   open. Anything else is logged and dropped — a hallucinated number cannot
   reach the shop's job log.
5. **Review** at `/tickets`, then send.

A call from someone with no open ticket produces no notes. So does a call in
any category that has nothing to do with a work order. Most calls to this shop
are like that — the prompt says so, and `stage_notes` enforces it.

## What a pushed note looks like

```
From a phone call on 03 Sep, 14:30:

Dave wants the cover replaced while the boat is in — says the
existing one tore over the winter. He is fine with the cost.

This changes the work — needs re-keying into BiT.
```

The provenance line is there because a mechanic reading it needs to know this is
the customer's word relayed, not something someone saw on the boat.

## The winter system's shape, and what it costs

`storageView` is one call for the whole season, but it carries no phone or
email — so contact details need a per-quote `lookup`. A first sync therefore
backfills a bounded number per run (25) and links progressively over a few
passes, rather than firing hundreds of Apps Script calls at once. Quotes
already carrying contact details are never looked up again.

A quote is **open until the season is closed out on it**, not until it is paid.
A boat that is paid for and still sitting in the yard is very much open, and
`seasonDone` is the field that says otherwise.

Names come back as `"Last, First"` from `storageView` and are turned round on
the way in, so they match how a contact is stored.

## Walk-in intake

`/intake` is a record button for a customer standing at the counter. Press it,
let them describe the boat and the work, press stop.

What comes back is a draft work order — customer, boat, one line per requested
job with the customer's own words kept where they are more useful than a
paraphrase — plus a list of **what you still need to ask** before it can be
written up. An empty open-questions list is the model claiming the intake is
complete.

It deliberately stops at a draft. Copy it into BiT, create the job in the
service tracker as normal, then put the work order number into the intake page
so the conversation and the job are joined up.

Two things happen automatically alongside the draft: the conversation lands on
the customer's CRM timeline, and anything personal they mentioned becomes a
fact on their profile. The counter is where most of that gets said and lost.

## Transcription

The shop already transcribes mechanic voice notes with AssemblyAI. Set
`TRANSCRIPTION_ENGINE=assemblyai` and reuse the same key to keep one vendor
across both apps:

```bash
TRANSCRIPTION_ENGINE=assemblyai
ASSEMBLYAI_API_KEY=<the same ASSEMBLYAI_API_KEY script property>
```

Differences from the service tracker's implementation, both deliberate:

- **Polling, not a webhook.** Apps Script cannot block, so it registers a
  webhook and waits. The CRM has a worker thread, so it polls — no public URL
  to expose.
- **Speaker labels are on.** A mechanic's voice note has one speaker; a phone
  call and a counter conversation have two or more.
- **A word boost list** for the vocabulary that costs the most when it is wrong:
  engine makes, hull models, and the shop's own terms — impeller, lower unit,
  outdrive, stringer, bellows, skeg, shrink wrap, winterize.

`faster_whisper` remains the default and keeps audio on your machine. Which
matters in Illinois — see [privacy-and-consent.md](privacy-and-consent.md).

## When the service tracker is unreachable

Everything degrades to still-useful:

- The ticket mirror is local, so matching keeps working against the last sync.
- Note staging is local, so notes queue rather than disappear.
- A failed push marks the note `failed` with the reason and leaves it in the
  queue to retry.
- Nothing in the call or email pipeline fails because the shop app is down —
  staging is wrapped so a service-tracker problem can never cost you a
  completed analysis.
