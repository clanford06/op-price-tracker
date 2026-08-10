# One Piece sealed box price tracker

Tracks the cheapest **delivered** price for sealed English One Piece booster
boxes, pushes a notification to your phone when one drops, and keeps a price
history you can open on any device.

Runs entirely on free infrastructure. Nothing runs on your PC — GitHub Actions
does the checking on a schedule, so it works whether or not your machine is on.

---

## What it actually does

Every 2 hours it queries the eBay Browse API for each set on your watchlist,
screens the results, records the cheapest surviving listing, and pushes to your
phone if it beats your target or sets a new all-time low.

**Why only eBay.** Every other English TCG retailer is unusable for automation.
Troll and Toad returns 401, DA Card World 403, CoolStuffInc serves an empty body
to anything that isn't a browser, and GameNerdz renders its entire product grid
in JavaScript — its search *and* category pages contain zero prices in the HTML.
Those were tested, not assumed. eBay's API is official, free, and works from any
IP, which is what makes a scheduled cloud job viable at all.

**Why the cheapest listing isn't the answer.** Counterfeit and resealed boxes
list at or below genuine market price. A naive "lowest price" bot is a
fake-finder. Nothing can prove a sealed box is authentic, so this gathers every
independently checkable signal, shows its working, and refuses anything that
fails.

### Two stages

**Stage 1 — relevance** (free, runs on every result). Title screening: is this
even the right product? Rejects wrong set, wrong language, singles, empty boxes,
cases, group breaks, repacks.

**Stage 2 — verification** (one API call per finalist). The cheapest few relevant
listings get their full item record pulled — item specifics, return terms, stock
level, seller location, category, description. Those are the cheapest listings,
which are both the ones you'd buy and the ones most likely to be fake.

### Vetoes — any one disqualifies, regardless of everything else

- Item specifics say **Language: Japanese** (or any non-English)
- Title or description contains `resealed`, `proxy`, `replica`, `counterfeit`,
  `bootleg`, `custom`, `reproduction`, `unofficial`…
- Price below the per-set `implausible_below` floor
- Seller under 100 sales or under 98% positive
- Seller explicitly refuses returns
- Ships from outside the US
- Item details could not be fetched at all
- Seller is on your personal blocklist

A 40,000-feedback Top Rated seller listing a Japanese box is still the wrong box.

### Trust score — 0–100, must clear `min_trust_score` (default 70)

| Signal | Max | What earns it |
|---|---|---|
| Language verified | 18 | structured `Language: English` in item specifics |
| Seller volume | 14 | log-scaled, saturates at 10,000 sales |
| Seller rating | 14 | 98% → 100% mapped across the range |
| Returns | 12 | accepted, longer window scores higher |
| Price plausibility | 12 | in line with the peer median, not far under |
| Top Rated Seller | 10 | eBay's own badge |
| Item specifics | 8 | seller's own fields confirm set and format |
| eBay category | 6 | listed under trading cards |
| Your trusted list | 6 | seller you've bought from before |
| Description | 4 | free of warning phrases |
| Stock level | 3 | not an implausible pile of a hot set |
| eBay programmes | 3 | Authenticity Guarantee and similar |

**Unverified listings are capped at 45**, below any sane threshold. Seller
reputation was otherwise worth ~50 on its own — but reputation says the seller
ships and answers messages, not what's in the box.

### The guarantee

**Price never overrides verification.** The tracker reports the cheapest listing
that passed everything — never simply the cheapest. Rejected cheaper listings
are shown on the dashboard with the exact reason, so you can see what was passed
over and disagree if you want.

This is built to leave money on the table rather than lose it. Rejecting a
genuine bargain costs a missed deal; accepting a fake costs the whole purchase.

### Verifying it yourself

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 30 tests
```

The suite encodes the safety properties directly — every veto is tested in
isolation, and the headline test asserts that a cheap unverifiable listing never
beats a pricier verified one. The scheduled workflow runs these before every
price check, so a bad edit to a threshold fails the job instead of quietly
recommending something.

---

## Setup

Five steps, about 20 minutes. Steps 1–3 can be done in any order.

### 1. eBay API keys

1. Sign up at [developer.ebay.com](https://developer.ebay.com) (free)
2. Create an application
3. Go to **My Account → Application Keys**
4. Copy the **Production** App ID and Cert ID — *not* Sandbox. Sandbox returns
   fake listings and will silently produce nonsense data.

### 2. ntfy for phone push

1. Install **ntfy** ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)) — free, no account
2. Generate a topic name:
   ```bash
   python3 -c "import secrets; print('op-boxes-' + secrets.token_hex(8))"
   ```
3. In the app: **+** → paste that topic → Subscribe

> The topic name is the only secret. Anyone who knows it can read *and* send
> your notifications, so use the random one — never something like `onepiece`.

### 3. Push to GitHub

```bash
cd op-price-tracker
git init && git add -A && git commit -m "Initial commit"
gh repo create op-price-tracker --private --source=. --push
```

### 4. Add secrets

**Settings → Secrets and variables → Actions → New repository secret:**

| Name | Value |
|---|---|
| `EBAY_CLIENT_ID` | Production App ID |
| `EBAY_CLIENT_SECRET` | Production Cert ID |
| `NTFY_TOPIC` | your random topic name |

### 5. Enable the dashboard

**Settings → Pages → Source: Deploy from a branch → `main` / `/docs`**

Your dashboard lands at `https://<username>.github.io/op-price-tracker/`.
Add it to your phone's home screen and it behaves like an app.

Then run it once: **Actions → Check box prices → Run workflow**.

---

## Daily use

Edit `watchlist.yaml` — it's the only file you need to touch.

```yaml
- id: op17
  name: "OP-17 The World's Strongest Warriors"
  query: "One Piece Card Game OP-17 Booster Box"
  require_any: ["op 17", "op17", "worlds strongest warriors"]
  alert_below: 110          # push a high-priority alert at or below this
  implausible_below: 75     # below this is a fake, not a deal
```

Adding a set: copy a block, change the four fields. Always include **both** the
spaced and unspaced set code (`"op 15"` and `"op15"`) — sellers write both.

Set `implausible_below` to roughly **55–65% of normal market** for that set.
Too high and you'll flag real bargains; too low and fakes get through.

### Tuning

Titles are normalised before matching — punctuation stripped, lowercased, whole
words only. So `OP-17`, `OP 17` and `op17` all compare equal, and `case` won't
fire inside `showcase`.

If a set returns nothing, your filters are too tight. Find out exactly why:

```bash
python -m tracker --only op17 --explain
```

That prints every listing with the reason it was kept, flagged, or dropped.

---

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in your keys
set -a && source .env && set +a

python -m tracker --dry-run   # search and print, write nothing, notify nobody
python -m tracker --explain   # show all screening decisions
python -m tracker --only op17 # single set
```

`--dry-run` never writes history and never sends a notification, so it's safe to
run as often as you like while tuning.

---

## Notes and limits

- **Browse API returns active listings, not sold comps.** Sold data needs the
  Marketplace Insights API, which requires approval individuals rarely get. For
  *buying*, active listings are the right data — you want what you can pay now.
- **Shipping is best-effort.** Where eBay doesn't advertise a cost, it's counted
  as $0, which can make a listing look cheaper than it delivers. Always open the
  listing before buying; the dashboard links straight to it.
- **Free tier limits:** eBay allows ~5,000 API calls/day; this uses about 50.
  GitHub Actions gives 2,000 free minutes/month on private repos; this uses
  roughly 200. Both have wide headroom.
- **Seller reputation is a proxy, not proof.** A 99%-rated seller with 5,000
  sales can still ship a resealed box. Treat every screen here as narrowing the
  field, never as a guarantee.
