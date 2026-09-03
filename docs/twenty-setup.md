# Configuring Twenty

> Need the server first? **[oracle-cloud-setup.md](oracle-cloud-setup.md)** —
> Oracle's free ARM tier, and the three things that will stop you.

## 1. First login

Open your `SERVER_URL` and create the workspace. The first account you make is
the owner. Use `chris@questwatersports.com` so invites and notifications come
from somewhere sensible.

## 2. Connect the mailbox

**Settings → Accounts → New account → IMAP/SMTP/CalDAV**

Twenty supports generic IMAP natively — it is not Google-only, which matters
because Quest's mail is on a cPanel host. `IS_IMAP_SMTP_CALDAV_ENABLED` is set
explicitly in `docker-compose.yml` so an upstream default change cannot quietly
disconnect it.

| | Host | Port | Security |
|---|---|---|---|
| **IMAP** (read) | `questwatersports.com` | 993 | SSL/TLS |
| **SMTP** (send) | `questwatersports.com` | 465 | SSL/TLS |
| **CalDAV** | leave blank unless your host runs one | | |

Username `chris@questwatersports.com`, and your normal mailbox password. **Not**
an app password — that is a Google concept and does not apply here.

Twenty tests the connection when you save, so a wrong port or password fails
immediately rather than silently.

### What to sync

Twenty syncs both received *and* sent mail from one account, which is what you
want. Without your replies the assistant sees a customer's question with no
answer and keeps producing "you owe them a reply" tasks for things you already
handled.

Under the account's settings:

- **Sync emails:** everything, or inbox+sent. Not inbox alone.
- **Contact auto-creation:** decide deliberately. On, and every stranger who
  emails you becomes a Person. For a shop that is usually right — they are
  mostly customers — but it will also create records for suppliers and
  newsletters. You can switch it on later once you have watched a week of mail.

## 3. API key for the assistant

**Settings → APIs → Create API key**

Name it `assistant`, copy the key — it is shown once — and put it in `.env`:

```bash
TWENTY_URL=http://server:3000      # inside compose; the service name
TWENTY_API_KEY=…
ANTHROPIC_API_KEY=…
```

`TWENTY_URL` is the *server* container, not your public URL. The assistant
talks to it inside the Docker network, so nothing needs to leave the box.

```bash
docker compose up -d assistant
docker compose exec assistant python -m crm.cli twenty-check
```

That prints the workspace it authenticated to. A 401 there is the key; a
connection error is `TWENTY_URL`.

## 4. Let it read

```bash
docker compose exec assistant python -m crm.cli twenty-sync
```

For each message newer than its watermark it:

- writes a **note on the person** — what happened, what they need, what is
  worth remembering about them, dates discussed, and the category it was filed
  under;
- creates **tasks for what you committed to**, never for what the customer did;
- stages a note for the service tracker or the winter system when the mail
  concerned open work there.

After that the scheduler runs it every `TWENTY_SYNC_MINUTES` (default 5).

Every write is additive — a note, a task, a link. It never edits a message,
never deletes anything, and has no send capability at all.

## Two things worth knowing about Twenty's data model

**A note carries no parent.** Attaching a note to a person is a second record,
a `noteTarget`. Same for tasks. If you write your own integration later and
your notes appear nowhere, that is why.

**There is a `callRecording` object.** Worth remembering for when you come back
to recording calls — there is somewhere native for them to live.

## Custom fields worth adding

Twenty's Settings → Data model does this in the UI, no code:

**Person**

| Field | Type | Why |
|---|---|---|
| Product interest | Multi-select | powerboat, sailboat, kayak, paddleboard, ebike, trailer |
| Assistant profile | Text | The rolling "who is this person" brief |

**Company** — if you sell to businesses (marinas, clubs), same product-interest
field.

The assistant works without them; the category is written into every note
either way. Adding them makes it filterable, which is where it earns its keep.

## When mail is not arriving

Look at the **worker**, not the server:

```bash
docker compose logs -f worker
```

The worker is what runs the IMAP sync. If it is not running, Twenty's UI looks
entirely healthy and no mail ever appears. That is the single most confusing
failure mode in this stack.

## Backups

```bash
bash scripts/backup.sh
```

Dumps Postgres and the uploads volume. Put it in cron. Twenty runs irreversible
database migrations when it starts a new version, so take one before any
`docker compose pull`.
