#!/usr/bin/env python3
"""
Hypixel Skyblock -> Discord rare-drop notifier.

How it works (short version):
  1. A "tailer" thread watches your Minecraft `latest.log`, reading ONLY the
     bytes appended since the last check. No re-reading, no parsing of old
     history -> effectively zero CPU while you play.
  2. Lines that look like a rare-drop chat message are pushed onto a queue.
  3. A separate "worker" thread does all the slow stuff (price lookups,
     Discord HTTP calls) so the tailer never blocks and never stutters.

Nothing is hardcoded: configuration comes from environment variables, loaded
from a local `.env` file (see .env.example).
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv()  # reads .env from the current folder, if present

WEBHOOK_URL       = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
# Auction alerts can go to their own channel. Falls back to the main one.
AUCTION_WEBHOOK   = os.getenv("AUCTION_WEBHOOK_URL", "").strip() or WEBHOOK_URL
AUCTION_ENABLED   = os.getenv("AUCTION_ALERTS", "true").strip().lower() in ("1", "true", "yes")
# Collecting a full mailbox fires many lines at once - gather them into
# one embed instead of spamming a webhook post per sale.
AUCTION_BATCH_SEC = float(os.getenv("AUCTION_BATCH_SECONDS", "6"))
LOG_PATH_OVERRIDE = os.getenv("MINECRAFT_LOG_PATH", "").strip()
PING_USER_ID      = os.getenv("PING_USER_ID", "").strip()       # optional Discord user id
MIN_VALUE         = float(os.getenv("MIN_VALUE_COINS", "0"))    # 0 = notify about everything
# Items with no known price: True = notify anyway (safer - new/unlisted items
# are often the interesting ones), False = stay quiet.
NOTIFY_UNKNOWN    = os.getenv("NOTIFY_UNKNOWN_VALUE", "true").strip().lower() in ("1", "true", "yes")
# Comma-separated item names to never notify about, regardless of value.
IGNORE_ITEMS      = {n.strip().lower() for n in os.getenv("IGNORE_ITEMS", "").split(",") if n.strip()}
POLL_INTERVAL     = float(os.getenv("POLL_INTERVAL_SECONDS", "1.0"))
PRICE_TTL         = float(os.getenv("PRICE_CACHE_SECONDS", "600"))
# Auction-house prices: {id} is replaced by the Skyblock item id. Set empty to disable.
AUCTION_PRICE_URL = os.getenv(
    "AUCTION_PRICE_URL", "https://sky.coflnet.com/api/item/price/{id}/bin").strip()

HYPIXEL_ITEMS_URL  = "https://api.hypixel.net/v2/resources/skyblock/items"
HYPIXEL_BAZAAR_URL = "https://api.hypixel.net/v2/skyblock/bazaar"
HTTP_TIMEOUT = 10  # seconds

# Discord POST resilience: transient network blips and 429/5xx used to drop a
# drop silently. Retry with exponential backoff instead. All tunable via .env.
DISCORD_MAX_ATTEMPTS = int(os.getenv("DISCORD_MAX_ATTEMPTS", "5"))
DISCORD_BACKOFF_BASE = float(os.getenv("DISCORD_BACKOFF_SECONDS", "2"))
DISCORD_BACKOFF_CAP  = float(os.getenv("DISCORD_BACKOFF_CAP_SECONDS", "30"))

USER_AGENT = "hypixel-drop-notifier/1.0"

# --------------------------------------------------------------------------
# Log-file discovery
# --------------------------------------------------------------------------

def candidate_log_paths() -> list[Path]:
    """Common `latest.log` locations for the popular Minecraft launchers."""
    home = Path.home()
    appdata = Path(os.getenv("APPDATA", str(home / "AppData" / "Roaming")))

    fixed = [
        appdata / ".minecraft" / "logs" / "latest.log",                        # vanilla / Forge
        home / ".minecraft" / "logs" / "latest.log",                           # Linux
        home / "Library" / "Application Support" / "minecraft" / "logs" / "latest.log",
    ]

    # Launchers that keep one log per instance/profile.
    globbed = [
        (appdata / "PrismLauncher" / "instances", "*/minecraft/logs/latest.log"),
        (appdata / "PolyMC" / "instances",        "*/minecraft/logs/latest.log"),
        (home / "AppData" / "Roaming" / ".minecraft" / "instances", "*/logs/latest.log"),
        (appdata / "ModrinthApp" / "profiles",    "*/logs/latest.log"),
        (home / ".lunarclient" / "profiles",      "*/logs/latest.log"),
        (home / ".lunarclient" / "offline",       "*/logs/latest.log"),
    ]
    for root, pattern in globbed:
        if root.is_dir():
            fixed.extend(sorted(root.glob(pattern)))

    return fixed


def find_log_file() -> Path:
    """Return the log file to watch, or exit with a helpful message."""
    if LOG_PATH_OVERRIDE:
        p = Path(LOG_PATH_OVERRIDE)
        if not p.exists():
            sys.exit("MINECRAFT_LOG_PATH points at a file that does not exist:\n  %s" % p)
        return p

    existing = [p for p in candidate_log_paths() if p.exists()]
    if not existing:
        sys.exit(
            "Could not find latest.log automatically.\n"
            "Set MINECRAFT_LOG_PATH in your .env file. Checked:\n  "
            + "\n  ".join(str(p) for p in candidate_log_paths())
        )
    # Newest-modified wins, in case several launchers are installed.
    chosen = max(existing, key=lambda p: p.stat().st_mtime)

    # A stale pick means we found the wrong launcher - say so loudly, because
    # otherwise it just looks like the script is broken.
    age_minutes = (time.time() - chosen.stat().st_mtime) / 60
    if age_minutes > 30:
        log("WARNING: %s was last written %.0f minutes ago." % (chosen, age_minutes))
        log("         If Minecraft is running now, this is the wrong log file.")
        log("         Set MINECRAFT_LOG_PATH in .env. Other candidates found:")
        for p in sorted(existing, key=lambda p: -p.stat().st_mtime)[:5]:
            log("           %s  (%.0f min ago)" % (p, (time.time() - p.stat().st_mtime) / 60))

    return chosen


# --------------------------------------------------------------------------
# Chat parsing
# --------------------------------------------------------------------------

# Minecraft colour codes look like "§a" - strip them before matching.
COLOR_CODE_RE = re.compile("§.")

# A log line looks like:
#   [12:34:56] [Render thread/INFO]: [CHAT] RARE DROP! Enchanted Ancient Claw (+20% Magic Find!)
# Some clients omit the "[CHAT]" tag, so it is optional.
CHAT_LINE_RE = re.compile(r"^\[[\d:]+\]\s+\[[^\]]+\]:\s+(?:\[CHAT\]\s*)?(?P<msg>.*)$")

# The drop announcements Skyblock actually prints.
DROP_RE = re.compile(
    r"^(?P<kind>PET DROP|RARE REWARD|(?:CRAZY |VERY |SUPER |INSANE )?RARE DROP)"
    r"!\s*(?P<item>.+)$"
)

# Trailing noise on drop lines: "(+320% Magic Find!)", "(+15% Magic Find)".
TRAILING_MF_RE = re.compile(r"\s*\(\+.*?\)\s*$")
# Decorations Hypixel prefixes onto drop names.
LEADING_DECOR_RE = re.compile("^[✦★✯◆»\\s]+")
# Pet drops arrive as "[Lvl 1] Golden Dragon".
PET_LEVEL_RE = re.compile(r"^\[Lvl \d+\]\s*")
# Stacks arrive wrapped: "(32x Toxic Arrow Poison)".
WRAPPED_ITEM_RE = re.compile(r"^\((.+)\)$")
QUANTITY_RE = re.compile(r"^(\d+)\s*x\s+")
# Auction house events, verified against real logs:
#   You collected 2.5M coins from selling Fierce Crimson Boots to [MVP+] Name in an auction!
#   You collected 1,484,999 coins from selling Mineral Leggings to [VIP] Name in an auction!
#   You purchased Mineral Boots for 1,529,999 coins!
AUCTION_SOLD_RE = re.compile(
    r"^You collected (?P<amount>[\d,.]+\s*[kmb]?) coins from selling "
    r"(?P<item>.+?) to (?:\[[^\]]+\]\s*)?(?P<who>\S+) in an auction!$", re.IGNORECASE)
AUCTION_BOUGHT_RE = re.compile(
    r"^You purchased (?P<item>.+?) for (?P<amount>[\d,.]+\s*[kmb]?) coins!$", re.IGNORECASE)
# Star upgrades trail the item name: "Fierce Crimson Boots ✪✪✪✪✪"
STARS_RE = re.compile(r"[✪➊➋➌➍➎⚝]+\s*$")

# "RARE REWARD! SomePlayer found a Recombobulator 3000 in their Bedrock Chest!"
# is a server-wide broadcast about somebody else - not your loot.
OTHER_PLAYER_RE = re.compile(r"^\S+ found (?:a|an|the)\b.*\bin their\b", re.IGNORECASE)


def extract_chat(line: str):
    """
    Pull the chat message out of a raw log line, or None if it is not chat.

    Clients disagree about how many tags they put in front of the message:
        [12:34:56] [Render thread/INFO]: [CHAT] RARE DROP! ...
        [12:34:56] [Render thread/INFO]: [System] [CHAT] RARE DROP! ...
    So we take everything after the LAST [CHAT] tag.
    """
    m = CHAT_LINE_RE.match(line)
    if not m:
        return None
    msg = COLOR_CODE_RE.sub("", m.group("msg"))
    if "[CHAT]" in msg:
        msg = msg.rsplit("[CHAT]", 1)[1]
    return msg.strip()


def match_drop(chat: str):
    """
    Return (drop_kind, clean_item_name, quantity) if this line is a rare drop.

    Skyblock has two shapes for the item part:
        RARE DROP! Enchanted Ancient Claw (+320% Magic Find!)
        RARE DROP! (32x Toxic Arrow Poison) (+112% Magic Find)
    The second wraps a stack in parentheses and prefixes the count.
    """
    m = DROP_RE.match(chat)
    if not m:
        return None

    item = m.group("item")
    item = TRAILING_MF_RE.sub("", item)
    item = LEADING_DECOR_RE.sub("", item).strip()

    # Server-wide broadcasts of OTHER players' dungeon loot are not your drops.
    if OTHER_PLAYER_RE.match(item):
        return None

    # Unwrap "(32x Toxic Arrow Poison)" -> "32x Toxic Arrow Poison"
    wrapped = WRAPPED_ITEM_RE.match(item)
    if wrapped:
        # Decorations can sit inside the brackets too: "(◆ Bite Rune I)"
        item = LEADING_DECOR_RE.sub("", wrapped.group(1).strip()).strip()

    # Split off a leading stack count.
    quantity = 1
    qty = QUANTITY_RE.match(item)
    if qty:
        quantity = int(qty.group(1))
        item = item[qty.end():].strip()

    if not item:
        return None
    return m.group("kind").title(), item, quantity


def parse_amount(text: str):
    """
    "1,484,999" -> 1484999.0, "30.6M" -> 30600000.0, "1.3k" -> 1300.0.

    Hypixel abbreviates large numbers in chat, so both forms turn up.
    """
    text = text.strip().replace(",", "")
    multiplier = 1.0
    if text and text[-1].lower() in "kmb":
        multiplier = {"k": 1e3, "m": 1e6, "b": 1e9}[text[-1].lower()]
        text = text[:-1].strip()
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def clean_item_name(name: str) -> str:
    """Trim star upgrades and the whitespace left by stripped rarity symbols."""
    return STARS_RE.sub("", name).strip()


def match_auction(chat: str):
    """
    Return (action, item, amount, counterparty) for auction-house activity.

    action is "sold" (coins in) or "bought" (coins out). counterparty is the
    other player for a sale, None for a purchase.
    """
    m = AUCTION_SOLD_RE.match(chat)
    if m:
        amount = parse_amount(m.group("amount"))
        if amount is None:
            return None
        return "sold", clean_item_name(m.group("item")), amount, m.group("who")

    m = AUCTION_BOUGHT_RE.match(chat)
    if m:
        amount = parse_amount(m.group("amount"))
        if amount is None:
            return None
        return "bought", clean_item_name(m.group("item")), amount, None

    return None


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------

class PriceBook:
    """
    Looks up an estimated coin value for an item name.

    Sources, in order:
      1. Hypixel Bazaar (official API, no key needed) - instant-sell price.
      2. Lowest BIN from a community auction API - for auction-only items.

    Both are cached for PRICE_CACHE_SECONDS, so a burst of drops causes at
    most one refresh and one lookup per item.
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._names = {}       # lowercase display name -> item id
        self._bazaar = {}      # item id -> instant-sell price
        self._ah = {}          # item id -> (price, fetched_at)
        self._names_at = 0.0
        self._prices_at = 0.0

    def _get_json(self, url: str):
        r = self._session.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _refresh_names(self) -> None:
        # The item catalogue barely changes; refresh it once per day.
        if self._names and time.time() - self._names_at < 86400:
            return
        try:
            data = self._get_json(HYPIXEL_ITEMS_URL)
            self._names = {
                i["name"].lower(): i["id"]
                for i in data.get("items", [])
                if i.get("name") and i.get("id")
            }
            self._names_at = time.time()
        except Exception as exc:
            log("[price] item catalogue unavailable: %s" % exc)

    def _refresh_prices(self) -> None:
        if time.time() - self._prices_at < PRICE_TTL:
            return
        try:
            data = self._get_json(HYPIXEL_BAZAAR_URL)
            self._bazaar = {
                pid: float(p.get("quick_status", {}).get("sellPrice") or 0.0)
                for pid, p in data.get("products", {}).items()
            }
        except Exception as exc:
            log("[price] bazaar unavailable: %s" % exc)

        self._prices_at = time.time()

    def _auction_price(self, item_id: str):
        """Lowest BIN for one item, cached. Community API - may be unavailable."""
        if not AUCTION_PRICE_URL:
            return None
        cached = self._ah.get(item_id)
        if cached and time.time() - cached[1] < PRICE_TTL:
            return cached[0]
        price = 0.0
        try:
            r = self._session.get(AUCTION_PRICE_URL.format(id=item_id), timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                price = float(data.get("lowest") or data.get("sell") or 0.0)
            elif r.status_code != 400:   # 400 just means "no such item id"
                log("[price] auction API HTTP %s for %s" % (r.status_code, item_id))
        except Exception as exc:
            # Community endpoints go down sometimes - degrade instead of crashing.
            log("[price] auction lookup failed for %s: %s" % (item_id, exc))
        self._ah[item_id] = (price, time.time())
        return price

    def value_of(self, item_name: str):
        """Return (coins, source_label). coins is None when unknown."""
        self._refresh_names()
        self._refresh_prices()

        is_pet = bool(PET_LEVEL_RE.match(item_name))
        name = PET_LEVEL_RE.sub("", item_name).strip()

        # Pets are not in the item catalogue, so also try the conventional id
        # spelling ("Golden Dragon" -> PET_GOLDEN_DRAGON / GOLDEN_DRAGON).
        guess = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
        candidates = []
        catalogued = self._names.get(name.lower())
        if catalogued:
            candidates.append(catalogued)
        if is_pet:
            candidates.append("PET_" + guess)
        candidates.append(guess)

        for item_id in candidates:
            bz = self._bazaar.get(item_id)
            if bz and bz > 0:
                return bz, "Bazaar (instant sell)"

        for item_id in candidates:
            bin_price = self._auction_price(item_id)
            if bin_price and bin_price > 0:
                return bin_price, "Lowest BIN"

        return None, "no price found"


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def format_coins(value: float) -> str:
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if value >= cutoff:
            return "%s%s" % (format(value / cutoff, ",.2f"), suffix)
    return format(value, ",.0f")


def embed_color(value):
    """Green -> gold -> purple -> pink as the drop gets more valuable."""
    if value is None:
        return 0x95A5A6            # grey
    if value >= 50_000_000:
        return 0xE91E63            # pink - jackpot
    if value >= 5_000_000:
        return 0x9B59B6            # purple
    if value >= 500_000:
        return 0xF1C40F            # gold
    return 0x2ECC71                # green


def build_payload(kind: str, item: str, value, source: str, raw: str, quantity: int = 1) -> dict:
    """`value` is the total for the whole stack, not the unit price."""
    now = datetime.now(timezone.utc)
    label = ("%dx %s" % (quantity, item)) if quantity > 1 else item

    if value:
        value_text = "**%s** coins\n*%s*" % (format_coins(value), source)
        if quantity > 1:
            value_text = "**%s** coins\n*%s each, %s*" % (
                format_coins(value), format_coins(value / quantity), source)
    else:
        value_text = "*%s*" % source

    embed = {
        "title": "%s!" % kind,
        "description": "**%s**" % label,
        "color": embed_color(value),
        # Discord renders this in each reader's own timezone, at the embed footer.
        "timestamp": now.isoformat(),
        "fields": [
            {"name": "Item",            "value": label,      "inline": True},
            {"name": "Estimated Value", "value": value_text, "inline": True},
            {"name": "When",            "value": "<t:%d:R>" % int(now.timestamp()), "inline": True},
            {"name": "Chat", "value": "```%s```" % raw[:900], "inline": False},
        ],
        "footer": {"text": "Hypixel Skyblock drop notifier"},
    }

    payload = {"username": "Skyblock Drops", "embeds": [embed]}

    # Only ping for genuinely big drops, so the alert stays meaningful.
    if PING_USER_ID and value and value >= max(MIN_VALUE, 1_000_000):
        payload["content"] = "<@%s>" % PING_USER_ID
        payload["allowed_mentions"] = {"users": [PING_USER_ID]}

    return payload


def build_auction_payload(events) -> dict:
    """
    One embed for a batch of auction events.

    `events` is a list of (action, item, amount, counterparty). Collecting a
    full mailbox produces a dozen lines in one tick, so they are summarised
    together rather than posted one by one.
    """
    now = datetime.now(timezone.utc)
    sold = [e for e in events if e[0] == "sold"]
    bought = [e for e in events if e[0] == "bought"]
    income = sum(e[2] for e in sold)
    spend = sum(e[2] for e in bought)
    net = income - spend

    if sold and not bought:
        title = "Auction sold" if len(sold) == 1 else "%d auctions sold" % len(sold)
    elif bought and not sold:
        title = "Auction bought" if len(bought) == 1 else "%d auctions bought" % len(bought)
    else:
        title = "%d auction sales, %d purchases" % (len(sold), len(bought))

    def lines(items, arrow):
        out = []
        for _, item, amount, who in items[:15]:
            tail = " %s %s" % (arrow, who) if who else ""
            out.append("**%s** - %s%s" % (format_coins(amount), item, tail))
        if len(items) > 15:
            out.append("*...and %d more*" % (len(items) - 15))
        return "\n".join(out)

    fields = []
    if sold:
        fields.append({"name": "Sold (+%s)" % format_coins(income),
                       "value": lines(sold, "->"), "inline": False})
    if bought:
        fields.append({"name": "Bought (-%s)" % format_coins(spend),
                       "value": lines(bought, ""), "inline": False})
    if sold and bought:
        fields.append({"name": "Net", "value": "**%s%s** coins" % (
            "+" if net >= 0 else "-", format_coins(abs(net))), "inline": True})
    fields.append({"name": "When", "value": "<t:%d:R>" % int(now.timestamp()), "inline": True})

    return {
        "username": "Skyblock Auctions",
        "embeds": [{
            "title": title,
            "color": 0x3498DB if net >= 0 else 0xE67E22,
            "timestamp": now.isoformat(),
            "fields": fields,
            "footer": {"text": "Hypixel Skyblock auction notifier"},
        }],
    }


def post_to_discord(session: requests.Session, payload: dict, url: str = "") -> bool:
    """POST to Discord with retries and exponential backoff. Returns True on success.

    Retried, because these are transient:
      * network errors (the "Connection aborted" that used to lose drops),
      * HTTP 429 rate limits (waiting the server-supplied `retry_after`),
      * HTTP 5xx server errors.
    Not retried, because more attempts cannot help:
      * HTTP 4xx other than 429 (bad webhook URL, malformed payload, ...).

    Backoff grows base, base*2, base*4, ... capped at DISCORD_BACKOFF_CAP.
    """
    url = url or WEBHOOK_URL
    if not url:
        log("[discord] no webhook URL configured - cannot post")
        return False

    def backoff(attempt: int) -> float:
        return min(DISCORD_BACKOFF_BASE * (2 ** (attempt - 1)), DISCORD_BACKOFF_CAP)

    for attempt in range(1, DISCORD_MAX_ATTEMPTS + 1):
        wait = None
        try:
            r = session.post(url, json=payload, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            wait = backoff(attempt)
            log("[discord] network error (attempt %d/%d): %s" % (
                attempt, DISCORD_MAX_ATTEMPTS, exc))
        else:
            if r.status_code < 300:
                return True
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", DISCORD_BACKOFF_BASE))
                except Exception:
                    wait = DISCORD_BACKOFF_BASE
                log("[discord] rate limited (attempt %d/%d), waiting %.1fs" % (
                    attempt, DISCORD_MAX_ATTEMPTS, wait))
            elif r.status_code >= 500:
                wait = backoff(attempt)
                log("[discord] server error HTTP %s (attempt %d/%d): %s" % (
                    r.status_code, attempt, DISCORD_MAX_ATTEMPTS, r.text[:200]))
            else:
                log("[discord] HTTP %s (permanent, not retrying): %s" % (
                    r.status_code, r.text[:200]))
                return False

        # Sleep between attempts, but never after the final one.
        if attempt < DISCORD_MAX_ATTEMPTS and wait is not None:
            time.sleep(wait)

    log("[discord] gave up after %d attempts - message dropped" % DISCORD_MAX_ATTEMPTS)
    return False


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------

def use_utf8_output() -> None:
    """Force stdout/stderr to UTF-8 so logging can't die on a fancy item glyph.

    Run via start_hidden.vbs, output is redirected to notifier.log, which on
    Windows defaults to cp1252. A single '✪' star or private-use icon in an
    item name then raised UnicodeEncodeError and killed the whole notifier
    (see the 14:50 crash in notifier.log). Reconfiguring to UTF-8 fixes every
    launch path; log() still guards as a last resort.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # not a reconfigurable stream; log()'s own guard covers it


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line = "[%s] %s\n" % (stamp, msg)
    try:
        sys.stdout.write(line)
    except UnicodeEncodeError:
        # Backstop: never let a stray glyph crash the writer thread, even if
        # stdout somehow stayed on a narrow codec.
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(line.encode(enc, "replace").decode(enc))
    sys.stdout.flush()


def tail_log(path: Path, out: queue.Queue, stop: threading.Event, from_start: bool) -> None:
    """
    Follow the log file. Cheap by design: one read of the newly appended bytes
    per POLL_INTERVAL, and the file is opened read-only so Minecraft is unaffected.
    """
    f = open(path, "r", encoding="utf-8", errors="replace")
    if not from_start:
        f.seek(0, os.SEEK_END)          # ignore everything logged before we started
    last_size = path.stat().st_size
    pending = ""

    log("Watching %s" % path)

    try:
        while not stop.is_set():
            chunk = f.read()
            if chunk:
                pending += chunk
                parts = pending.split("\n")
                pending = parts.pop()   # keep the incomplete last line for next round
                for line in parts:
                    chat = extract_chat(line)
                    if not chat:
                        continue
                    event = None
                    hit = match_drop(chat)
                    if hit:
                        event = ("drop", hit[0], hit[1], hit[2], chat)
                    elif AUCTION_ENABLED:
                        auc = match_auction(chat)
                        if auc:
                            event = ("auction",) + auc

                    if event:
                        try:
                            out.put_nowait(event)
                        except queue.Full:
                            log("Queue full - dropping notification")
                last_size = f.tell()
            else:
                # No new data: check whether Minecraft rotated/replaced the file.
                try:
                    if path.stat().st_size < last_size:
                        log("Log file rotated - reopening")
                        f.close()
                        f = open(path, "r", encoding="utf-8", errors="replace")
                        last_size = 0
                        pending = ""
                except FileNotFoundError:
                    pass  # Minecraft restarting; try again next tick
                stop.wait(POLL_INTERVAL)
    finally:
        f.close()


def assess(prices: PriceBook, item: str, quantity: int):
    """
    Price a drop and decide whether it is worth notifying about.

    Returns (total_value, source, skip_reason). skip_reason is None when the
    drop should be posted. Shared by the live watcher and `--parse`, so the
    dry run always agrees with the real thing.
    """
    if PET_LEVEL_RE.sub("", item).strip().lower() in IGNORE_ITEMS:
        return None, "ignored", "on IGNORE_ITEMS list"

    unit, source = prices.value_of(item)
    total = unit * quantity if unit is not None else None

    if MIN_VALUE > 0:
        if total is None:
            # Unknown price: fall back to the NOTIFY_UNKNOWN_VALUE setting
            # rather than silently swallowing a possibly-great drop.
            if not NOTIFY_UNKNOWN:
                return None, source, "no price and NOTIFY_UNKNOWN_VALUE=false"
        elif total < MIN_VALUE:
            return total, source, "%s coins is below MIN_VALUE_COINS (%s)" % (
                format_coins(total), format_coins(MIN_VALUE))

    return total, source, None


def notifier(inbox: queue.Queue, stop: threading.Event) -> None:
    """All network I/O lives here, off the log-reading path."""
    prices = PriceBook()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    recent = {}   # item -> last notified time (dedupe)

    pending_auctions = []   # batched until the burst goes quiet
    last_auction_at = 0.0

    while not stop.is_set():
        try:
            event = inbox.get(timeout=0.5)
        except queue.Empty:
            event = None

        now = time.time()

        # Flush the auction batch once nothing new has arrived for a moment.
        if pending_auctions and now - last_auction_at >= AUCTION_BATCH_SEC:
            total = sum(e[2] for e in pending_auctions if e[0] == "sold")
            log("Auctions: %d event(s), %s coins in" % (len(pending_auctions), format_coins(total)))
            post_to_discord(session, build_auction_payload(pending_auctions), AUCTION_WEBHOOK)
            pending_auctions = []

        if event is None:
            continue

        if event[0] == "auction":
            _, action, item, amount, who = event
            log("Auction %s: %s for %s coins" % (action, item, format_coins(amount)))
            pending_auctions.append((action, item, amount, who))
            last_auction_at = now
            continue

        _, kind, item, quantity, raw = event

        # Skyblock sometimes echoes the same drop line twice.
        if now - recent.get(item, 0) < 3:
            continue
        recent[item] = now

        value, source, skip = assess(prices, item, quantity)
        label = ("%dx %s" % (quantity, item)) if quantity > 1 else item

        if skip:
            log("Skipped %s - %s" % (label, skip))
            continue

        log("%s: %s ~ %s coins" % (kind, label, format_coins(value) if value else "?"))
        post_to_discord(session, build_payload(kind, item, value, source, raw, quantity))


# --------------------------------------------------------------------------
# Dry-run tester
# --------------------------------------------------------------------------

def run_parse(lines) -> None:
    """
    Push chat lines through the exact same parser + pricer the watcher uses,
    printing every step. Nothing is sent to Discord.

    Accepts a bare chat line ("RARE DROP! ...") or a full log line
    ("[12:34:56] [Render thread/INFO]: [CHAT] RARE DROP! ...").
    """
    prices = PriceBook()

    for raw in lines:
        print("\n" + "-" * 66)
        print("input     : %s" % raw)

        # Accept both full log lines and bare chat text.
        chat = extract_chat(raw) or COLOR_CODE_RE.sub("", raw).strip()
        print("chat      : %s" % chat)

        hit = match_drop(chat)
        if not hit:
            auc = match_auction(chat)
            if auc:
                action, item, amount, who = auc
                print("kind      : auction %s" % action)
                print("item      : %s" % item)
                print("amount    : %s coins" % format_coins(amount))
                if who:
                    print("buyer     : %s" % who)
                print("result    : %s" % (
                    "WOULD POST to the auction webhook (batched)" if AUCTION_ENABLED
                    else "SKIPPED - AUCTION_ALERTS=false"))
                continue
            print("result    : NO MATCH - this line would be ignored")
            continue

        kind, item, quantity = hit
        print("kind      : %s" % kind)
        print("item      : %s" % item)
        print("quantity  : %d" % quantity)

        value, source, skip = assess(prices, item, quantity)
        print("price src : %s" % source)
        print("value     : %s coins%s" % (
            format_coins(value) if value else "unknown",
            (" (%s each)" % format_coins(value / quantity)) if value and quantity > 1 else ""))

        if skip:
            print("result    : SKIPPED - %s" % skip)
        else:
            print("result    : WOULD POST to Discord")

    print("\n" + "-" * 66)
    print("Dry run - nothing was sent.")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    use_utf8_output()   # before anything can print an item name

    ap = argparse.ArgumentParser(description="Notify Discord about Skyblock rare drops.")
    ap.add_argument("--test", action="store_true",
                    help="send one sample embed and exit (checks your webhook)")
    ap.add_argument("--from-start", action="store_true",
                    help="also scan the existing log contents (useful for testing patterns)")
    ap.add_argument("--parse", nargs="+", metavar="LINE",
                    help="dry-run one or more chat lines through the parser and "
                         "pricer, printing what would happen. Sends nothing.")
    ap.add_argument("--test-auction", action="store_true",
                    help="send one sample auction embed to AUCTION_WEBHOOK_URL and exit")
    args = ap.parse_args()

    if args.test_auction:
        if not AUCTION_WEBHOOK:
            sys.exit("AUCTION_WEBHOOK_URL is not set.")
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        sample = [
            ("sold", "Fierce Shadow Assassin Leggings", 2_500_000, "ItsYoloSwag"),
            ("sold", "Sharp Felthorn Reaper", 30_600_000, "StarBFanPage"),
            ("bought", "Mineral Boots", 1_529_999, None),
        ]
        post_to_discord(session, build_auction_payload(sample), AUCTION_WEBHOOK)
        log("Test auction embed sent.")
        return

    if args.parse:
        run_parse(args.parse)
        return

    if not WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK_URL is not set. Copy .env.example to .env and fill it in.")

    if args.test:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        post_to_discord(session, build_payload(
            "Rare Drop", "Enchanted Ancient Claw", 1_250_000,
            "test value", "RARE DROP! Enchanted Ancient Claw (+320% Magic Find!)"))
        log("Test message sent.")
        return

    path = find_log_file()
    inbox = queue.Queue(maxsize=200)
    stop = threading.Event()

    threads = [
        threading.Thread(target=tail_log, args=(path, inbox, stop, args.from_start), daemon=True),
        threading.Thread(target=notifier, args=(inbox, stop), daemon=True),
    ]
    for t in threads:
        t.start()

    log("Running. Press Ctrl+C to stop.")
    try:
        while all(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)
        log("Stopped.")


if __name__ == "__main__":
    main()
