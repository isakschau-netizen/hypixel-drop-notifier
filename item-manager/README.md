# Skyblock Item Manager

A desktop app to pick which Skyblock drops **notify** you and which are **ignored**,
for the `hypixel-drop-notifier`. It edits only the `IGNORE_ITEMS` line in the
notifier's `.env` — your webhook URLs and other settings are never touched.

## Using it

Windows only (it uses PowerShell to find and restart the notifier). The notifier
itself also runs on macOS/Linux — edit `IGNORE_ITEMS` in `.env` by hand there.

Run **`Skyblock Item Manager.exe`** (a copy sits in the notifier root folder).

- **Search** to filter the list of every item (8000+), plus common enchant books.
- Each row is **colour-coded by rarity** (Common → Ultimate + Enchant), with a
  legend up top and a sortable **Rarity** column. Click any column header to sort.
- Select one or more rows and use **Ignore selected** / **Notify selected**, or
  **double-click** / press **Space** to toggle. Ignored rows are red + struck out.
- **Show** dropdown filters to All / Notify only / Ignored only.
- **Add exact drop name** — for anything not in the list, type the name exactly as
  it appears in chat and add it to the ignored set.
- **🔔 Test webhook** posts a sample drop to your `DISCORD_WEBHOOK_URL` so you can
  confirm the pipeline still works. The header shows whether the background
  **notifier is running** (● green) or stopped (○ grey).
- **⚙ Settings** lets you edit the webhook URL(s) — rare-drop and auction — plus
  ping user id and min-value, and test a URL before saving. Nothing is hardcoded,
  so anyone can point it at their own webhook. If no `.env` exists next to the app,
  it offers to create a fresh one on first launch.
- **Save** writes the ignore list to `.env`. **Save & restart notifier** also
  bounces the background process so changes apply immediately.

Matching is case-insensitive and uses the notifier's own cleaning (colour codes,
pet-level tags and leading `◆★✦` decorations are stripped), so a name you pick
here matches the real drop.

> Note: "Notify" doesn't guarantee a message — the notifier still skips drops
> below `MIN_VALUE_COINS` (500k by default). "Ignore" always silences an item.

## Refreshing the item list

The item list is baked into the exe from `items.json`. To pull the latest items
from the NotEnoughUpdates repo and rebuild:

```
cd item-manager
python build_items.py          # re-downloads NEU repo -> items.json
python -m PyInstaller --onefile --windowed --name "Skyblock Item Manager" --add-data "items.json;." item_manager.py
```

The new exe lands in `item-manager/dist/`. Copy it over the one in the notifier root.
The app finds the notifier's `.env` from wherever it runs (root, `item-manager/` or
`item-manager/dist/`), so no paths need editing on a new computer.

If the download fails, `build_items.py` falls back to the droppopup mod's cached
index, searched for in the usual Prism / PolyMC / Modrinth / vanilla folders. Set
`DROPPOPUP_INDEX` to the file's full path if yours lives somewhere else.

## Files

- `build_items.py` — downloads + cleans every item name into `items.json`.
- `item_manager.py` — the GUI (`--selftest` / `--guitest` validate without a real window).
- `items.json` — generated data bundled into the exe.
- `dist/Skyblock Item Manager.exe` — the built app.

## First-run note

The exe is unsigned, so Windows SmartScreen may warn on first launch:
**More info → Run anyway**.
