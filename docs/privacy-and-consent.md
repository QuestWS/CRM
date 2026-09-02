# Recording calls: consent, and what to do with the data

You are in Ottawa, so this is written for Canadian law. It is a practical
summary, not legal advice — if the business grows or you take calls from the US
or EU, get an hour with a lawyer.

## Is recording your own calls legal in Canada?

Yes. Under s. 184(2)(a) of the *Criminal Code*, a call may be recorded if **one
party consents** — and you are a party to the call, so your own consent covers
the criminal side.

**That is not the end of it.** Because you are recording customers as part of
running a business, PIPEDA (the federal private-sector privacy law) also applies,
and PIPEDA requires more than one-party consent:

1. **Notify the person** that the call is being recorded. Before the recording
   starts.
2. **Say why.** "For quality and record-keeping" is a real purpose; be honest.
3. **Offer an alternative** if they object — turn the recording off, or continue
   in writing.
4. **Only use it for the stated purpose.**
5. **Protect it**, and don't keep it longer than you need.

The announcement in [sangoma-setup.md](sangoma-setup.md) covers points 1–3. That
is exactly why you were right to ask for it.

## Outbound calls

Your inbound announcement won't play to someone you dial. Say it out loud at the
start: *"Just so you know, I record my calls for my own notes — let me know if
you'd rather I didn't."* One sentence.

The CRM flags every recording where the PBX did not confirm an announcement
played (`announcement not confirmed` on the call page). Treat that flag as a
prompt to check, not as decoration.

## Calling the US

Some US states (California, Florida, Pennsylvania, Illinois, Washington and
others) require **all** parties to consent. If you take or make calls across the
border, use the announcement on every call and let people opt out. Announcing on
all calls regardless of jurisdiction is the simple, safe policy.

## If someone says no

Stop recording that call. On FreePBX the one-touch recording toggle is `*1` by
default — check yours. Delete anything already captured for that call, and make
a note in the CRM that this contact declines recording.

## What this system stores

| Data | Where | Contains |
|---|---|---|
| Call audio | `media/recordings/` on your machine | Everything said on the call |
| Transcripts | Your database | The same, as text |
| Extracted facts | Your database | Personal details customers mentioned |
| Email bodies | Your database | Whatever is in your mail |
| AI profiles | Your database | A written summary of each person |

Two things worth being deliberate about:

**The facts table is the sensitive part.** It is a deliberate record of personal
details people mentioned in passing — family, health, plans. That is the point of
the feature, and it is also exactly the kind of data that is uncomfortable if it
leaks or if someone sees their own profile and finds it creepy. Keep it to things
you would be comfortable saying to their face: *"How did your daughter's move to
Carleton go?"* is warm. A logged note about someone's medical situation is not.
Prune facts you don't need — every fact has an archive action.

**Access requests.** Under PIPEDA a person can ask what you hold about them, and
you have to tell them. The contact page is that answer — you can read it
straight off the screen. They can also ask you to correct or delete it.

## Retention

Nothing here deletes itself. Decide on a period — twelve months is a common
default for a small service business — and enforce it on both the PBX and this
CRM. A cron job over `media/recordings/` plus a delete of the matching rows is
enough. Keeping recordings forever "just in case" is the option with the most
downside and the least benefit.

## Where the data goes

- **Audio** stays on your machine when `TRANSCRIPTION_ENGINE=faster_whisper`
  (the default). Nothing is uploaded. If you switch to Deepgram, audio is sent
  to Deepgram.
- **Transcripts and email text** are sent to the Anthropic API for summarisation
  and extraction. Anthropic does not train on API data by default. If a specific
  customer's calls are too sensitive for that, don't record them.
- **Your database** is a plain SQLite file by default. It is not encrypted.
  Encrypt the disk (FileVault, BitLocker, LUKS) — that is the single highest-value
  security step for this system, and it takes ten minutes.
- **Back it up.** `data/crm.db` and `media/` are the whole system. Losing them
  loses every profile you have built.
