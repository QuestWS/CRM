# Recording calls on Sangoma

## The thing to understand first

**The Sangoma app on your phone cannot record your calls, and no software you
install on the phone can make it.** This is not a limitation of this project —
it is how both platforms work:

- **iOS** gives no app access to call audio. Not the Sangoma app, not any other
  app. Apple has never opened this, and apps that claim to do it route your call
  through a third-party bridge instead.
- **Android** closed the call-audio recording APIs in Android 10 (2019).

Screen-recorder and "call recorder" tricks are unreliable, capture only your own
microphone on many handsets, break on OS updates, and produce audio too poor to
transcribe well.

**The PBX is the correct place to record.** It sits in the middle of every call,
sees both legs, and Sangoma's platform already supports recording and consent
announcements as first-class features. You configure it once and it applies to
every device on your account — desk phone, softphone, the mobile app, all of it.
Nothing needs to be installed on the phone.

This also means recording keeps working when you switch phones, and your phone's
battery and storage are untouched.

---

## Which Sangoma product do you have?

The steps differ slightly. Check by logging into your admin portal:

| Product | Admin looks like | Where recording lives |
|---|---|---|
| **FreePBX** (self-hosted, free) | Web GUI at your PBX's IP | Settings → Advanced Settings, plus per-extension |
| **PBXact** (Sangoma's appliance) | Same GUI as FreePBX | Same as FreePBX |
| **Sangoma Business Voice / Switchvox** (cloud) | portal.sangoma.com or your Switchvox admin | Admin portal → Call Recording |

If you are not sure, the fastest answer is to call Sangoma support and ask:
*"Is call recording enabled on my account, and can I turn on the recording
announcement?"* Both are standard features.

---

## Part 1 — Turn on recording

### FreePBX / PBXact

1. **Per extension** (recommended — it covers the mobile app, which registers as
   an extension):
   Applications → Extensions → *your extension* → **Recording** tab
   - Inbound External Calls: **Force**
   - Outbound External Calls: **Force**
   - Inbound Internal Calls: Don't Care (or Force)
   - Outbound Internal Calls: Don't Care (or Force)

2. **Globally**: Settings → Advanced Settings → search "Call Recording".
   Confirm `Call Recording Location` — the default is
   `/var/spool/asterisk/monitor`, organised as `YYYY/MM/DD/`.

3. **Record both legs on separate channels.** This is the single highest-value
   setting for this CRM. Settings → Advanced Settings → set
   `MixMonitor Recording Options` (or the equivalent `mixmon` options field) to:

   ```
   b
   ```

   The `b` flag records the two directions on separate stereo channels. This CRM
   detects a two-channel file, transcribes each leg separately, and labels who
   said what — which is far more accurate than trying to guess speakers from a
   single mixed track. Without it you still get a transcript, just with weaker
   speaker attribution.

4. Apply Config.

### Sangoma Business Voice / Switchvox (cloud)

Recording is a per-user or per-account feature toggled in the admin portal
(often a paid add-on). Enable it for your extension, and note where recordings
are retrieved — usually a portal download, an API, or an SFTP drop. All three
work with this CRM (see Part 3).

---

## Part 2 — The consent announcement

You asked for the "this call is being recorded" message, and you are right to.
See [privacy-and-consent.md](privacy-and-consent.md) for why it matters legally.

### FreePBX / PBXact

FreePBX ships a built-in announcement for exactly this.

1. **Record or upload the message.** Admin → System Recordings → Add Recording.
   Either dial `*77` from your phone to record it, then `*99` to play it back,
   or upload a WAV/MP3.

   Suggested wording — short, and it names who is recording:

   > "Hello, you've reached *[your business]*. This call may be recorded for
   > quality and record-keeping purposes. If you'd prefer not to be recorded,
   > let me know when we connect and I'll turn it off."

   Keep it under about eight seconds. Callers hang up on long preambles.

2. **Attach it to inbound calls.** The cleanest approach is an Announcement
   object played before the call reaches you:

   Applications → Announcements → Add Announcement
   - Recording: the one you just made
   - Destination: your extension / ring group / queue
   - **Don't Answer Channel: No** — you want the caller to hear it
   - Allow Skip: **No** (a skippable notice is not a reliable notice)

   Then point your inbound route at this Announcement instead of directly at
   your extension: Connectivity → Inbound Routes → *your route* → Set
   Destination → Announcements → *your announcement*.

3. **Outbound calls.** Callers you dial won't hear the inbound announcement, so
   say it yourself at the top of the call. Make it a habit — one sentence. The
   CRM shows a "announcement not confirmed" flag on every recording where the
   PBX didn't confirm one played, which is your reminder to check.

   If you want this automated on outbound too, it needs a custom dialplan
   context that plays the message to the far end before bridging. Sangoma
   support or a FreePBX consultant can set this up; it is beyond what the GUI
   exposes.

### Switchvox / Business Voice

Look for "Call Recording Announcement" or "Recording Notification" in the admin
portal — it is a checkbox on most plans. If your plan doesn't have one, build
the same thing with an IVR greeting on your inbound route.

### Confirming it worked

Call your own number from a mobile. You should hear the announcement before it
rings through. Then check that a file appeared in the recording folder.

---

## Part 3 — Getting recordings into the CRM

Three ways in. Use whichever fits your setup; you can use more than one, and
duplicate files are ignored (matching is by content hash, not filename).

### Option A — Watch a folder (simplest for self-hosted FreePBX/PBXact)

Mount or sync the PBX's recording folder to the machine running the CRM, then
point the CRM at it:

```bash
# in .env
SANGOMA_WATCH_DIR=/mnt/pbx-recordings
```

A read-only mount is enough — the CRM copies files into its own storage and
never writes to the PBX.

Syncing on a schedule works just as well if you'd rather not mount anything:

```bash
# on the CRM machine, every 5 minutes via cron
rsync -az --ignore-existing \
  pbx-user@your-pbx:/var/spool/asterisk/monitor/ /mnt/pbx-recordings/
```

The CRM rescans every `PIPELINE_POLL_SECONDS` (default 30) and processes
whatever is new.

### Option B — Webhook from the PBX (best for near-real-time)

Have the PBX POST to the CRM as soon as a recording is finished. On FreePBX, add
a post-call hook in `/etc/asterisk/extensions_custom.conf` that calls a small
script, or run this script from a cron job that watches for new files:

```bash
#!/usr/bin/env bash
# /usr/local/bin/crm-notify.sh  — called with the recording path
set -euo pipefail
RECORDING="$1"

curl -sS -X POST "https://your-crm-host/api/sangoma/recording" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: ${CRM_WEBHOOK_SECRET}" \
  -d @- <<JSON
{
  "recording_url": "https://your-pbx/recordings/$(basename "$RECORDING")",
  "src": "${CALLERID_NUM:-}",
  "dst": "${EXTEN:-}",
  "direction": "inbound",
  "start_time": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "uniqueid": "${UNIQUEID:-}",
  "consent_announced": true
}
JSON
```

The endpoint accepts either `recording_url` (it fetches the file) or
`recording_path` (a path the CRM itself can read — useful when both run on the
same box). Field names are flexible: `src`/`from`/`caller`/`callerid` are all
understood, as are `dst`/`to`/`destination`/`did`. Set `consent_announced: true`
only if the announcement actually played — the CRM surfaces that flag and you
want it to mean something.

Set the shared secret on both sides:

```bash
# in .env
SANGOMA_WEBHOOK_SECRET=a-long-random-string
```

The endpoint returns `401` without a matching `X-Webhook-Secret` header. If you
expose the CRM to the internet, put it behind HTTPS — the secret and the audio
both travel over that connection.

### Option C — Manual upload

Open **Calls → Add a recording** in the web UI and drop a file in. Useful for
backfilling old recordings, or for a cloud PBX whose only export is a download
button.

Keep the PBX's original filename if you can — names like
`out-6135551234-201-20240115-143022-1705345822.14.wav` carry the caller, the
direction and the timestamp, and the CRM reads all three out of them.

---

## Verifying the whole path

```bash
python -m crm.cli doctor          # config check
python -m crm.cli import-calls    # scan the watch folder and process
python -m crm.cli add-call /path/to/one-recording.wav   # process a single file
```

Then open the web UI at `/calls`. A recording should move
`pending → transcribing → transcribed → analyzing → done`. If it stops at
`failed`, the reason is shown on the call's page.

---

## A note on retention

Recordings and transcripts of customer calls are personal information. Decide
how long you keep them and stick to it — see
[privacy-and-consent.md](privacy-and-consent.md). The PBX and the CRM keep
separate copies; a retention policy needs to cover both.
