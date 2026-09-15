#!/usr/bin/env python3
"""Generate items.json: every Skyblock item, named the way the notifier matches,
plus its rarity for colour-coded browsing.

Source of truth is the NotEnoughUpdates item repo (the same one the droppopup
mod uses). Each item's `displayname` is cleaned with the EXACT same rules the
notifier applies to a drop chat line - strip Minecraft colour codes, a leading
pet-level tag, and leading decoration glyphs - so a name saved from the GUI to
IGNORE_ITEMS will actually match the drop when it happens. Rarity is read from
the last line of the item's lore.

Run this whenever you want to refresh; it writes items.json next to this file,
which the GUI bundles.

Fallback: if the GitHub download fails, it reads the droppopup mod's cached
index (lower-cased names, no rarity) so a build can still succeed offline. The
index is searched for in the usual launcher folders; set DROPPOPUP_INDEX to the
file's full path to point at it directly.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

ZIP_URL = "https://github.com/NotEnoughUpdates/NotEnoughUpdates-REPO/archive/refs/heads/master.zip"
ENCHANTS_URL = "https://raw.githubusercontent.com/NotEnoughUpdates/NotEnoughUpdates-REPO/master/constants/enchants.json"
CACHE_NAME = "droppopup-neu-index-v2.json"
OUT = Path(__file__).with_name("items.json")


def candidate_cache_paths() -> list[Path]:
    """Where the droppopup mod may keep its index, across common launchers/OSes."""
    home = Path.home()
    appdata = Path(os.getenv("APPDATA", str(home / "AppData" / "Roaming")))
    mac = home / "Library" / "Application Support"
    xdg = Path(os.getenv("XDG_DATA_HOME", str(home / ".local" / "share")))

    fixed = [
        appdata / ".minecraft" / "config" / CACHE_NAME,   # vanilla launcher (Windows)
        home / ".minecraft" / "config" / CACHE_NAME,      # Linux
        mac / "minecraft" / "config" / CACHE_NAME,        # macOS
    ]
    globbed = [
        (appdata / "PrismLauncher" / "instances", "*/minecraft/config/" + CACHE_NAME),
        (xdg / "PrismLauncher" / "instances",     "*/minecraft/config/" + CACHE_NAME),
        (mac / "PrismLauncher" / "instances",     "*/minecraft/config/" + CACHE_NAME),
        (appdata / "PolyMC" / "instances",        "*/minecraft/config/" + CACHE_NAME),
        (appdata / "ModrinthApp" / "profiles",    "*/config/" + CACHE_NAME),
    ]
    for root, pattern in globbed:
        if root.is_dir():
            fixed.extend(sorted(root.glob(pattern)))
    return fixed


def find_cache_index() -> Path | None:
    override = os.getenv("DROPPOPUP_INDEX")
    if override:
        return Path(override)
    existing = [p for p in candidate_cache_paths() if p.is_file()]
    # Newest wins, in case several instances have the mod installed.
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else None

# --- name cleaning: mirror hypixel_drop_notifier.py exactly ----------------
COLOR_CODE_RE    = re.compile("§.")
LEADING_DECOR_RE = re.compile("^[✦★✯◆»\\s]+")
PET_LEVEL_RE     = re.compile(r"^\[Lvl [^\]]*\]\s*")   # NEU writes "[Lvl {LVL}]"

# Rarity keywords, longest/most-specific first so "VERY SPECIAL" beats "SPECIAL".
RARITIES = ["VERY SPECIAL", "SUPREME", "SPECIAL", "DIVINE", "MYTHIC", "LEGENDARY",
            "EPIC", "RARE", "UNCOMMON", "COMMON", "ULTIMATE"]


def clean(display: str) -> str:
    name = COLOR_CODE_RE.sub("", display)
    name = PET_LEVEL_RE.sub("", name)
    name = LEADING_DECOR_RE.sub("", name)
    return name.strip()


def rarity_from_lore(lore) -> str:
    """The rarity is the last lore line and starts with the rarity word."""
    if not isinstance(lore, list):
        return ""
    for raw in reversed(lore):
        line = COLOR_CODE_RE.sub("", str(raw)).strip().upper()
        for r in RARITIES:
            if line.startswith(r):
                return r.title()
    return ""


def from_github() -> dict[str, dict]:
    """Return {clean name: {internal, rarity}}. Raises on network failure."""
    print("Downloading NEU repo (~22 MB) ...")
    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "sb-item-manager/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        blob = resp.read()
    print("Downloaded %.1f MB, parsing ..." % (len(blob) / 1e6))

    items: dict[str, dict] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            n = info.filename
            if info.is_dir() or "/items/" not in n or not n.endswith(".json"):
                continue
            try:
                obj = json.loads(zf.read(info).decode("utf-8"))
            except Exception:
                continue
            disp = obj.get("displayname")
            if not disp:
                continue
            name = clean(disp)
            if not name:
                continue
            internal = obj.get("internalname", "").split(";", 1)[0]
            items.setdefault(name, {"internal": internal,
                                    "rarity": rarity_from_lore(obj.get("lore"))})
    return items


def from_cache(index: Path) -> dict[str, dict]:
    """Fallback: the mod's cached index. Names are lower-cased, no rarity."""
    print("Falling back to cached droppopup index (lower-cased, no rarity): %s" % index)
    data = json.loads(index.read_text(encoding="utf-8"))
    items: dict[str, dict] = {}
    for name, entry in data.items():
        clean_name = clean(name)
        if clean_name:
            items.setdefault(clean_name, {"internal": (entry or {}).get("internal", ""),
                                          "rarity": ""})
    return items


# Enchant book drops appear in chat as "<Name> <Roman>", at these mob-drop levels.
BOOK_LEVELS = [5, 6, 7]
_SMALL_WORDS = {"of", "the", "and", "for"}
_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X"}


def _enchant_display(enchant_id: str) -> str:
    words = enchant_id.lower().split("_")
    return " ".join(w if w in _SMALL_WORDS else w.capitalize() for w in words)


def enchant_books() -> dict[str, dict]:
    print("Fetching enchant list ...")
    req = urllib.request.Request(ENCHANTS_URL, headers={"User-Agent": "sb-item-manager/1.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    ids: set[str] = set()
    for group in data.get("enchants", {}).values():
        for eid in group:
            if not eid.lower().startswith("ultimate_"):
                ids.add(eid.lower())
    books: dict[str, dict] = {}
    for eid in ids:
        name = _enchant_display(eid)
        for lvl in BOOK_LEVELS:
            books["%s %s" % (name, _ROMAN[lvl])] = {"internal": "ENCHANT", "rarity": "Enchant"}
    print("Generated %d enchant book names from %d enchants" % (len(books), len(ids)))
    return books


def main() -> None:
    try:
        items = from_github()
    except Exception as exc:
        print("GitHub download failed (%s)" % exc)
        index = find_cache_index()
        if not index or not index.is_file():
            sys.exit("No cache available either - cannot build items.json\n"
                     "(set DROPPOPUP_INDEX to the full path of %s if you have it)" % CACHE_NAME)
        items = from_cache(index)

    try:
        for name, rec in enchant_books().items():
            items.setdefault(name, rec)
    except Exception as exc:
        print("Skipping enchant books (%s)" % exc)

    records = [{"name": name, "internal": rec["internal"], "rarity": rec["rarity"]}
               for name, rec in sorted(items.items(), key=lambda kv: kv[0].lower())]
    OUT.write_text(json.dumps(records, ensure_ascii=False, indent=0), encoding="utf-8")

    counts: dict[str, int] = {}
    for r in records:
        counts[r["rarity"] or "(none)"] = counts.get(r["rarity"] or "(none)", 0) + 1
    print("Wrote %d items -> %s" % (len(records), OUT))
    print("Rarity breakdown:", dict(sorted(counts.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
