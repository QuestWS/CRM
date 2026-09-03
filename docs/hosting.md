# Where this runs

Your other two projects need no server: GitHub Pages serves the pages and
Google Apps Script runs the backend, both free, both somebody else's problem.
EspoCRM is different. It is a PHP application with a MySQL database that has to
be **running all the time** — a laptop that sleeps does not fetch email.

So: yes, a server this time. It is a small one, and there are three honest ways
to get it.

## First, the thing people miss

Two pieces need a home, not one:

1. **EspoCRM** — PHP + MySQL, plus a cron job every minute.
2. **The assistant** — this repo, Python, talking to Espo's API.

That matters because EspoCRM Cloud only solves the first. You would still need
somewhere for the second. One box running both is usually the simpler answer.

## Option A — your existing web host

You already pay for `questwatersports.com`, and it is cPanel-shaped, which
usually means PHP + MySQL + cron are all there. If it works, this costs nothing.

There is a real bonus: **your mail is on that same host**, so Espo's IMAP fetch
is a local connection rather than a trip across the internet.

The thing that decides it is the PHP version. EspoCRM needs **PHP 8.3 or newer,
below 8.6** (from its own `composer.json`). A lot of shared hosting is still on
8.1 or 8.2.

**Find out in five minutes:**

1. Upload `tools/host-check.php` to your web root (File Manager, or FTP).
2. Visit `https://questwatersports.com/host-check.php`.
3. Read the verdict.
4. **Delete the file.**

If it says yes, follow *Installing on cPanel* below. If the PHP version is the
only failure, look in cPanel for **MultiPHP Manager** or **Select PHP Version**
first — you can often just switch it.

The assistant still needs a home. Some cPanel hosts have **Setup Python App**
(Passenger), which would run it; if yours does not, put the assistant on a
cheap VPS or the shop PC and point it at Espo over the internet.

**Where this option bites:** shared hosting throttles CPU, and some hosts
disable `exec()`, which Espo's job daemon uses. The symptom is email that never
arrives while nothing looks broken. The host check reports both.

## Option B — a small VPS  *(what I would pick)*

Roughly **$5–10 a month** at Hetzner, DigitalOcean, Vultr or Linode. Get
**2 GB of RAM** — 1 GB runs, but MySQL and PHP together will make you regret it.

Everything lives in one place, `docker compose up -d` brings up all four
containers, and there are no shared-hosting surprises. `scripts/provision-vps.sh`
does the whole setup on a fresh Ubuntu box.

This is the option I would choose. It is the price of two coffees and it
removes an entire category of "why is this behaving strangely".

## Option C — a PC in the shop

If you have a spare desktop or a mini PC, this is free. The CRM is an internal
tool — nobody outside needs to reach it — so it can sit on the shop network with
no public address at all, which is also the most private option: customer
conversations never leave the building except for the AI calls.

**What you take on:** it has to stay on and stay patched, and when it dies on a
Saturday nobody notices until Monday. Set up the backup in
`docs/espocrm-setup.md` on day one, not later.

## Option D — EspoCRM Cloud

Their official hosting, priced per user per month — check espocrm.com for the
current figure. No server to run, and the API our assistant needs is available.

But as above, it houses Espo and not the assistant, so you end up paying monthly
*and* still finding a home for the second piece. Worth it if you want zero
sysadmin; otherwise Option B does more for less.

## Straight comparison

| | Cost | You maintain | Reachable from anywhere | Setup |
|---|---|---|---|---|
| **A** existing host | $0 | patches only | yes | 30 min, if PHP 8.3+ |
| **B** small VPS | $5–10/mo | the box | yes | 20 min, scripted |
| **C** shop PC | $0 | the box | shop network only | an afternoon |
| **D** Espo Cloud | per user/mo | nothing | yes | minutes, but only half the problem |

**Recommendation:** run the host check first, because free is free. If it passes
and your host offers Python apps, take Option A. Otherwise Option B — one $6
box, one compose file, done.

## Installing on cPanel (Option A)

1. **Database:** cPanel → MySQL Databases. Create a database and a user, give
   the user All Privileges. Write down all three values.
2. **Files:** download the EspoCRM zip from espocrm.com. Upload to a
   subdirectory — `crm/` — not your web root, so it does not collide with your
   site. Extract there.
3. **Installer:** visit `https://questwatersports.com/crm/` and follow it. Give
   it the database details from step 1.
4. **Cron — do not skip this.** cPanel → Cron Jobs, every minute:

   ```
   * * * * * cd /home/USERNAME/public_html/crm; /usr/local/bin/php -f cron.php > /dev/null 2>&1
   ```

   Replace `USERNAME` and check the PHP path (cPanel usually shows it). If your
   host only allows every 5 minutes, that is fine — mail arrives a little later.

   **This is the step people skip, and skipping it means no email ever
   arrives.** Espo will look perfectly healthy and do nothing.
5. **Force HTTPS** for the CRM directory — you are about to put customer data
   and a mailbox password into it.
6. Then follow [espocrm-setup.md](espocrm-setup.md) from *Connect the mailbox*.

## Installing on a VPS (Option B)

On a fresh Ubuntu 24.04 box, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/QuestWS/CRM/main/scripts/provision-vps.sh | bash
```

Or, if you would rather read it first (you should):

```bash
git clone https://github.com/QuestWS/CRM.git /opt/quest-crm
cd /opt/quest-crm
bash scripts/provision-vps.sh
```

It installs Docker, generates the passwords, writes `.env`, and starts
everything. Then follow [espocrm-setup.md](espocrm-setup.md).

## Whichever you pick

- **Back it up from day one.** Two Docker volumes, or one database dump plus the
  files directory. The command is in `espocrm-setup.md`.
- **HTTPS, always.** A mailbox password and customer conversations go over that
  connection. On a VPS, put Caddy or nginx in front — the provisioning script
  offers to do it with a real certificate if you point a domain at the box.
- **Restrict who can reach it.** This is an internal tool. If it is on the
  public internet it should be behind HTTPS with strong passwords, and ideally
  a firewall rule limiting it to the shop's IP.
