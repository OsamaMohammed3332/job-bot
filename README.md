# Telegram Job Bot

Posts new LinkedIn jobs (Egypt + remote worldwide) to a Telegram channel, DMs
you filtered alerts, and answers `/search` on demand.

~~~
Software Engineer ll - Flutter
🏢 talabat
📍 Dubai, Dubai, United Arab Emirates
🕒 6 minutes ago
📌 via LinkedIn

➡️ Apply Here
~~~

## Setup

1. **Create the bot** — message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token.
2. **Create a channel** — add your bot as an Administrator with **Post Messages**.
   Public channel ID is `@yourchannel`; for a private one, forward a message to
   [@userinfobot](https://t.me/userinfobot) to get the `-100...` id.
3. **Secrets** — Settings → Secrets and variables → Actions:
   - `BOT_TOKEN` — the BotFather token
   - `CHANNEL_ID` — `@yourchannel` or `-100...` (skip for DM-only)
4. **Permissions** — Settings → Actions → General → Workflow permissions →
   **Read and write**. Without this the bot cannot save `state.json` and will
   re-post the same jobs every run.
5. **Run it** — Actions → Job Bot → Run workflow. After that it runs itself
   every 10 minutes, around the clock.
6. **Subscribe** — message your bot `/start`.

## Commands

| Command | What it does |
|---|---|
| `/start` | Subscribe to DM alerts |
| `/stop` | Unsubscribe |
| `/keywords python, flutter` | Only DM jobs matching these words |
| `/keywords clear` | Remove the keyword filter |
| `/locations egypt, remote` | Only DM jobs in these places |
| `/status` | Show your current filters |
| `/search flutter egypt` | Search right now |

On GitHub Actions, commands are answered on the next cron run (up to ~10 min).
In `serve` mode they are answered in about 3 seconds.

## Tuning

Everything lives in **`config.yaml`**, which is commented throughout.

- **Too few jobs?** Loosen or empty `filters.title_must_include`.
- **Too much noise?** Add words to `title_must_not_include`.
- **Different roles?** Edit the `searches` list.
- **Junior only?** Add `exp: [1, 2, 3]` to a search entry.
- **Rate-limited a lot?** You have too many searches. Cut the list to the 6-8
  that matter. This is the single most effective fix.

## Test locally

~~~bash
pip install -r requirements.txt
python bot.py test     # prints jobs to your terminal, sends nothing
~~~

## Always-on mode

GitHub Actions cron is not punctual — free-tier runs get queued and often fire
10-30 minutes late, and GitHub runner IPs get rate-limited by LinkedIn fairly
often. For a real 5-minute cadence and instant command replies, run the same
code on any small VPS:

~~~bash
export BOT_TOKEN="8123..."
export CHANNEL_ID="@yourchannel"
export POLL_SECONDS=300
python bot.py serve
~~~

## Files

| File | Purpose |
|---|---|
| `bot.py` | Entry point — `once`, `serve`, `test` modes |
| `sources.py` | LinkedIn scraper + Remotive/RemoteOK/Arbeitnow APIs |
| `tg.py` | Telegram API client and message formatting |
| `store.py` | Dedup + subscribers, persisted to `state.json` |
| `config.yaml` | **Your settings — edit this one** |
| `.github/workflows/jobs.yml` | The 10-minute cron |

## Troubleshooting

**Nothing posted.** Check the Actions log. If every LinkedIn line says `-> 0`
you are rate-limited — reduce your searches. If jobs are found but nothing
posts, your filters are too strict.

**"chat not found".** The bot is not an admin in the channel, or `CHANNEL_ID`
is wrong. Private channels need the `-100...` form.

**Duplicate posts.** `state.json` is not being committed back — check Workflow
permissions are **Read and write**.

**Workflow stopped.** GitHub disables cron on repos with no activity for 60
days. Push any commit, or hit *Run workflow*.

## Note

Scraping LinkedIn is against their Terms of Service. Enforcement against
read-only guest-page access is rare, but make that call knowingly. The bot also
pulls from three public job APIs, so it stays useful either way.
