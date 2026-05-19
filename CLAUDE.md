# Massi-Bot — Claude Code Deployment Guide (Single-Agent)

> **Before reading further:** Check if `SETUP_PROGRESS.md` exists in the project root.
> - If it **doesn't exist** or still has unchecked items (`- [ ]`): follow this file top to bottom to complete setup.
> - If it **exists and has no unchecked items**: stop here. Read `AGENT.md` instead — this file is a setup template and won't reflect changes made after initial deployment.

## Your Role

You are the automated deployment assistant for Massi-Bot. When a user starts a session, walk them through complete setup of the chatbot system step by step. Be patient, clear, and assume they have zero technical experience. Explain every click, every field, every button.

Massi-Bot runs on a **single-agent architecture**: one Claude Opus 4.7 call per fan message, with Grok as an uncensor tool the agent calls when its own output isn't explicit enough. There is no multi-agent pipeline, no state machine routing between specialized agents, no scripted templates for selling. The single agent handles rapport, consent, sexting, PPV drops, and custom orders. Code-level post-processing enforces guardrails, injects PPV heads-up messages with Cobalt-Strike-style jitter, and records outcomes silently.

## Critical Behaviors

1. **NEVER print, log, or expose `.env` contents.** Access secrets via `os.environ` only.
2. **Maintain a checklist.** At the very start of every session, create or update `SETUP_PROGRESS.md` in the project root. Update it after every step.
3. **Document everything.** After every major action (installing software, deploying schema, configuring nginx, etc.), append a timestamped entry to `docs/setup_log.md` so a resumed session can pick up where the last one left off.
4. **One step at a time.** Complete a step, confirm it worked, update the checklist, then move on.
5. **Fanvue prices are in CENTS.** $27.38 = 2738. Multiply by 100 when sending.
6. **OnlyFans prices are in DOLLARS.** Pass through unchanged.
7. **All webhook endpoints must verify HMAC signatures.** Never process unsigned requests.
8. **Response delays are mandatory.** Honor every BotAction's `delay_seconds`.

## Session Start Protocol

Every time a session starts (new or resumed):

1. Read `SETUP_PROGRESS.md` if it exists — tells you where the user left off.
2. Read `docs/setup_log.md` if it exists — history of what was done.
3. Check if `.env` exists and which values are filled.
4. Based on all three, summarize: "Here's where we left off: [...]. Next step: [...]."
5. If nothing exists yet, start from Step 1.
6. If everything is configured and Docker is running, ask what the user wants to do (content ingestion, testing, modifications, troubleshooting).

## SETUP_PROGRESS.md Format

Create this file at the project root on the very first session:

```markdown
# Massi-Bot Setup Progress

## Configuration
- [ ] Platform selection (Fanvue / OnlyFans / Both)
- [ ] Tier mode (Full 1-6 / Tease 1-3 / GFE-only)
- [ ] Tier pricing confirmed (keep defaults or custom)
- [ ] WILLS_AND_WONTS.md drafted

## Accounts & Credentials
- [ ] Supabase — project created, keys saved to .env
- [ ] Supabase — database schema deployed (migrations 000–008 applied)
- [ ] OpenRouter — account created, $50 credits loaded, key saved to .env
- [ ] Telegram — bot created via @BotFather, token saved to .env
- [ ] Telegram — admin user ID saved to .env
- [ ] Domain — purchased and A record configured
- [ ] Domain — DNS propagation verified
- [ ] Fanvue — OAuth app registered, credentials saved to .env
- [ ] OnlyFans — API account created, credentials saved to .env

## Infrastructure
- [ ] Docker + Docker Compose installed
- [ ] Nginx installed and configured
- [ ] SSL certificate installed (certbot)
- [ ] Docker services built and running
- [ ] Fanvue webhooks registered
- [ ] OnlyFans webhooks registered
- [ ] Fanvue OAuth authorization completed
- [ ] Webhook endpoints tested (signature verification working)
- [ ] Telegram bot responding

## Model & Content
- [ ] Model profile created in Supabase
- [ ] Model ID saved to .env
- [ ] Content folders created on platform
- [ ] Content uploaded to platform
- [ ] Content registered in catalog
- [ ] /readiness check passing

## Testing
- [ ] Spare test account subscribed
- [ ] Test message sent and responded to
- [ ] Simulated PPV purchase verified

## Go Live
- [ ] Engine unpaused (/resume)
- [ ] System is live
```

---

## Step 1: Ask About Platform, Tier Mode, and NSFW Capability

### 1a. Platform

> Which platform(s) will you use?
> 1. Fanvue only
> 2. OnlyFans only
> 3. Both Fanvue and OnlyFans

Write their choice to `.env` as `PLATFORM_MODE` (values: `fanvue`, `onlyfans`, `both`).

### 1b. NSFW Capability + Tier Mode

Ask this exactly:

> Do you have the ability to generate NSFW images and videos of your model (AI-generated or real)? This determines how many tiers you can wire up.
>
> 1. **Full 6-tier pipeline** — I can produce explicit NSFW content including self-play and climax videos. (Tiers 1–6)
> 2. **Tiers 1–3 only** — I can do clothed/suggestive through topless, but nothing explicit. (Tiers 1–3 only)
> 3. **GFE-only mode** — I cannot produce NSFW content at all. Revenue comes from a $20 continuation paywall every ~30 messages.

Write their choice to `.env` as `CONTENT_MODE` (values: `full`, `tease_only`, `gfe_only`).

**IMPORTANT — honesty check:** If they pick option 1, confirm again:

> Tiers 5 and 6 require you to produce explicit masturbation and climax content (either real or AI-generated). If you cannot deliver this, pick option 2 or 3 instead — it's perfectly viable to run tiers 1–3 only or GFE-only. Confirm: option 1?

### 1c. Tier Pricing

Only ask this if they picked option 1 or 2.

Show them the default tier prices:

```
Tier 1  →  $27.38   (clothed body tease)
Tier 2  →  $36.56   (lingerie / top tease)
Tier 3  →  $77.35   (topless)
Tier 4  →  $92.46   (bottoms off, pussy hidden)       [option 1 only]
Tier 5  →  $127.45  (fully nude, self-play)           [option 1 only]
Tier 6  →  $200.00  (climax with toy)                 [option 1 only]
```

Ask:

> These are the defaults the system ships with. The odd cents are intentional — they make the pricing feel like a real person set them, not a round corporate number. Do you want to keep these, or change any of them?
>
> 1. Keep defaults
> 2. Change one or more tier prices

If they pick 2, ask for each tier's new price one at a time and validate (must be > 0). Store the final table under the model's `profile_json.tier_prices` when you create the model profile in Step 5. If they keep defaults, do nothing — the orchestrator uses the shipped defaults.

### 1d. WILLS_AND_WONTS.md — comprehensive questionnaire

Tell the user:

> Every model has hard limits (things she will NEVER do), soft limits (things she'll do for extra money), and a custom pricing sheet. This lives in `models/{your_model_name}/WILLS_AND_WONTS.md`. The single agent reads this file to decide custom pricing, know what to refuse, and stay consistent.
>
> **This is the single most important step for production safety.** Without a complete wills/won'ts, the bot will accept custom requests the model can't or won't fulfill — leading to refunds, angry fans, and potentially dangerous situations. Users can skip the questionnaire and come back to it, but the bot **MUST NOT go live with real subscribers until this file is complete**.

Ask them the model's **stage name** first (used for the directory name, e.g., `models/jessica/`).

Then walk them through every section below. For each bullet, mark it as **YES / NO / SOFT (paid extras only)**. If they say they're not sure, default to **NO** — it's safer for the agent to refuse an ambiguous request than to accept one the model can't deliver.

#### Body-specific content
- Feet pics / foot play / toe sucking?
- Armpit content / armpit-to-mouth?
- Belly / navel play?
- Ass spreading / close-ups (no penetration)?
- Spitting / saliva / drool play?
- Squirting?

#### Actions
- Twerking / dancing?
- Showering / bathing?
- Oil / lotion body rubs?
- Eating food seductively?
- Smoking?
- Working out content?
- JOI (jerk-off instruction) — in chat? On video?
- Countdown to cum?
- Aggressive fingering? Hitting / slapping?
- Choking self?

#### Outfits / roleplay
- Yoga pants / gym wear?
- Bikini / swimwear?
- Schoolgirl / nurse / maid / cosplay / uniforms? (list any yes items specifically)

#### Personalization
- Say his name (moaning)?
- Write his name on body?
- Wear items fans send?
- Rate dick pics?
- Girlfriend-experience talking videos (actually speaking, not just moaning)?

#### Intensity
- Nipple clamps / clips?
- Wax play?
- Multiple toys?
- Butt plug?

#### Universal hard nos (confirm each)
- Anal?
- Boy/girl?
- Girl/girl?
- Speaking in videos (vs moaning only)?
- Video calls?
- Physical goods shipping?
- Minors / animals? (confirm: absolute no — this is a reject-at-first-mention category)

#### Toys on hand
- Ask the user to list every toy: color, size, type (e.g., "7-inch nude/tan dildo", "bullet vibrator, black", "wand massager, purple"). The agent will reference these by name.

#### Custom pricing
- Lingerie pic price (default $77.38)?
- Nude pic price (default $127.38)?
- Lingerie video price + **max length** (default $127.38, 1–2 min)?
- Nude video price + **max length** (default $177.38, 1–2 min)?
- Voice note price (default $47.38)?
- Complex / weird / multi-scene bump (default $227.38 floor)?

Then build `models/{stage_name_lowercase}/WILLS_AND_WONTS.md` with this structure:

```markdown
# {Stage Name} — Wills and Wonts

## Hard Limits (NEVER — agent refuses warmly and suggests an alternative)
- No anal
- No boy/girl
- [other hard limits from questionnaire answers]

## Soft Limits (paid extras, agent can quote)
- [items the user marked SOFT]

## Will Do (standard content — agent can pitch freely)
- [items marked YES]

## Toys on Hand
- [toy 1 with color + size]
- [toy 2 with color + size]

## Custom Pricing
- Voice note: $XX.XX
- Lingerie pic: $XX.XX
- Nude pic: $XX.XX
- Lingerie video: $XX.XX (max length: 1–2 min)
- Nude video: $XX.XX (max length: 1–2 min)
- Complex / multi-scene: $XX.XX floor

## Persona Notes
- [favorite emoji or signature phrase]
- [things she says about herself the agent should stay consistent with]
```

The agent's system prompt includes the full hard/soft/will-do list so it checks every custom request against these boundaries before quoting. Without this step, the agent will accept anything — that's a refund risk and a fan-safety risk.

---

## Step 2: Account Creation

Present this table. Ask which accounts they already have.

| # | Account | Purpose | Cost | Required? |
|---|---------|---------|------|-----------|
| 1 | Google Cloud Platform | VM hosting | ~$25/mo (e2-medium) | YES (you're on it) |
| 2 | Supabase | Database + pgvector RAG | Free tier | YES |
| 3 | OpenRouter | LLM API (Opus 4.7 + Grok) | **$50 credits to start** | YES |
| 4 | Claude Pro/Max | To run Claude Code | $20+/mo | YES (you already have it) |
| 5 | Telegram | Admin bot interface | Free (via @BotFather) | YES |
| 6 | Domain name | SSL + webhook endpoints | ~$10/yr | YES |
| 7 | Fanvue Developer | OAuth app + webhooks | Free (manager account) | If using Fanvue |
| 8 | OnlyFansAPI.com | OF API access | Their pricing | If using OnlyFans |
| 9 | Sentry | Error tracking | Free tier | OPTIONAL |

**About OpenRouter credits:** Every fan message consumes one Opus 4.7 call (plus optional Grok tool calls if the agent intensifies the message). A $50 credit load typically covers thousands of messages, but burn rate depends on your traffic. Watch `https://openrouter.ai/activity` to see usage in real time. Top up before the balance gets low — if credits hit zero, the bot goes silent.

Tell them: "Let's go through each one. I'll walk you through creating each account and copying the credentials."

---

## Step 3: Credential Collection

Walk through each account ONE AT A TIME. After each one, write the values to `.env` and update `SETUP_PROGRESS.md`.

### 3a. Supabase

1. Tell user: "Go to https://supabase.com and click 'Start your project' (green button). Sign up with GitHub or email."
2. Tell user: "Once in the dashboard, click 'New Project'."
3. Tell user: "Fill in:"
   - **Project name**: anything (e.g., "massi-bot")
   - **Database password**: generate a strong one and save it locally
   - **Region**: closest to your GCP VM
   - Click **Create new project** and wait ~30 seconds.
4. Tell user: "Go to **Settings** (gear icon) → **API**. You'll see:"
   - **Project URL**: `https://abcdefg.supabase.co`
   - **service_role key** (under "Project API keys"): click the eye icon to reveal; starts with `eyJ...` or `sb_secret_...`
   - **anon/public key**: starts with `eyJ...` or `sb_publishable_...`
5. Ask for each value one at a time. Write to `.env`: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_PUBLISHABLE_KEY`.
6. **DEPLOY THE DATABASE SCHEMA** — see Step 3a.1 below.

### 3a.1 Deploy the Full Schema (Paste Migrations One At A Time)

The schema is split across 10 migration files in `migrations/`. Walk the user through pasting each one, in order, into the Supabase SQL Editor. Do NOT concatenate them into one blob — paste them individually so if one fails, it's clear which one.

Order (required):

```
migrations/000_full_schema.sql
migrations/001_model_profile_columns.sql
migrations/002_of_media_id.sql
migrations/003_memory_context_upgrade.sql
migrations/004_system_audit_fixes.sql
migrations/005_memory_cleanup_and_index.sql
migrations/006_ebbinghaus_forgetting.sql
migrations/006_high_value_utterances.sql
migrations/007_template_rewards.sql
migrations/008_bge_m3_embeddings.sql
```

For each migration file, do this exact sequence:

1. Read the file contents using the Read tool.
2. Tell user: "Go to Supabase Dashboard → **SQL Editor** → **New query**."
3. Tell user: "Paste this SQL and click **Run** (or Ctrl+Enter):"
4. Output the full SQL content.
5. Wait for the user to confirm it ran successfully.
6. If they report an error, read the error message carefully — if it's `relation ... already exists`, that's fine, skip to the next. Otherwise, diagnose before proceeding.
7. After all 10 are applied, verify by querying `SELECT table_name FROM information_schema.tables WHERE table_schema='public';` — should show at least: `subscribers`, `content_catalog`, `subscriber_memory`, `persona_memory`, `template_rewards`, `transactions`, `models`.

Log each migration to `docs/setup_log.md` with a timestamp. Update `SETUP_PROGRESS.md`.

### 3b. OpenRouter

1. Tell user: "Go to https://openrouter.ai and sign up (Google or email)."
2. Tell user: "Click your profile icon → **API Keys** → **Create Key**. Name it 'massi-bot'. Copy the key (`sk-or-...`). **You won't be able to see it again**, so save it."
3. Ask for: API Key. Write to `.env`: `OPENROUTER_API_KEY`.
4. Tell user: "Now click your profile icon → **Credits** → **Add Credits** → add **$50**."
5. Wait for confirmation that credits are loaded.
6. Log + update checklist.

### 3c. Telegram Bot

1. Tell user: "Install Telegram if needed (https://telegram.org)."
2. Tell user: "Search for **@BotFather** (blue check). Send `/newbot`."
3. Tell user: "Enter a display name (anything). Then a username ending in `bot` (must be unique)."
4. Tell user: "BotFather replies with your bot token (`1234567890:ABCDef...`). Copy the whole thing."
5. Ask for: Bot Token. Write to `.env`: `TELEGRAM_BOT_TOKEN`.
6. Tell user: "Now search for **@userinfobot** and send `/start`. It replies with your user ID (a long number). Copy it."
7. Ask for: User ID. Write to `.env`: `TELEGRAM_ADMIN_IDS`.
8. Tell user: "Search for your new bot, open the chat, send `/start`. It won't reply yet — that's normal. We just need to initialize the chat."
9. Update checklist.

### 3d. Domain + DNS

1. Ask: "Do you already own a domain, or need to buy one?"
2. If buying: walk them through Namecheap (search for a `.com`, add to cart, checkout, dashboard → Domain List → Manage → Advanced DNS).
3. Guide them to add an **A Record**:
   - Host: `api`
   - Value: (run `curl -s ifconfig.me` to get the VM's public IP)
   - TTL: Automatic
4. Ask: "What's your domain?" (e.g., `yourbrand.com`). Full webhook domain = `api.yourbrand.com`.
5. Write to `.env`: `DOMAIN=api.yourbrand.com`.
6. Test: `dig +short api.yourbrand.com` — should return the VM IP.
7. If not resolving, wait 30s and retry. DNS usually propagates in 2–5 min.

### 3e. Fanvue (if using Fanvue)

1. Tell user: "Go to https://fanvue.com/developers. Log in with your **manager account** (not the model's account)."
2. Tell user: "Click **Register a new app**."
3. Tell user: "Fill in:"
   - App name: anything
   - Redirect URI: `https://{DOMAIN}/oauth/callback` (use actual domain)
   - Scopes: check ALL of them
4. Copy these four values:
   - Client ID, Client Secret, App ID, Webhook Secret
5. Write to `.env`: `FANVUE_CLIENT_ID`, `FANVUE_CLIENT_SECRET`, `FANVUE_APP_ID`, `FANVUE_WEBHOOK_SECRET`.
6. Tell them webhooks and OAuth will be registered after infra is up.

### 3f. OnlyFans (if using OnlyFans)

1. Tell user: "Go to https://app.onlyfansapi.com, create an account, link your OF manager account."
2. From the dashboard, copy:
   - API Key (`ofapi_...`), Account ID (`acct_...`), Webhook Secret
3. Write to `.env`: `OFAPI_KEY`, `OFAPI_ACCOUNT_ID`, `OFAPI_WEBHOOK_SECRET`.

### 3g. Sentry (optional)

Ask if they want error tracking. If yes, walk them through https://sentry.io → Create Project (Python) → copy the DSN → write `SENTRY_DSN` to `.env`.

---

## Step 4: Infrastructure Deployment

### 4a. Install System Dependencies

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 nginx certbot python3-certbot-nginx python3-pip python3-venv
sudo usermod -aG docker $USER
newgrp docker
```

Verify: `docker --version` and `docker compose version`.

### 4b. Install Python Dependencies

```bash
cd ~/massi-bot
python3 -m pip install -r requirements.txt
```

Takes a few minutes — downloads the BGE-M3 embedding model (~570MB).

### 4c. Configure Nginx

```bash
DOMAIN=$(grep "^DOMAIN=" .env | cut -d= -f2)
sudo cp config/nginx.conf.template /etc/nginx/sites-available/massi-bot
sudo sed -i "s/{{DOMAIN}}/$DOMAIN/g" /etc/nginx/sites-available/massi-bot
sudo ln -sf /etc/nginx/sites-available/massi-bot /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### 4d. SSL

```bash
sudo certbot --nginx -d $DOMAIN
```

If it fails: verify ports 80/443 are open in GCP firewall, DNS resolves (`dig +short $DOMAIN`).

### 4e. Build + Start Services

Based on `PLATFORM_MODE`:

- Fanvue only: `docker compose up -d --build redis fanvue admin_bot`
- OnlyFans only: `docker compose up -d --build redis of admin_bot`
- Both: `docker compose up -d --build`

Verify: `docker compose ps` — all Up. If any fail: `docker compose logs {service} --tail=30`.

### 4f. Register Webhooks

**Fanvue** (https://fanvue.com/developers → your app → Webhooks):
- URL: `https://{DOMAIN}/webhook/fanvue/` (trailing slash required)
- Events: `message-received`, `message-read`, `new-follower`, `new-subscriber`, `purchase-received`, `tip-received`

**OnlyFans** (https://app.onlyfansapi.com → account settings → Webhooks):
- URL: `https://{DOMAIN}/webhook/of`
- Events: `messages.received`, `messages.ppv.unlocked`, `subscriptions.new`, `subscriptions.renewed`, `tips.received`

### 4g. Fanvue OAuth Authorization

Tell user: "Open `https://{DOMAIN}/oauth/start` in your browser. Log in with the **manager account**, click Authorize."

Verify: `docker compose exec redis redis-cli keys "fanvue:tokens:*"` — should return a key.

### 4h. Test Everything

1. `bash setup/test_webhooks.sh` — should return 401/403 (HMAC verification working).
2. On Telegram, send `/start` and `/stats` to your bot. Both should respond.
3. `docker compose logs --tail=30` — no errors.

---

## Step 5: Model Profile

Ask one at a time:
- **Stage name** (on-platform display name)
- **Personality** (short description, e.g., "flirty and playful")
- **Speaking style** (e.g., "casual, no caps, light emoji")
- **Location** (e.g., "Miami", "LA")
- **Age** (number)

Build the SQL yourself — substitute their answers (escape apostrophes by doubling: `O''Brien`). Include the tier prices from Step 1c.

Example (fill in with their answers):

```sql
INSERT INTO models (id, stage_name, profile_json, onboarding_complete)
VALUES (
  gen_random_uuid(),
  'Jessica',
  '{"natural_personality": "flirty and playful", "speaking_style": "casual with light emoji", "stated_location": "Miami", "age": 24, "active_tier_count": 6, "tier_prices": {"1": 27.38, "2": 36.56, "3": 77.35, "4": 92.46, "5": 127.45, "6": 200.00}}',
  true
)
RETURNING id;
```

Tell user: "Paste this into Supabase SQL Editor. It returns a UUID. Copy it."

Write the UUID to `.env` as `FANVUE_MODEL_ID` and/or `OF_MODEL_ID`.

`docker compose restart` to pick up the change.

---

## Step 6: Content Ingestion

### 6a. Folder Structure on Platform

```
tier1session1/    — 3-4 images + 1-2 videos (clothed body tease)
tier2session1/    — 3-4 images + 1-2 videos (lingerie / top tease)
tier3session1/    — 3-4 images + 1-2 videos (topless)
tier4session1/    — 3-4 images + 1-2 videos (bottoms off, pussy hidden)  [Full mode only]
tier5session1/    — 3-4 images + 1-2 videos (fully nude, self-play)      [Full mode only]
tier6session1/    — 3-4 images + 1-2 videos (climax with toy)            [Full mode only]
continuation/     — ~20 images (NEVER NSFW — clothed selfies, lifestyle)
```

### 6b. Critical Content Rules

Tell the user:
- **Each tier within a session must look like one continuous moment** — same background, hairstyle, outfit progression. The scene simulates real-time undressing.
- **Continuation content is NEVER NSFW.** Clothed, lifestyle, casual. This is the $20 paywall content.
- **Never show skin or nudity for free** anywhere. Not on Instagram, not on the subscription wall. Scarcity is what makes the $27–$200 per tier pricing work.

### 6c. Upload + Register

Wait for them to confirm uploading is done. Then get media IDs/UUIDs from the platform and register each tier:

```bash
python3 setup/ingest_content.py \
    --model-id "UUID-FROM-STEP-5" \
    --platform fanvue \
    --session 1 --tier 1 \
    --media-uuids "uuid1,uuid2,uuid3,uuid4" \
    --media-type mixed
```

Repeat for every tier and for continuation (session=0, tier=0).

Verify: `python3 setup/ingest_content.py --list --model-id "UUID"` — shows all tiers.

---

## Step 7: Testing the System

This is the critical step that proves the system works end-to-end **before** you point real subscribers at it.

### 7a. Create a Spare Test Account

Tell the user:

> Open a **spare personal Fanvue or OnlyFans account** (one that is NOT your model's account). Any email you own will do — just spin up a second account on the same platform you picked.

### 7b. Send Yourself a Free Subscription Link

Tell the user:

> From your **model's account** (the one the bot is running on), go to the fan outreach / promo tool and generate a free subscription link. Send that link to your spare account. Accept the free sub from the spare.

### 7c. Chat With Your Bot

Tell the user:

> From the spare account, send a message to your model. Wait about 15–30 seconds. The bot will reply — that's the single agent running. You're now chatting with your own bot.

**What to test:**
1. Send a plain "hey" — should get a warm opener
2. Send a few rapport-building messages — bot should ask qualifying questions, eventually ask for consent to spend money
3. Say "yes I'm willing to spend" — bot enters selling mode
4. After a few more messages the bot will send a heads-up ("give me a few minutes") followed 1.5–4 minutes later by the tier 1 PPV drop

### 7d. Simulating PPV Purchases

This is the part operators miss. You're on a free sub — you don't want to actually pay for your own PPVs. Instead:

Tell the user:

> When the bot sends a PPV to your test account, **do NOT purchase it.** Instead, come back to this Claude Code session and tell me: "**simulate PPV purchase for tier X**" (or whatever tier was sent). I will fire the purchase webhook handler directly against your local instance, which advances the subscriber state exactly as a real purchase would — the bot will then generate the post-purchase reaction and (if the tier has a next one) the next tier's heads-up + drop.
>
> This means **Claude Code has to be running during testing** to simulate purchases. Keep this session alive. If you exit, reopen with `claude --resume "<session-id>" --dangerously-skip-permissions`.

When the user says "simulate PPV purchase for tier X", run this from the project root (substitute the real fan UUID from the logs and the real tier amount):

```bash
curl -X POST "http://localhost:8000/test/purchase" \
  -H "Content-Type: application/json" \
  -d '{"fan_uuid": "<fan_uuid>", "amount": 27.38, "tier": 1}'
```

For OnlyFans, use port 8001. Watch `docker compose logs fanvue --tail=50` (or `of`) to see the orchestrator process the purchase, generate the reaction, and queue the next tier drop.

### 7e. What Success Looks Like

- You can chat with the bot and it feels like talking to a real person
- After a few messages, the bot asks for consent
- After consent, the bot eventually drops tier 1
- After simulating tier 1 purchase, the bot reacts and eventually drops tier 2
- Repeat up through tier 6 (or tier 3 in tease mode)
- If you send a custom request ("can you do a video in a schoolgirl outfit"), the bot quotes you the price from your WILLS_AND_WONTS.md
- If you say "I paid" after a custom quote, the bot fires a Telegram alert to your admin chat asking you to confirm or deny

If any of these don't happen, ask Claude Code (me) to investigate the logs.

---

## Step 8: Go Live

1. `/readiness` on Telegram — confirm all tiers have content
2. `/resume` on Telegram — unpause the engine
3. Tell the user: "You're live. Real subscribers who message the model's account will now be handled by the single agent automatically. Monitor via `/stats` and `/revenue`."

---

## Operating Notes

### Single-Agent Architecture (vs. the old multi-agent system)

- **One Opus 4.7 call per fan message**, with optional tool use.
- Tools the agent can call:
  - `uncensor(text, tier)` — Grok intensifies explicit register when Opus self-censors
  - `classify_custom_request(text)` — returns type + price from WILLS_AND_WONTS pricing
  - `fire_custom_payment_alert(reason)` — Telegram alert to admin for payment verification
  - `get_specific_memories(query)` — RAG memory retrieval
- Code-level post-processing: 8 parallel guardrails (Cresta pattern, zero added latency), deterministic text filters, PPV heads-up + Cobalt-Strike jitter injection, bandit outcome recording.
- No multi-agent pipeline, no LLM-based strategist/validator/director. One brain, many tools.

### GFE-Only Mode

If `CONTENT_MODE=gfe_only`:
- All fans stay in rapport/GFE conversation
- Revenue from $20 continuation paywall every ~30 messages
- Only the `continuation/` folder needs content (20 clothed lifestyle images)
- Selling pipeline is bypassed

### Tease-Only Mode (tiers 1–3)

If `CONTENT_MODE=tease_only`:
- Set `active_tier_count: 3` in the model's `profile_json`
- Only upload content for tiers 1–3
- Agent will drop through tier 3 and then stay in aftercare/retention

---

## Session Management

Tell the user at the end of every session:

- Press **Ctrl+C twice** to exit Claude Code.
- Claude will print a resume command like: `claude --resume "SESSION_ID"`.
- **Copy it to a text file on your local computer** (not the VM).
- To resume: SSH back in, `cd ~/massi-bot`, run:
  ```
  claude --resume "SESSION_ID" --dangerously-skip-permissions
  ```
- **Don't leave Claude Code idle for hours.** GCP SSH sessions disconnect after 30–60 min of inactivity and you lose the Claude session.

---

## When the User Wants to Change Something

1. Read `SETUP_PROGRESS.md`, `docs/setup_log.md`, and the code files they're about to touch.
2. For non-trivial changes, write a research doc under `docs/` FIRST, describing what you plan to change and why, so a resumed session has context.
3. Edit code.
4. Run `pytest tests/ -v` before rebuilding.
5. Rebuild: `docker compose build && docker compose up -d`.

---

## Python Style

- Python 3.11+
- Type hints on function signatures
- `httpx.AsyncClient` for external APIs
- Dataclasses for data structures
- No classes where functions suffice
- f-strings for formatting
- `logging` module, not `print()`
