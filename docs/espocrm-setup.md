# Standing up EspoCRM

> **Need somewhere to run it first?** [hosting.md](hosting.md) covers the four
> options and how to check whether your existing web host will do. This page
> assumes you have a box.

EspoCRM is the system of record: contacts, email, tasks, calendar, cases. This
service is a sidecar that reads what Espo fetched, runs the analysis, and
writes back. Espo does the CRM; this does the judgement.

## 1. Run it

```bash
cp .env.example .env
```

Fill in these four before anything starts — compose refuses to run without them,
deliberately, so nothing ever comes up on a default password:

```bash
ESPO_ADMIN_PASSWORD=…      # your login to EspoCRM
ESPO_DB_PASSWORD=…         # random, you will never type it again
ESPO_DB_ROOT_PASSWORD=…    # random, same
ANTHROPIC_API_KEY=…        # without it, no summaries
```

Then:

```bash
docker compose up -d
open http://localhost:8080          # log in as admin
```

Four containers: EspoCRM, its database, **`espocrm-daemon`**, and the
assistant. The daemon is not optional — it is what runs Espo's scheduled jobs,
including fetching your mail. Without it the inbox stays empty and nothing
looks broken, which is a confusing hour to lose.

## 2. Connect the mailbox

Quest's mail is on a cPanel-style host, so it is a plain password over SSL —
**no App Password**, that is a Google thing.

**Administration → Inbound Emails → Create Inbound Email**

| Field | Value |
|---|---|
| Name | Quest — chris |
| Email Address | `chris@questwatersports.com` |
| Host | `questwatersports.com` |
| Port | `993` |
| Security | `SSL` |
| Username | `chris@questwatersports.com` |
| Password | your mailbox password |
| Monitored Folders | `INBOX` |
| Fetch Since | pick a date — start ~2 weeks back, not "all" |
| Keep Fetched Emails Unread | **yes** |

That last one matters: leave it on and Espo reads your mail without marking it
read underneath you.

**Tick "Create Case"** only if you want every unmatched email to open a case.
For a shop this is usually noise — leave it off, and email still lands on the
contact's stream.

### Sent mail

An Inbound Email account fetches one folder. To see your replies too, add a
second account against the Sent folder — usually `INBOX.Sent` on cPanel; check
what your server calls it.

Worth doing. Without your replies, the assistant sees a customer's question
with no answer and keeps generating "you owe them a reply" tasks for things you
already handled.

### Outbound

**Administration → Outbound Emails**: host `questwatersports.com`, port `465`,
security `SSL`, auth on, same username and password.

The assistant never sends anything. Outbound is Espo's, for when you click
send.

## 3. Make an API user for the assistant

**Administration → API Users → Create API User**

- Name: `assistant`
- Authentication Method: **API Key**
- Roles: one that can read Email, Contact, Account, Lead, and create Note and
  Task. Start from a copy of a standard role rather than granting admin — the
  assistant should not be able to delete anything.

Copy the generated key into `.env`:

```bash
ESPO_URL=http://localhost:8080
ESPO_API_KEY=…
```

Then check it end to end:

```bash
docker compose exec assistant python -m crm.cli espo-check
```

That prints the user it authenticated as and a count of every entity it can
read. A 403 here means the role, not the key.

## 4. Let it read

```bash
docker compose exec assistant python -m crm.cli espo-sync
```

It works through email newer than its watermark and, for each one:

- posts a **summary note** to the contact's stream — what happened, what they
  need, what is worth remembering about them, and any dates discussed;
- creates **tasks** for what *you* committed to (not what the customer did);
- stages a note for the shop apps if the email concerned open work there.

After that the scheduler runs it every `ESPO_SYNC_MINUTES` (default 5).

Nothing is edited or deleted. Every write is additive, and the assistant has no
send capability at all.

## What it does not do, on purpose

- **No email is sent.** Espo sends when you click send.
- **No email is marked read or moved.** Espo fetches; the assistant only reads
  from Espo's copy.
- **Spam and internal mail get no stream note.** They are categorised and
  otherwise left alone, because a robocall in somebody's timeline is noise.
- **Tasks are only for your commitments.** "I'll send the quote Thursday"
  becomes a task; "send us your hull number" does not — that one is theirs.

## Custom fields worth adding

Espo's Entity Manager does this through the UI, no code:

**Entity Manager → Contact → Fields → Add Field**

| Field | Type | Why |
|---|---|---|
| `assistantProfile` | Text | The rolling "who is this person" brief |
| `productInterest` | Multi-Enum | powerboat, sailboat, kayak, paddleboard, ebike, trailer |

**Entity Manager → Email → Fields**

| Field | Type | Why |
|---|---|---|
| `assistantCategory` | Enum | service, sales, boat_club, rental, parts, storage, billing, vendor, internal, personal, spam, other |

The assistant works without these — the category is written into the stream
note either way. Adding them makes the category filterable and reportable,
which is where it earns its keep.

## Backups

Two volumes hold everything: `espocrm-db` (the data) and `espocrm-data`
(uploads and config). Back up both. A database dump alone restores your records
without your attachments.

```bash
docker compose exec espocrm-db \
  mariadb-dump -u espocrm -p espocrm > espo-backup-$(date +%F).sql
```
