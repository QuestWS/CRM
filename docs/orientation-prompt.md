# A prompt for Claude chat

Copy everything in the box below into a new conversation at
[claude.ai](https://claude.ai). It gives Claude your real situation so the
answers fit your setup rather than a generic one.

Then bring your questions and decisions back to Claude Code here.

---

```
I run Quest Watersports, a marine dealership in Ottawa, Illinois. We sell and
service powerboats, sailboats, kayaks, paddleboards and e-bikes, run a boat
club, and do winter storage, shrink wrap and winterization. Our shop software
is BiT Dealership Software, which has no API and no export — everything in and
out of it is a human retyping.

I've built two working apps with Claude Code and I understand them well:

1. A service tracker — mechanics scan a QR code on a printed work order and log
   hours, parts, photos and voice notes from their phone. Static pages on
   GitHub Pages, with Google Apps Script and a Google Sheet as the backend.
2. A winter services quoting system — customers quote, sign and pay online.
   Same shape: GitHub Pages plus Apps Script plus a Sheet.

Both were free to run and needed no server at all, because Google runs the
backend for me. That's the extent of my hosting experience.

Now I'm building something bigger and I want to genuinely understand it before
I flip it on, not just follow instructions. The plan is:

- **Twenty CRM**, an open-source CRM, self-hosted, as the system of record for
  customers, email, notes and tasks.
- Running on an **Oracle Cloud Free Tier** server (their always-free ARM
  instance: 4 cores, 24 GB RAM).
- Plus an **AI assistant service** I've had Claude Code build. It reads the
  email Twenty has fetched, works out what each message was about, writes a
  summary onto the customer's record, creates tasks for things I committed to,
  and links conversations to open work orders in my two existing apps.
- My email is chris@questwatersports.com, on a normal cPanel web host —
  IMAP on port 993, SMTP on 465. Not Gmail, not Microsoft.
- Later I want to add phone call recording and transcription (we use a Sangoma
  phone system), but I've parked that for now.

I am not a developer. I'm comfortable with GitHub, copying commands, and
editing config files, and I've never run a server, used Docker, or managed a
domain beyond basic DNS.

**What I want from you: teach me, don't build for me.** The building happens in
Claude Code. What I need here is to actually understand what I'm agreeing to
own.

Please work through these with me, roughly in this order, and stop after each
to check I've got it before moving on:

1. Why do my other two projects need no server, and why does this one? What
   changed?
2. What is a "server" or "instance" in this context? What am I actually renting
   from Oracle, and what does "free tier" really mean — what's the catch?
3. What is Docker, and what is a container? Why is my CRM four containers
   (server, worker, database, Redis) instead of one program? What does each do,
   and what breaks if one stops?
4. What is a database, and why does Twenty need PostgreSQL when my other apps
   just use a Google Sheet?
5. Domains and HTTPS: what happens when I point crm.questwatersports.com at
   this server? What is DNS, an A record, a reverse proxy, and a certificate,
   and why do I need each one?
6. IMAP vs SMTP: what's the difference between reading my mail and sending it,
   and what is the CRM actually doing to my mailbox? Should I be worried about
   it touching my email?
7. What is an API and an API key, and what does it mean that my AI assistant is
   a separate service talking to Twenty over one?
8. What am I signing up to maintain? Backups, updates, security, what happens
   when it breaks on a Saturday, and how much time per month is realistic.
9. What are the running costs, honestly? The server is free — what isn't?
   (The AI part calls the Anthropic API per email.)
10. Privacy and legal. Illinois is an all-party consent state for recording
    conversations, and there's a biometric privacy law (BIPA) here too. I'll be
    storing customer emails and, later, call recordings and transcripts. What
    should I understand about that?

Style I'd like: plain language, no jargon without explaining it first, and use
analogies from a service shop where they help — I'll understand a comparison to
a work order or the yard better than one to a web app. Ask me questions to
check I've followed rather than assuming. If I say something that reveals I've
misunderstood, correct me directly.

At the end, help me produce two lists to take back to Claude Code:
- decisions I need to make, with the tradeoffs
- things I still don't understand well enough to be comfortable with

Let's start with #1.
```

---

## Why this prompt is shaped like this

- **It leads with what you already know.** The fastest way to learn the new
  thing is by contrast with the thing you understand, and you genuinely
  understand the Apps Script model.
- **It says "teach me, don't build for me."** Chat Claude will otherwise start
  writing configuration files, which is not what you need from it and will just
  conflict with what is already in this repo.
- **It asks for one topic at a time with a comprehension check.** Ten topics in
  one answer is a wall of text you will skim.
- **It ends by producing a list.** So the conversation has an output you can
  act on, rather than a feeling of having understood something yesterday.

## Coming back here

Bring the two lists. The decisions are the useful part — most of them
(auto-create contacts from unknown senders? which folders to sync? how much
history to back-fill?) are ones I have picked a default for, and your answer
may well be different once you understand the tradeoff.

If something in the setup docs doesn't make sense after that conversation, say
so and I will rewrite that section. A doc you cannot follow is a broken doc,
not a you problem.
