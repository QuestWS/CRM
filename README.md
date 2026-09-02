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

## Before you start: the phone app

**Your Sangoma phone app cannot record its own calls.** iOS gives no app access
to call audio, and Android closed those APIs in 2019. Recording happens on the
**PBX**, which sits in the middle of every call and already supports both
recording and the consent announcement as built-in features.

This is better anyway: it covers every device on your account, survives a phone
upgrade, and needs nothing installed on the phone.

Full walkthrough: **[docs/sangoma-setup.md](docs/sangoma-setup.md)**.

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
