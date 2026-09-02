# Recording calls: consent, and what to do with the data

Quest Watersports is in **Ottawa, Illinois**. That matters more than almost any
other fact about this system, so it comes first. This is a practical summary,
not legal advice — the exposure here is large enough to be worth an hour with an
Illinois attorney before you go live.

## Illinois is an all-party consent state

Under the Illinois Eavesdropping Act (720 ILCS 5/14-2), recording a **private
conversation** requires the consent of **every** party, not just you. Illinois
is one of a minority of states that work this way, and it is one of the
stricter ones: a violation is a criminal offense, and recording a phone
conversation without all-party consent is a Class 4 felony on a first offense.

So the recording announcement is not a courtesy here. It is the mechanism that
makes the whole system lawful.

**How consent actually works in practice:** you tell the person at the start
that the call is recorded, and they stay on the line. Continuing the
conversation after a clear notice is treated as consent. What makes that hold
up is that the notice was clear, came *before* the recording captured anything
of substance, and that you stopped when someone objected.

Three things follow from that:

1. **The announcement plays on every call, inbound and outbound.** Not most.
   Every one.
2. **It has to come first**, before the conversation starts — not after you
   have already been talking for thirty seconds.
3. **If someone says no, you stop recording, and you delete what you have.**

### The beep is not enough on its own

The Sangoma app's "warning beep" every 15 seconds is a recording-notification
convention, but it does not say *who* is recording or *why*, and a customer who
does not recognise the convention has not knowingly consented to anything. In
an all-party state, leaning on a beep alone is the weakest version of the
argument you could make.

Keep the beep on — it is a continuing reminder, which is genuinely useful — but
put a **spoken notice** in front of it:

- **Inbound:** an announcement on the PBX inbound route, before it rings
  through. See [sangoma-setup.md](sangoma-setup.md).
- **Outbound:** say it yourself, first sentence, every time. *"Morning, it's
  Quest Watersports — just so you know, I record my calls for the shop's
  notes. That alright?"* Then wait for the answer.

The CRM flags every recording where an announcement was not confirmed
(`announcement not confirmed` on the call page). In Illinois, treat that flag as
something to act on, not decoration.

### The counter recorder

The walk-in intake button records a conversation at your service desk. The same
statute applies, and there is no PBX to play an announcement for you: **you have
to ask.** *"Mind if I record this so I get your job written up right?"* Then
press the button.

A person who says no still gets served — type the intake instead. The button is
a convenience, never a condition of doing business.

A sign at the counter helps, but on its own a sign is weaker than asking: the
statute turns on the parties' consent, and you cannot show someone read a sign.

### Calls that cross state lines

Boat customers travel. If you are recording someone in another state, the safe
assumption is that the stricter of the two states' laws applies. Announcing on
every call regardless of where the other person is means you never have to work
out which rule governs a particular call.

## A second Illinois problem: voiceprints and BIPA

Illinois' Biometric Information Privacy Act (740 ILCS 14) treats a
**voiceprint** as a biometric identifier, requires written consent before you
collect one, and lets people sue directly with statutory damages per violation.
BIPA litigation is a real industry in Illinois.

Where this system currently sits:

- **Speaker diarisation** — what AssemblyAI does, and what splitting a stereo
  call does — separates speakers *within one recording*. It does not build a
  stored template that identifies a person across recordings. That is generally
  understood not to be a voiceprint under BIPA.
- **Speaker identification** — recognising that the voice on today's call is the
  same person as last month's — *is* a voiceprint, and would put you squarely
  inside BIPA.

So: do not turn on cross-recording speaker identification, in AssemblyAI or
anywhere else, without written consent and a published retention schedule
first. It is a feature that looks like a small convenience and carries an
outsized legal cost in this state specifically.

## What this system stores

| Data | Where | Contains |
|---|---|---|
| Call and counter audio | `media/recordings/` on your machine | Everything said |
| Transcripts | Your database | The same, as text |
| Extracted facts | Your database | Personal details customers mentioned |
| Email bodies | Your database | Whatever is in your mail |
| AI profiles | Your database | A written summary of each person |
| Work order mirror | Your database | Customer and boat details from the shop app |

Two things worth being deliberate about:

**The facts table is the sensitive part.** It is a deliberate record of personal
details people mentioned in passing — family, plans, health. That is the point
of the feature, and it is also exactly the kind of data that is uncomfortable if
it leaks or if someone reads their own profile and finds it creepy. Keep it to
things you would be happy saying to their face: *"How did the wedding go?"* is
warm. A logged note about someone's finances or health is not. Every fact has an
archive action — use it.

**Access requests.** Someone can ask what you hold about them. The contact page
is that answer — you can read it straight off the screen.

## Retention

Nothing here deletes itself, and the Sangoma app defaults to **Keep forever**.
Both are worth changing.

Pick a period — twelve months is a common default for a small service business —
and apply it in three places, because there are three copies:

1. **The phone**, via the app's "Delete after" setting. Set this only once
   uploads to the CRM are confirmed working.
2. **The PBX**, if it is also recording.
3. **This CRM** — `media/recordings/` plus the matching rows.

Keeping recordings forever "just in case" is the option with the most downside
and the least benefit, and in a state with felony exposure and a biometric
statute, an indefinite archive of customer voices is the wrong thing to be
sitting on.

## Where the data goes

- **Audio** stays on your machine with `TRANSCRIPTION_ENGINE=faster_whisper`.
  With `assemblyai` — the engine the shop already uses for mechanic voice notes
  — audio is uploaded to AssemblyAI. Both are legitimate choices; know which one
  you are running.
- **Transcripts and email text** go to the Anthropic API for summarisation.
  Anthropic does not train on API data by default.
- **Notes pushed to the service tracker** land in the shop's job log as
  `writer_note` entries, which `customerView_` filters out of the customer page
  by construction. Nothing this CRM writes can reach a customer.
- **Your database** is a plain SQLite file and is not encrypted. Encrypt the
  disk — it is the highest-value ten minutes of security work here.
- **Back it up.** `data/crm.db` and `media/` are the whole system.
