# Talking to the service tracker

The CRM knows what customers say. `QuestWS/servicetracker` knows what work is
open on the floor. This connects them without either one having to change how
it works.

## What crosses the wire

```
   servicetracker (Apps Script /exec)
        │  listJobs          ─────▶  ServiceTicket mirror, linked to contacts
        │
        ◀── addWriterNote    ─────   a staged note, after somebody clicks send
```

That is the whole surface. Two calls, one each way.

## Rules carried over from that repo

These are not optional — they are the reason this integration is safe to add.

**BiT is never integrated with.** A job id *is* a BiT invoice number, so the CRM
cannot create a job. Walk-in intake produces a draft that a person keys into
BiT; nothing here invents a ticket.

**Nothing customer-facing is ever sent.** The only write is `addWriterNote`,
which creates a `writer_note` — "From the office". Over there, `customerView_`
returns `customer_note` and nothing else, so a `writer_note` cannot surface to a
customer even if the tracking page is switched back on. The CRM's write path is
inside that boundary by construction, not by remembering to stay there.

**Nothing leaves on a timer.** Staged notes wait for a click, matching the
shop's rule that the invoice email only goes when a writer presses send.
`SERVICETRACKER_AUTOPUSH=true` exists if you decide otherwise; it is off.

## Setup

```bash
# .env
SERVICETRACKER_EXEC_URL=https://script.google.com/macros/s/AKfy…/exec
SERVICETRACKER_PASSWORD=<the ADMIN_PASSWORD script property over there>
SERVICETRACKER_SYNC_MINUTES=10
SERVICETRACKER_AUTOPUSH=false
```

The `/exec` URL is the one in the service tracker's `assets/lib/config.js` —
the same one the four pages use. Do not mint a new deployment to get one; that
orphans every QR code already printed on paper.

```bash
python -m crm.cli sync-tickets   # pull open jobs, link them to contacts
```

Then open `/tickets`.

## How a call finds its work order

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

A call from someone with no open ticket produces no notes. Most calls are like
that, and the prompt says so.

## What a pushed note looks like

```
From a phone call on 03 Sep, 14:30:

Dave wants the cover replaced while the boat is in — says the
existing one tore over the winter. He is fine with the cost.

This changes the work — needs re-keying into BiT.
```

The provenance line is there because a mechanic reading it needs to know this is
the customer's word relayed, not something someone saw on the boat.

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
