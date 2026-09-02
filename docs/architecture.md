# How it fits together

```
  Sangoma app / PBX      IMAP mailbox    counter mic     Google Calendar
  (records+announces)    (inbox+sent)    (/intake)              ^
        |                     |               |                 |
        | upload/webhook      | every 10 min  | record button   | appointments
        v                     v               v                 |
  +-----------------------------------------------------------------------+
  |  ingest/ sangoma.py  imap_mail.py   services/intake.py    gcal.py     |
  +-----------------------------------------------------------------------+
        |                              |
        v                              v
  +-------------------+          +------------------+
  | transcription/    |          |  mailparse.py    |
  | faster-whisper    |          |  quote stripping |
  | (local, private)  |          |  bulk filtering  |
  +-------------------+          +------------------+
        |                              |
        +---------------+--------------+
                        v
              +---------------------+
              |  ai/enrich.py       |   Claude, structured output
              |  ConversationAnalysis|
              +---------------------+
                        v
              +---------------------+
              | services/profile.py |   dedupe, merge, apply
              +---------------------+
                        v
     Contact - Fact - Need - Task - Appointment - Interaction
                        |
        +---------------+----------------+
        v                                v
  web/ dashboard, contact           services/tickets.py
  pages, daily brief                      |
                                          |  listJobs / addWriterNote
                                          v
                          servicetracker + winter-quotes
                            (two Apps Script /exec endpoints)
                                          |
                                    a person keys it in
```

## The data model, and why it is shaped this way

You said it yourself: *a good transaction comes from knowing the issue, a good
customer comes from getting to know the person.* Those are two different things,
so they are two different tables.

- **`Need`** is the issue. What job does this person want done, how urgent, what
  is blocking it, is it still open. Needs get merged across conversations — a
  second call about the same furnace updates the existing need rather than
  creating a new one.
- **`Fact`** is the person. One durable thing they told you, with the quote they
  said it in and a confidence score. Categorised as personal / business /
  preference / logistics. Facts are deduplicated across calls and get a
  `last_confirmed_at` bump when they come up again.
- **`Interaction`** is the raw event — a call with its transcript, an email, a
  note. Everything else points back to one.
- **`Task`** is a commitment. The extraction prompt is strict about this: only
  things someone actually committed to. "We should look at that sometime" is not
  a task, it goes in the need description.
- **`Contact.ai_profile`** is the rolling narrative, rewritten from scratch after
  each conversation from all the facts, needs and history. It is what you read
  before you call someone back.

## Why the pipeline is built to survive failure

Each stage saves before the next one starts, so a failure never costs you the
work already done:

```
pending -> transcribing -> transcribed -> analyzing -> done
                                 ^
                          if the AI is down or the key is missing,
                          it stops here. The transcript is saved.
                          A later run picks it up from transcribed.
```

The valuable, expensive, unrepeatable artefact is the transcript — the audio may
be deleted, the call can't be re-held. The analysis is cheap to redo. So the
transcript is committed the moment it exists, and analysis failures never roll it
back.

## Routing, and why it is a code gate

Every conversation gets a `category` (what they wanted) and a `product_line`
(what it was about). Most calls to this shop are not service calls, so the
first thing the analyst does is place the conversation.

`WORK_ORDER_CATEGORIES` in `crm/models.py` is the whole rule: only `service`,
`parts`, `storage` and `billing` may produce a ticket note. `stage_notes`
checks it and drops anything else, logging what it dropped.

That gate is deliberately in code rather than only in the prompt. The prompt
tells the model not to stretch a category to reach a work order; the gate is
what makes it true when the model does it anyway. The same pattern as the
ticket-id check next to it: the model is asked to copy an id from a list, and
staging then verifies the ticket exists and is open.

`NO_PROFILE_CATEGORIES` does the corresponding thing for noise: a `spam` or
`internal` conversation is recorded, and nothing is learned from it.

## Identity resolution

Every path that creates a record goes through `resolve_contact()`. Rules:

1. **Phone beats email.** The number on an inbound call is hard evidence; a name
   heard through speech recognition is not.
2. **A real name is never overwritten by a lower-confidence one.** ASR mangles
   proper nouns constantly — once you have "Dave Mercer", the CRM will not let
   "Dev Mercy" replace it. Placeholders like `Unknown (613) 555-1234` *are*
   upgraded as soon as a real name appears.
3. **Names extracted with confidence below 0.5 don't rename anybody.**
4. Duplicates that slip through can be merged in the UI; the merge moves every
   interaction, fact, need, task and appointment, and invalidates the surviving
   profile so it gets rewritten from the combined history.

## Speaker attribution

If your PBX records with the `b` MixMonitor flag, each leg of the call lands on
its own stereo channel. The CRM detects that, splits the file with ffmpeg,
transcribes each leg separately, and interleaves the results by timestamp. That
gives reliable "who said what" without a diarisation model.

Without stereo, you get a single mixed transcript, and the analysis prompt is
told that speaker labels may be wrong and to use content rather than labels to
work out who is who.

## Scheduling

One in-process APScheduler, no broker, no Redis. This is one person's CRM; a
message queue would cost more to run than it saves.

| Job | Interval | What it does |
|---|---|---|
| `process_calls` | 30s | Scan the watch folder, drain the recording queue |
| `sync_mail` | 10 min | IMAP fetch, then analyse new messages |
| `sync_calendar` | 15 min | Push new appointments to Google Calendar |
| `walk_in_intakes` | 30s | Finish counter recordings orphaned by a restart |
| `sync_work` | 10 min | Mirror both shop apps; push notes if autopush is on |
| `daily_brief` | 07:00 | Generate the morning brief |

Run it inside the web server (`crm.cli serve`) or on its own (`crm.cli worker`).

## Where the AI is used, and where it isn't

Three calls to Claude, all with structured output validated against a Pydantic
schema:

1. **`analyze_conversation`** — one call or email in, a `ConversationAnalysis`
   out. The prompt is explicit that ASR mangles names and numbers, that a low
   confidence is better than a plausible guess, and that only real commitments
   count as tasks.
2. **`write_profile`** — rewrites a contact's narrative from everything on file.
3. **`write_brief`** — the morning brief.

Everything else is ordinary code: deduplication, phone normalisation, identity
resolution, scheduling. The model is used where judgement is needed and nowhere
else, which keeps costs low and behaviour predictable.

## Schema changes

`create_all` creates missing tables but never alters existing ones, so
`ensure_schema()` runs after it and adds any column a model has gained. The
convention is the service tracker's: columns are only ever **appended**, never
renamed, reordered or dropped, which keeps the whole migration story to one
additive `ALTER`. A non-nullable column without a server default is logged
rather than guessed at.

## Google Calendar setup

1. Google Cloud Console → new project → APIs & Services → enable **Google
   Calendar API**.
2. Credentials → Create Credentials → **OAuth client ID** → Application type
   **Desktop app**.
3. Download the JSON, save as `.credentials/client_secret.json`.
4. `python -m crm.cli google-auth` — a browser opens, you approve, a token is
   written to `.credentials/token.json`.

Only calendar scopes are requested. Email comes over IMAP.

## Swapping pieces out

- **Database**: set `DATABASE_URL` to a Postgres URL and uncomment `psycopg` in
  `requirements.txt`. Nothing else changes.
- **Transcription**: implement the `Transcriber` protocol in
  `crm/transcription/base.py` and register it in `registry.py`.
- **Calendar**: `crm/ingest/gcal.py` is the only file that knows about Google.
  The `Appointment` table is the interface.
