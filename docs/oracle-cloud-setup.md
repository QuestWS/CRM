# Oracle Cloud Free Tier, end to end

Oracle's always-free tier gives you **4 ARM cores and 24 GB of RAM** — far more
than a $6 VPS, for nothing. It is also the fiddliest of the free tiers, and
there are three places it will stop you that have nothing to do with your
software. All three are covered here.

Verified before writing this: `twentycrm/twenty`, `twentycrm/twenty-postgres-spilo`,
`postgres:16` and `redis` all publish **linux/arm64** manifests, so the whole
stack runs on Ampere as-is. No emulation, no source builds.

## 1. Create the account

Sign up at **cloud.oracle.com**. It asks for a credit card to verify identity;
Always Free resources do not charge it. Pick your home region carefully — **you
cannot change it later**, and your free resources live only there. Choose the
region closest to Ottawa IL (us-chicago-1 or us-ashburn-1).

## 2. Create the instance

**Compute → Instances → Create Instance**

| Field | Value |
|---|---|
| Name | `quest-crm` |
| Image | **Ubuntu 24.04** (Canonical) |
| Shape | **VM.Standard.A1.Flex** — Ampere, this is the free ARM one |
| OCPUs | 4 |
| Memory | 24 GB |
| Boot volume | 100 GB (you have 200 GB free to spend) |
| SSH keys | **Generate a key pair and download the private key** |

Take the 4 OCPU / 24 GB in one instance — that is the whole free ARM allowance
and there is no reason to split it.

> **Save the private key somewhere you will still have it next month.** Oracle
> shows it once. Lose it and the only fix is rebuilding the instance.

### When it says "Out of host capacity"

It will, probably more than once. The free ARM shape is heavily oversubscribed
and this is the single most common reason people give up on Oracle's free tier.

What actually works, in order:

1. **Change the Availability Domain** and try again — on the create page there
   is an AD selector. Try each one.
2. **Try again later.** Capacity frees up constantly. Early morning in the
   region's local time is better than evening.
3. **Upgrade to Pay As You Go.** This is the reliable fix and it is the one
   most people end up using. Your Always Free resources *stay free* — the
   upgrade only removes the free-tier queue position, and you are not billed
   as long as you stay inside the free allowances. It also stops Oracle
   reclaiming the instance for being idle, which Always Free accounts are
   subject to.

Do not fall back to the AMD micro shape. It is 1 GB of RAM and Twenty will not
run in it.

## 3. Open the ports — *both* firewalls

This is the second thing that stops everyone. Oracle has **two** independent
firewalls and opening one does nothing on its own.

### The cloud one (VCN Security List)

**Networking → Virtual Cloud Networks → your VCN → Security Lists → Default**

Add ingress rules:

| Source CIDR | Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | 80 |
| `0.0.0.0/0` | TCP | 443 |

### The one on the machine (iptables)

Ubuntu images on Oracle ship with iptables rules that **DROP everything except
SSH**. The security list above is necessary and not sufficient. SSH in and:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

If you skip this, the site times out with no error anywhere and everything
looks configured correctly. Hours disappear here. The provisioning script does
it for you.

## 4. Point a domain at it

Twenty builds links from `SERVER_URL`, and its IMAP integration wants a real
certificate. Use a subdomain of the shop's existing domain — `crm.questwatersports.com`
— by adding an **A record** in your DNS pointing at the instance's public IP.

You can run on the bare IP to try it, but set `SERVER_URL` to the IP then, and
change it when the domain is live.

## 5. Install everything

SSH in and run one script:

```bash
ssh -i /path/to/your-key.key ubuntu@YOUR_IP

sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/QuestWS/CRM.git /opt/quest-crm
cd /opt/quest-crm
sudo bash scripts/provision-oracle.sh crm.questwatersports.com
```

It installs Docker, fixes iptables, generates every secret, writes `.env`,
obtains a Let's Encrypt certificate through Caddy, and starts the stack. Pass
your domain as the argument; leave it off to run on the bare IP over plain
HTTP for testing.

Then open `https://crm.questwatersports.com` and create the first workspace.

## 6. After it is up

Follow **[twenty-setup.md](twenty-setup.md)** to connect
`chris@questwatersports.com`, create the API key, and start the assistant.

## Keeping it alive

- **Back up.** `scripts/backup.sh` dumps Postgres and the uploads. Put it in
  cron. Oracle's free tier gives you object storage — send the dumps there, not
  just to the same disk that would die with the instance.
- **Do not let it idle if you stayed on Always Free.** Oracle reclaims idle
  Always Free compute. A CRM syncing mail every few minutes is not idle, but if
  you upgrade to Pay As You Go the question goes away entirely.
- **Updates:** `docker compose pull && docker compose up -d`. Take a backup
  first — Twenty runs database migrations on start and they are not reversible.

## If something is wrong

```bash
docker compose ps                    # what is running
docker compose logs -f server        # Twenty's own log
docker compose logs -f worker        # mail sync lives here
docker compose exec assistant python -m crm.cli twenty-check
```

**Site unreachable:** iptables, ninety percent of the time. Re-run step 3.

**Mail never arrives:** look at `worker`, not `server`. The worker is what
fetches IMAP; if it is not running, everything else looks perfectly healthy.

**Out of memory:** `free -h`. If you took less than 24 GB, add swap:
```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```
