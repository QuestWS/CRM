# AI CRM — calls, email, and remembering people

A CRM for a one-person business that records and transcribes your phone calls,
reads your inbox and sent mail, and keeps a running profile of every person you
deal with — what they need, and who they are.

Built around one idea: **a good transaction comes from knowing the issue, a good
customer comes from getting to know the person.** Those are two different things,
so the system tracks them separately. "Needs" are the job to be done. "Facts" are
what someone told you about their life. You get both on one page before you call
them back.

## What it does

- **Records every call** via your Sangoma PBX, with a "this call is being
  recorded" announcement played to the caller.
- **Transcribes locally** by default — audio never leaves your machine.
- **Extracts** the caller's name, number, company, what they need, what was
  promised, and any personal details they mentioned.
- **Builds a profile** of each person, rewritten after every conversation.
- **Reads your email over IMAP** — inbox *and* sent, so it sees both halves of
  every conversation.
- **Creates tasks** from actual commitments, and **appointments** from times you
  agreed on, pushed to Google Calendar.
- **Writes you a brief each morning**: what to do today, what's waiting on
  others, who's going cold.
- **Routes every conversation** — service, sales, boat club, rental, parts,
  storage, billing, vendor, internal, spam — across your product lines, so a
  kayak enquiry is never mistaken for a service call.
- **Matches conversations to open work** in both
  [`servicetracker`](https://github.com/QuestWS/servicetracker) (repair jobs)
  and [`winter-quotes`](https://github.com/QuestWS/winter-quotes_26-27)
  (winter services), and stages a note you send with one click.
- **Records walk-in drop-offs** at the counter and turns them into a draft work
  order, with a list of what you still need to ask.

## Before you start: Illinois is an all-party consent state

Recording a private conversation in Illinois requires **everyone's** consent,
and a violation is a felony. The recording announcement is not a courtesy here —
it is what makes the system lawful. The same applies to the counter recorder:
you have to ask before you press it.

Read **[docs/privacy-and-consent.md](docs/privacy-and-consent.md)** before you
record anything. It also covers BIPA, which treats voiceprints as biometric
data and is a live litigation risk in Illinois.

## Getting recordings in

The Sangoma mobile app records SIP calls itself and can POST each recording
straight here — that is the simplest path, and it is what
**[docs/sangoma-setup.md](docs/sangoma-setup.md)** walks through, along with
PBX-side recording for desk phones and the announcement that plays to callers.

## Install

```bash
git clone <this repo> && cd CRM
make install
cp .env.example .env
$EDITOR .env          # see the annotated settings below
make init
make doctor           # tells you what's still missing
```

Requires Python 3.11+ and `ffmpeg` (for splitting stereo call recordings —
`apt install ffmpeg` or `brew install ffmpeg`).

### The settings that matter most

| Setting | Why it matters |
|---|---|
| `OPERATOR_NUMBERS` | Every number that is you. Without these the CRM can't tell inbound calls from outbound. |
| `BUSINESS_LINES` | What you sell and service. Drives how calls get categorised. |
| `OPERATOR_EMAIL` / `OPERATOR_ALIASES` | Same job for email — this is how sent mail is recognised as yours. |
| `ANTHROPIC_API_KEY` | Without it you still get recordings and transcripts, but no summaries, profiles or extracted tasks. |
| `IMAP_*` | On Gmail use an **App Password**, not your account password. |
| `SANGOMA_WATCH_DIR` | Where the PBX's recordings land, if you're mounting or syncing them. |

## Run it

```bash
make serve     # web UI + background jobs at http://127.0.0.1:8000
```

Or split them:

```bash
make worker                          # background jobs only
.venv/bin/python -m crm.cli serve    # web only
```

## Command line

```bash
python -m crm.cli doctor              # check every integration
python -m crm.cli check-mail          # test IMAP, list folders, confirm Sent is found
python -m crm.cli sync-mail           # pull and analyse new mail now
python -m crm.cli import-calls        # scan the watch folder and process
python -m crm.cli add-call rec.wav    # process one recording
python -m crm.cli brief               # today's brief in the terminal
python -m crm.cli brief --raw         # what the AI was given, for debugging
python -m crm.cli refresh-profile 12  # rewrite one contact's profile
python -m crm.cli sync-work           # pull open jobs and winter quotes
python -m crm.cli intake counter.wav  # a drop-off recording -> draft work order
```

## The web UI

| Page | What's there |
|---|---|
| **Today** | Appointments, open tasks, recent conversations, who's going cold |
| **People** | Search across names, numbers, emails and profile text |
| **Person** | Profile, needs, facts by category, tasks, full history with transcripts |
| **Calls** | Every recording and its processing status; upload files here |
| **Tasks** | Everything outstanding, overdue first |
| **Calendar** | Appointments, and one click to push them to Google |
| **Conversations** | Everything that came in, filtered by what it was about |
| **Drop-off** | The counter record button, and recent intakes |
| **Open work** | Repair jobs and winter quotes, and notes waiting to send |
| **Brief** | The morning brief |

## Email over IMAP

Both folders are synced: your **inbox** and your **sent** folder. Sent mail is
found through the IMAP `\Sent` special-use flag, so it works across providers
without hardcoding folder names; `IMAP_FOLDERS` overrides it if detection guesses
wrong.

Sent mail is not optional in practice. Without your replies, the CRM sees a
customer's question with no answer and keeps generating "you owe them a reply"
tasks for things you already handled.

Everything is fetched with `BODY.PEEK`, so **nothing is ever marked as read**,
moved, flagged or deleted. Newsletters, no-reply addresses and auto-replies are
filtered out. Quoted history and signature blocks are stripped so the model reads
the new message rather than the whole thread again.

```bash
python -m crm.cli check-mail
```

lists every folder on your server, marks which one was detected as Sent, and
shows which addresses are being treated as you.

## Privacy

Recording customer calls is legal in Canada with one-party consent, but PIPEDA
requires you to tell people and offer an alternative — which is what the
announcement is for. Details, plus what this system stores and what to do about
retention: **[docs/privacy-and-consent.md](docs/privacy-and-consent.md)**.

Data flow in short: **audio stays local** (with the default local Whisper
engine); **transcripts and email text go to the Anthropic API** for
summarisation; **everything else stays in your SQLite file**. Encrypt the disk —
it's the highest-value ten minutes of security work here — and back up
`data/crm.db` and `media/`.

## Not every call is a service call

The shop sells boats, kayaks, sailboats and e-bikes, runs a club, stores and
winterizes, and takes as many vendor and staff calls as customer ones. Every
conversation is placed on two axes:

- **Category** — what they wanted: `service`, `sales`, `boat_club`, `rental`,
  `parts`, `storage`, `billing`, `vendor`, `internal`, `personal`, `spam`,
  `other`.
- **Product line** — what it was about: powerboat, sailboat, kayak,
  paddleboard, e-bike, trailer, engine, apparel.

Only `service`, `parts`, `storage` and `billing` can concern a work order, and
that is enforced in code rather than left to the prompt: a customer with a boat
in the shop who calls about a kayak gets a `sales` conversation and no note on
their open job.

`spam` and `internal` conversations are recorded but build no profile, no needs
and no tasks — robocalls don't get customer profiles. Vendors and staff are
marked as such and drop out of the follow-up lists.

Set `BUSINESS_LINES` in `.env` to your actual lines; the router uses it.

## The shop apps

Conversations get matched to open work in **both** shop systems — repair jobs
in the service tracker, winter services quotes in the winter system — and a
note is staged for you to send.

Nothing is created in BiT, no quote is invented, and nothing customer-facing is
ever sent. The service tracker gets a `writer_note`, which is shop-only by
construction over there. The winter system gets a staff note, appended beneath
whatever a staff member already wrote — that note field *replaces*, so
appending is what stops a phone call wiping out somebody's handwriting. The
winter client wraps no send function at all, because that repo forbids emailing
customers.

The counter record button produces a draft work order you key into BiT, plus a
timeline entry and profile facts for the customer.

Setup and the rules this respects: **[docs/servicetracker-integration.md](docs/servicetracker-integration.md)**.

## How it works

[docs/architecture.md](docs/architecture.md) covers the data model, the
failure-tolerant pipeline, identity resolution, speaker attribution, and how to
swap out the database, transcription engine or calendar.

## Tests

```bash
make dev && make test
```

Covers phone and email normalisation, contact resolution and merging, PBX
filename parsing, webhook field mapping, email body extraction and quote
stripping, the deduplication logic that decides what actually lands in the
database, and that every page renders.

The AI is stubbed throughout — the tests exercise the code around it, which is
where the bugs live.
