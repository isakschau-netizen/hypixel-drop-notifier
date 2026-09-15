# Hypixel Skyblock -> Discord rare-drop notifier

Watches your Minecraft chat log and posts a Discord embed whenever you get a
rare drop, including an estimated coin value from the Bazaar / auction house.

## 1. Prerequisites

* Python 3.9 or newer (`python --version`)
* Two libraries:

```bash
pip install requests python-dotenv
```

(or `pip install -r requirements.txt`)

On Windows, if `pip` or `python` is "not recognized", Python was installed
without being added to PATH — use the `py` launcher instead:
`py -m pip install -r requirements.txt`. `run.bat` and `start_hidden.vbs` fall
back to `py` automatically.

Nothing else is needed — no Hypixel API key. The endpoints used
(`api.hypixel.net/v2/skyblock/bazaar` and the item catalogue) are public.

## 2. Get your Discord webhook URL

1. Open Discord, go to the server where you want the alerts.
2. Right-click the target channel -> **Edit Channel**.
3. **Integrations** -> **Webhooks** -> **New Webhook**.
4. Give it a name (e.g. "Skyblock Drops"), pick the channel, then
   **Copy Webhook URL**.

You need **Manage Webhooks** permission on that server. Treat the URL like a
password: anyone who has it can post to your channel.

## 3. Configure

```bash
copy .env.example .env       # Windows
# cp .env.example .env       # macOS/Linux
```

Then open `.env` and paste your webhook URL into `DISCORD_WEBHOOK_URL`.

| Variable | Meaning |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | **Required.** Where alerts are posted. |
| `MINECRAFT_LOG_PATH` | Force a specific `latest.log`. Empty = auto-detect. |
| `PING_USER_ID` | Your Discord user id; pings you on drops worth 1M+. |
| `MIN_VALUE_COINS` | Only notify at/above this value. `0` = everything (spammy). Default `500000`. |
| `NOTIFY_UNKNOWN_VALUE` | `true` = still notify when no price is found. |
| `IGNORE_ITEMS` | Comma-separated names to never notify about. |
| `POLL_INTERVAL_SECONDS` | How often to check the log. Default `1.0`. |
| `PRICE_CACHE_SECONDS` | Price cache lifetime. Default `600`. |
| `AUCTION_PRICE_URL` | Auction-house price API. Empty = Bazaar prices only. |

`.env` is listed in `.gitignore`, so the secret never lands in git.

To get your Discord user id: Settings -> Advanced -> enable **Developer Mode**,
then right-click your name -> **Copy User ID**.

## 4. Run it

Test the webhook first — this sends one sample embed and exits:

```bash
python hypixel_drop_notifier.py --test
```

Then start the watcher (leave the window open while you play):

```bash
python hypixel_drop_notifier.py
```

Order does not matter — start it before or after Minecraft. It seeks to the end
of the log on startup, so old drops are not re-announced, and it reopens the
file automatically when Minecraft restarts and rotates `latest.log`.

On Windows you can double-click `run.bat` instead.

Stop with `Ctrl+C`.

### Running it without a console window

The script has to be running for alerts to arrive — but the window does not
have to be visible. Minimising it is fine; it uses no meaningful CPU.

To run it with no window at all, double-click **`start_hidden.vbs`**. It starts
the notifier in the background and appends everything it would have printed to
`notifier.log`, so you can still check what it caught.

To stop a hidden instance, double-click **`stop.bat`** (it only kills the
notifier's own Python process, nothing else).

To start it automatically with Windows: press `Win+R`, type `shell:startup`,
and drop a shortcut to `start_hidden.vbs` in that folder.

Changing `.env` requires a restart — settings are read once at startup.

### Testing without playing: `--parse`

Push any chat line through the real parser and pricer and see exactly what
would happen. Nothing is sent to Discord:

```bash
python hypixel_drop_notifier.py --parse "RARE DROP! (32x Toxic Arrow Poison) (+112% Magic Find)"
```

```
chat      : RARE DROP! (32x Toxic Arrow Poison) (+112% Magic Find)
kind      : Rare Drop
item      : Toxic Arrow Poison
quantity  : 32
price src : Bazaar (instant sell)
value     : 53.48k coins (1.67k each)
result    : WOULD POST to Discord
```

Pass several lines at once (space-separated, each quoted). Full log lines
(`[12:34:56] [Render thread/INFO]: [CHAT] ...`) work too, so you can paste
straight out of `latest.log`. `--parse` shares its logic with the live
watcher, so if it says WOULD POST, the real run posts.

### Debugging the chat patterns

```bash
python hypixel_drop_notifier.py --from-start
```

This also scans the existing log contents, so you can confirm your client's
log format matches.

## 4b. Filtering out junk drops

`RARE DROP!` fires constantly on cheap items, so two filters are on by default:

* **`MIN_VALUE_COINS=500000`** — anything worth less is logged to the console
  but not posted. For reference (live Bazaar instant-sell):
  Enchanted Spider Eye ~1.3k, Enchanted Ancient Claw ~31k, Spider Catalyst
  ~41k, Fly Swatter ~470k. Raise to `5000000` if you only want big hits;
  set `0` to see everything again.
* **`IGNORE_ITEMS`** — a hard blocklist by name, whatever the price.

Items with no price at all (brand-new or unlisted) still notify, because those
are often the interesting ones. Set `NOTIFY_UNKNOWN_VALUE=false` to mute them.

## 4c. Auction alerts

Posts to its own webhook (`AUCTION_WEBHOOK_URL`), so sales do not mix with drop
alerts. Two chat lines are recognised, both verified against real logs:

```
You collected 2.5M coins from selling Fierce Crimson Boots to [MVP+] Name in an auction!
You purchased Mineral Boots for 1,529,999 coins!
```

Amounts arrive both as `1,484,999` and as `30.6M`; both are parsed. Rank
prefixes, and star upgrades on item names, are stripped.

**Batching.** Collecting a full mailbox fires a dozen lines in one tick. Rather
than a webhook post per sale, events are gathered for `AUCTION_BATCH_SECONDS`
(default 6) of quiet and posted as one embed with a per-item list, totals, and
net profit when the batch contains both sales and purchases.

Set `AUCTION_ALERTS=false` to turn this off without touching the drop watcher.

Note that "You collected ..." fires when you *collect* from the mailbox, not at
the moment of sale — so the alert arrives when the coins actually land.

## 5. Which log file?

Auto-detection checks, newest first:

* `%APPDATA%\.minecraft\logs\latest.log` (vanilla / Forge)
* `%USERPROFILE%\.lunarclient\offline\multiver\logs\latest.log` (Lunar)
* the Linux/macOS equivalents

* PrismLauncher / PolyMC instances (`instances/*/minecraft/logs/latest.log`)
* Modrinth App profiles and Lunar profiles

If several are installed, the most recently written one wins, and the script
prints a warning at startup if that file has not been touched for 30 minutes —
that almost always means it picked the wrong launcher. Put the full path in
`MINECRAFT_LOG_PATH` to settle it.

Badlion and other launchers keep their logs elsewhere — find `latest.log` there
and set `MINECRAFT_LOG_PATH`.

**Note:** some clients (notably Badlion) do not write chat to the log by
default. If `--from-start` finds nothing, enable chat logging in your client,
or switch to a launcher that logs chat.

## 6. Why it does not lag your game

* The log is opened **read-only** and only the bytes appended since the last
  check are read — no re-parsing, no full-file scans.
* Reading and matching happen in one thread; all network calls (prices,
  Discord) happen in a **separate** thread fed by a queue, so a slow API never
  blocks the reader.
* Prices are cached for 10 minutes, so a burst of drops causes at most one
  refresh.
* At idle the process does roughly one `stat()` + one empty `read()` per second.

## 7. Detected messages

| Chat line | Example |
| --- | --- |
| `RARE DROP!` | `RARE DROP! Enchanted Ancient Claw (+320% Magic Find!)` |
| `VERY/SUPER/CRAZY/INSANE RARE DROP!` | `CRAZY RARE DROP! Necron's Handle` |
| `PET DROP!` | `PET DROP! [Lvl 1] Golden Dragon (+15% Magic Find!)` |
| `RARE REWARD!` | `RARE REWARD! Wither Blood` |
| stacks | `RARE DROP! (32x Toxic Arrow Poison) (+112% Magic Find)` |

Stacks are unwrapped and the count multiplied into the estimated value.

Server-wide broadcasts about *other* players (`RARE REWARD! SomeGuy found a
Recombobulator 3000 in their Bedrock Chest!`) are filtered out — they are not
your loot.

Both `[CHAT]` and `[System] [CHAT]` log formats are handled.

Adding more is a one-line change to `DROP_RE` near the top of the script.

## 8. Fair-play note

This only reads a log file your own client writes and sends a webhook. It does
not read or inject into the game process, so it is not a mod or a macro — but
Hypixel's rules are the final word, so check them if in doubt.
