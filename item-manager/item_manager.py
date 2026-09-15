#!/usr/bin/env python3
"""Skyblock Item Manager - pick which drops notify you and which are ignored.

A desktop app for the hypixel-drop-notifier. It lists every Skyblock item (from
the bundled items.json, built off the NotEnoughUpdates repo) plus common enchant
book drops, each with its rarity, and lets you flip it between "Notify" and
"Ignore". Saving writes only the IGNORE_ITEMS line in the notifier's .env - your
webhook URLs and other settings are left untouched.

Matching is case-insensitive and uses the notifier's own cleaning, so a name you
ignore here matches the drop when it actually happens.

Run with --selftest (no window) or --guitest (build window then auto-close) to
validate without interacting.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request

NOTIFIER_SCRIPT = "hypixel_drop_notifier.py"
CREATE_NO_WINDOW = 0x08000000  # avoid console flashes when calling powershell/wscript

# Rarity -> (row background tint, chip/accent colour). Dark text stays readable
# on every tint. Order also defines the sort ranking.
RARITY_STYLE = {
    "Common":       ("#f1f3f5", "#6b7280"),
    "Uncommon":     ("#e6f6ea", "#2e7d32"),
    "Rare":         ("#e6eefc", "#1565c0"),
    "Epic":         ("#f2e8fb", "#7b1fa2"),
    "Legendary":    ("#fdf1df", "#e08600"),
    "Mythic":       ("#fde6f1", "#d81b8c"),
    "Divine":       ("#e2f6fb", "#00838f"),
    "Special":      ("#fde9e9", "#c62828"),
    "Very Special": ("#fbe3e0", "#ad1457"),
    "Supreme":      ("#fdeae0", "#d84315"),
    "Ultimate":     ("#eceff1", "#455a64"),
    "Enchant":      ("#eaeefb", "#3949ab"),
    "":             ("#ffffff", "#9aa0a6"),
}
RARITY_RANK = {r: i for i, r in enumerate(RARITY_STYLE)}

# Palette for the app chrome.
BG      = "#f6f7f9"
HEADER  = "#1f2937"
ACCENT  = "#4f46e5"
DANGER  = "#c0392b"
SUCCESS = "#1e874b"
INFO    = "#2563eb"


# --------------------------------------------------------------------------
# Paths, data and .env I/O
# --------------------------------------------------------------------------

def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def notifier_dir() -> str | None:
    """The notifier's folder, found relative to this app (no machine-specific paths).

    Works whether the exe sits in the notifier root, the script runs from
    item-manager/, or the freshly built exe runs from item-manager/dist/.
    """
    folder = app_dir()
    for _ in range(3):
        if os.path.isfile(os.path.join(folder, NOTIFIER_SCRIPT)):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


def find_env() -> str | None:
    cands = [os.path.join(app_dir(), ".env")]
    notifier = notifier_dir()
    if notifier:
        cands.append(os.path.join(notifier, ".env"))
    for cand in cands:
        if os.path.isfile(cand):
            return cand
    return None


def load_items() -> list[dict]:
    with open(resource_path("items.json"), encoding="utf-8") as f:
        return json.load(f)


def read_ignore(env_path: str) -> list[str]:
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("IGNORE_ITEMS="):
                return [x.strip() for x in line.split("=", 1)[1].split(",") if x.strip()]
    return []


def read_env_value(env_path: str, key: str) -> str:
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return ""


DEFAULT_ENV = """\
# hypixel-drop-notifier settings (created by Skyblock Item Manager)
# Paste your Discord webhook below, then use the app to pick ignored items.
DISCORD_WEBHOOK_URL=
AUCTION_WEBHOOK_URL=
AUCTION_ALERTS=true
MINECRAFT_LOG_PATH=
PING_USER_ID=
MIN_VALUE_COINS=500000
NOTIFY_UNKNOWN_VALUE=true
IGNORE_ITEMS=
POLL_INTERVAL_SECONDS=1.0
PRICE_CACHE_SECONDS=600
DISCORD_MAX_ATTEMPTS=5
DISCORD_BACKOFF_SECONDS=2
DISCORD_BACKOFF_CAP_SECONDS=30
"""


def create_env(env_path: str) -> None:
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(DEFAULT_ENV)


def write_env_values(env_path: str, updates: dict[str, str]) -> None:
    """Update or insert simple KEY=VALUE lines, leaving every other line intact."""
    with open(env_path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        for key in list(remaining):
            if stripped.startswith(key + "="):
                lines[i] = "%s=%s" % (key, remaining.pop(key))
                break
    for key, val in remaining.items():
        lines.append("%s=%s" % (key, val))
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_ignore(env_path: str, names: list[str]) -> None:
    with open(env_path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    new_line = "IGNORE_ITEMS=" + ", ".join(names)
    for i, line in enumerate(lines):
        if line.strip().startswith("IGNORE_ITEMS="):
            lines[i] = new_line
            break
    else:
        lines += ["", "# Managed by Skyblock Item Manager.", new_line]
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def restart_notifier(env_path: str) -> str:
    folder = os.path.dirname(env_path)
    vbs = os.path.join(folder, "start_hidden.vbs")
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' "
         "-and $_.CommandLine -like '*hypixel_drop_notifier.py*' } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        capture_output=True, creationflags=CREATE_NO_WINDOW)
    if not os.path.isfile(vbs):
        return "Saved, but start_hidden.vbs was not found - start the notifier yourself."
    subprocess.Popen(["wscript.exe", vbs], cwd=folder, creationflags=CREATE_NO_WINDOW)
    return "Saved and notifier restarted."


def notifier_running() -> bool:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' "
             "-and $_.CommandLine -like '*hypixel_drop_notifier.py*' }).Count"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=15)
        return int((out.stdout or "0").strip() or "0") > 0
    except Exception:
        return False


def send_test(webhook_url: str) -> tuple[bool, str]:
    """POST a sample drop embed to the webhook. Returns (ok, detail)."""
    payload = {
        "username": "Skyblock Drops",
        "embeds": [{
            "title": "✅ Test from Item Manager",
            "description": "Your rare-drop webhook works. This is a manual test, "
                           "not a real drop.",
            "color": 0x2ECC71,
            "fields": [{"name": "Sample item", "value": "Warden Heart", "inline": True},
                       {"name": "Rarity", "value": "Legendary", "inline": True}],
            "footer": {"text": "Skyblock Item Manager"},
        }],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "sb-item-manager/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return True, "Discord accepted it (HTTP %s)." % r.status
    except urllib.error.HTTPError as e:
        return False, "Discord rejected it: HTTP %s\n%s" % (e.code, e.read()[:200].decode("utf-8", "replace"))
    except Exception as e:
        return False, "Could not reach Discord: %s" % e


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui(auto_close: bool = False) -> None:
    import tkinter as tk
    from tkinter import ttk, font as tkfont, filedialog, messagebox

    env_path = find_env()
    root = tk.Tk()
    root.title("Skyblock Item Manager")
    root.geometry("860x680")
    root.minsize(620, 460)
    root.configure(bg=BG)

    if not env_path:
        make_new = messagebox.askyesno(
            "No .env found",
            "No notifier settings (.env) were found next to this app.\n\n"
            "Create a new one here so you can set your own webhook?\n"
            "(Choose No to browse for an existing .env instead.)")
        if make_new:
            # Next to the notifier if we can find it, so "restart notifier" works.
            env_path = os.path.join(notifier_dir() or app_dir(), ".env")
            try:
                create_env(env_path)
            except Exception as exc:
                messagebox.showerror("Could not create .env", str(exc))
                root.destroy()
                return
        else:
            env_path = filedialog.askopenfilename(
                title="Select the notifier .env file",
                filetypes=[("env file", "*.env* .env"), ("All files", "*.*")])
            if not env_path:
                messagebox.showerror("No file", "No .env selected - exiting.")
                root.destroy()
                return

    # ---- fonts + ttk styling ----
    base_font   = tkfont.nametofont("TkDefaultFont"); base_font.configure(family="Segoe UI", size=10)
    strike_font = tkfont.Font(family="Segoe UI", size=10, overstrike=1)
    title_font  = tkfont.Font(family="Segoe UI Semibold", size=16)
    sub_font    = tkfont.Font(family="Segoe UI", size=10)
    chip_font   = tkfont.Font(family="Segoe UI", size=8)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", font=base_font)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG)
    style.configure("Card.TFrame", background="#ffffff")
    style.configure("Treeview", rowheight=24, fieldbackground="#ffffff", background="#ffffff")
    style.configure("Treeview.Heading", font=tkfont.Font(family="Segoe UI Semibold", size=10),
                    padding=4)
    style.map("Treeview", background=[("selected", "#c7d2fe")],
              foreground=[("selected", "#111827")])

    def button_style(name, color):
        style.configure(name, background=color, foreground="white", borderwidth=0,
                        focusthickness=0, padding=(12, 6),
                        font=tkfont.Font(family="Segoe UI Semibold", size=10))
        style.map(name, background=[("active", color), ("pressed", color)],
                  foreground=[("disabled", "#dddddd")])

    button_style("Accent.TButton", ACCENT)
    button_style("Danger.TButton", DANGER)
    button_style("Success.TButton", SUCCESS)
    button_style("Info.TButton", INFO)
    style.configure("Ghost.TButton", background="#e5e7eb", foreground="#111827",
                    borderwidth=0, padding=(10, 6))
    style.map("Ghost.TButton", background=[("active", "#d1d5db")])

    # ---- data ----
    items = load_items()
    rarity_of = {it["name"]: (it.get("rarity") or "") for it in items}
    item_names = [it["name"] for it in items]

    ignored: dict[str, str] = {}
    for name in read_ignore(env_path):
        ignored[name.lower()] = name

    names_by_lower = {n.lower(): n for n in item_names}
    for low, disp in ignored.items():
        names_by_lower.setdefault(low, disp)
    all_names = sorted(names_by_lower.values(), key=str.lower)

    sort_state = {"col": "name", "reverse": False}

    # ---- header ----
    header = tk.Frame(root, bg=HEADER)
    header.pack(fill="x")
    htext = tk.Frame(header, bg=HEADER)
    htext.pack(side="left", padx=16, pady=12)
    tk.Label(htext, text="⚔  Skyblock Item Manager", bg=HEADER, fg="white",
             font=title_font).pack(anchor="w")
    tk.Label(htext, text="Choose which drops ping your Discord",
             bg=HEADER, fg="#c7cad1", font=sub_font).pack(anchor="w")

    status_wrap = tk.Frame(header, bg=HEADER)
    status_wrap.pack(side="right", padx=16)
    status_pill = tk.Label(status_wrap, text="Notifier: checking…", bg="#374151",
                           fg="white", font=sub_font, padx=10, pady=4)
    status_pill.pack()

    def refresh_status():
        def work():
            up = notifier_running()
            def apply():
                status_pill.config(
                    text=("● Notifier running" if up else "○ Notifier stopped"),
                    bg=("#166534" if up else "#4b5563"))
            root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    # ---- controls ----
    controls = ttk.Frame(root, padding=(12, 10))
    controls.pack(fill="x")
    ttk.Label(controls, text="🔎").pack(side="left")
    search_var = tk.StringVar()
    search_entry = ttk.Entry(controls, textvariable=search_var)
    search_entry.pack(side="left", fill="x", expand=True, padx=(6, 10))
    ttk.Label(controls, text="Show:").pack(side="left")
    mode_var = tk.StringVar(value="All")
    ttk.Combobox(controls, textvariable=mode_var, width=12, state="readonly",
                 values=["All", "Notify only", "Ignored only"]).pack(side="left", padx=(4, 0))
    ttk.Button(controls, text="⚙ Settings", style="Ghost.TButton",
               command=lambda: open_settings()).pack(side="left", padx=(8, 0))

    # ---- rarity legend ----
    legend = ttk.Frame(root, padding=(12, 0))
    legend.pack(fill="x")
    ttk.Label(legend, text="Rarity:").pack(side="left", padx=(0, 6))
    for r in ["Common", "Uncommon", "Rare", "Epic", "Legendary", "Mythic",
              "Divine", "Special", "Ultimate", "Enchant"]:
        bg, accent = RARITY_STYLE[r]
        tk.Label(legend, text=" %s " % r, bg=bg, fg=accent, font=chip_font,
                 bd=1, relief="solid").pack(side="left", padx=2, pady=4)

    # ---- table ----
    mid = ttk.Frame(root, padding=(12, 6))
    mid.pack(fill="both", expand=True)
    cols = ("name", "rarity", "status")
    tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
    tree.heading("name", text="Item", command=lambda: set_sort("name"))
    tree.heading("rarity", text="Rarity", command=lambda: set_sort("rarity"))
    tree.heading("status", text="Status", command=lambda: set_sort("status"))
    tree.column("name", width=520, anchor="w")
    tree.column("rarity", width=130, anchor="w")
    tree.column("status", width=110, anchor="center")
    for r, (bg, _accent) in RARITY_STYLE.items():
        tree.tag_configure("rar_%s" % r, background=bg)
    tree.tag_configure("ignored", foreground=DANGER, font=strike_font)
    tree.tag_configure("notify", foreground="#111827")
    vsb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    # ---- rendering ----
    def is_ignored(name: str) -> bool:
        return name.lower() in ignored

    def sort_rows(rows):
        col, rev = sort_state["col"], sort_state["reverse"]
        if col == "rarity":
            key = lambda t: (RARITY_RANK.get(t[1], 99), t[0].lower())
        elif col == "status":
            key = lambda t: (0 if t[2] else 1, t[0].lower())
        else:
            key = lambda t: t[0].lower()
        rows.sort(key=key, reverse=rev)
        return rows

    def refresh(*_):
        q = search_var.get().strip().lower()
        m = mode_var.get()
        tree.delete(*tree.get_children())
        rows = []
        for name in all_names:
            ign = is_ignored(name)
            if m == "Ignored only" and not ign:
                continue
            if m == "Notify only" and ign:
                continue
            if q and q not in name.lower():
                continue
            rows.append((name, rarity_of.get(name, ""), ign))
        sort_rows(rows)
        for name, rarity, ign in rows:
            tree.insert("", "end", iid=name,
                        values=(name, rarity or "—", "Ignored" if ign else "Notify"),
                        tags=("rar_%s" % rarity, "ignored" if ign else "notify"))
        count_lbl.config(text="  %d ignored   ·   %d shown   ·   %d total"
                              % (len(ignored), len(rows), len(all_names)))

    def set_sort(col):
        if sort_state["col"] == col:
            sort_state["reverse"] = not sort_state["reverse"]
        else:
            sort_state.update(col=col, reverse=False)
        refresh()

    _debounce = {"id": None}

    def on_search(*_):
        if _debounce["id"]:
            root.after_cancel(_debounce["id"])
        _debounce["id"] = root.after(120, refresh)

    def set_state(names, ign):
        for name in names:
            low = name.lower()
            if ign:
                ignored[low] = names_by_lower.get(low, name)
            else:
                ignored.pop(low, None)
            if tree.exists(name):
                tree.item(name, values=(name, rarity_of.get(name, "") or "—",
                                        "Ignored" if ign else "Notify"),
                          tags=("rar_%s" % rarity_of.get(name, ""),
                                "ignored" if ign else "notify"))
        count_lbl.config(text="  %d ignored   ·   %d total" % (len(ignored), len(all_names)))
        if mode_var.get() != "All":
            refresh()

    def toggle_selected(*_):
        sel = tree.selection()
        if sel:
            set_state(sel, not is_ignored(sel[0]))

    def add_custom():
        name = custom_var.get().strip()
        if not name:
            return
        low = name.lower()
        if low not in names_by_lower:
            names_by_lower[low] = name
            all_names.append(name)
            all_names.sort(key=str.lower)
            rarity_of.setdefault(name, "")
        ignored[low] = names_by_lower[low]
        custom_var.set("")
        refresh()
        if tree.exists(names_by_lower[low]):
            tree.see(names_by_lower[low])
            tree.selection_set(names_by_lower[low])

    def do_test():
        url = read_env_value(env_path, "DISCORD_WEBHOOK_URL")
        if not url:
            messagebox.showerror("No webhook", "DISCORD_WEBHOOK_URL is not set in .env.")
            return
        test_btn.config(state="disabled")
        count_lbl.config(text="  Sending test to Discord…")

        def work():
            ok, detail = send_test(url)
            def done():
                test_btn.config(state="normal")
                (messagebox.showinfo if ok else messagebox.showerror)(
                    "Webhook test", ("✅ " if ok else "⚠ ") + detail)
                refresh()
            root.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def open_settings():
        nonlocal env_path
        dlg = tk.Toplevel(root)
        dlg.title("Settings")
        dlg.configure(bg=BG)
        dlg.transient(root)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)

        tk.Label(frm, text="Notifier settings", bg=BG, fg="#111827",
                 font=tkfont.Font(family="Segoe UI Semibold", size=13)).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(frm, text="These are saved into %s" % env_path, bg=BG, fg="#8a8f98",
                 font=chip_font).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        fields = [
            ("Rare-drop webhook URL", "DISCORD_WEBHOOK_URL", 58),
            ("Auction webhook URL (optional)", "AUCTION_WEBHOOK_URL", 58),
            ("Ping Discord user ID (optional)", "PING_USER_ID", 30),
            ("Min value to notify (coins)", "MIN_VALUE_COINS", 16),
        ]
        entries = {}
        first_entry = None
        for i, (label, key, width) in enumerate(fields):
            ttk.Label(frm, text=label).grid(row=i + 2, column=0, sticky="w", pady=5, padx=(0, 10))
            var = tk.StringVar(value=read_env_value(env_path, key))
            ent = ttk.Entry(frm, textvariable=var, width=width)
            ent.grid(row=i + 2, column=1, sticky="we", pady=5)
            entries[key] = var
            if first_entry is None:
                first_entry = ent
        frm.columnconfigure(1, weight=1)

        tk.Label(frm, text="Tip: in Discord → Channel → Edit → Integrations → Webhooks → "
                           "New Webhook → Copy URL.", bg=BG, fg="#8a8f98",
                 font=chip_font, wraplength=520, justify="left").grid(
            row=len(fields) + 2, column=0, columnspan=2, sticky="w", pady=(8, 4))

        btns = ttk.Frame(frm)
        btns.grid(row=len(fields) + 3, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def test_entered():
            url = entries["DISCORD_WEBHOOK_URL"].get().strip()
            if not url:
                messagebox.showerror("No webhook", "Enter a webhook URL first.", parent=dlg)
                return
            test_this.config(state="disabled")

            def work():
                ok, detail = send_test(url)
                root.after(0, lambda: (
                    test_this.config(state="normal"),
                    (messagebox.showinfo if ok else messagebox.showerror)(
                        "Webhook test", ("✅ " if ok else "⚠ ") + detail, parent=dlg)))
            threading.Thread(target=work, daemon=True).start()

        def save_settings():
            updates = {k: v.get().strip() for k, v in entries.items()}
            wh = updates["DISCORD_WEBHOOK_URL"]
            if wh and not wh.startswith("https://"):
                if not messagebox.askyesno(
                        "Unusual URL",
                        "That webhook doesn't start with https://. Save anyway?", parent=dlg):
                    return
            mv = updates["MIN_VALUE_COINS"]
            if mv and not mv.replace(".", "", 1).isdigit():
                messagebox.showerror("Invalid number",
                                     "Min value must be a number (coins).", parent=dlg)
                return
            try:
                write_env_values(env_path, updates)
            except Exception as exc:
                messagebox.showerror("Save failed", str(exc), parent=dlg)
                return
            messagebox.showinfo(
                "Saved", "Settings saved.\n\nUse \"Save & restart notifier\" (or restart it) "
                         "for changes to take effect.", parent=dlg)
            dlg.destroy()

        test_this = ttk.Button(btns, text="🔔 Test this URL", style="Info.TButton",
                               command=test_entered)
        test_this.pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Save", style="Accent.TButton",
                   command=save_settings).pack(side="left")
        ttk.Button(btns, text="Cancel", style="Ghost.TButton",
                   command=dlg.destroy).pack(side="left", padx=(8, 0))

        dlg.update_idletasks()
        try:
            dlg.grab_set()
        except tk.TclError:
            pass  # window not viewable (e.g. headless test) - not fatal
        if first_entry is not None:
            first_entry.focus_set()

    def do_save(restart):
        names = sorted(ignored.values(), key=str.lower)
        try:
            write_ignore(env_path, names)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        msg = "Saved %d ignored item(s)." % len(names)
        if restart:
            msg = restart_notifier(env_path) + "\n\n" + msg
            refresh_status()
        else:
            msg += "\n\nRestart the notifier for changes to take effect."
        messagebox.showinfo("Saved", msg)

    # ---- selection actions ----
    acts = ttk.Frame(root, padding=(12, 4))
    acts.pack(fill="x")
    ttk.Button(acts, text="Ignore selected", style="Danger.TButton",
               command=lambda: set_state(tree.selection(), True)).pack(side="left")
    ttk.Button(acts, text="Notify selected", style="Success.TButton",
               command=lambda: set_state(tree.selection(), False)).pack(side="left", padx=6)
    ttk.Button(acts, text="Toggle (Space)", style="Ghost.TButton",
               command=toggle_selected).pack(side="left")
    ttk.Label(acts, text="   Add exact drop name:").pack(side="left")
    custom_var = tk.StringVar()
    custom_entry = ttk.Entry(acts, textvariable=custom_var, width=24)
    custom_entry.pack(side="left", padx=(4, 4))
    ttk.Button(acts, text="Add", style="Ghost.TButton", command=add_custom).pack(side="left")

    # ---- bottom bar ----
    bar = tk.Frame(root, bg="#eef0f4")
    bar.pack(fill="x")
    inner = tk.Frame(bar, bg="#eef0f4")
    inner.pack(fill="x", padx=12, pady=8)
    count_lbl = tk.Label(inner, text="", bg="#eef0f4", fg="#374151", font=sub_font)
    count_lbl.pack(side="left")
    ttk.Button(inner, text="Save & restart notifier", style="Accent.TButton",
               command=lambda: do_save(True)).pack(side="right")
    ttk.Button(inner, text="Save", style="Ghost.TButton",
               command=lambda: do_save(False)).pack(side="right", padx=6)
    test_btn = ttk.Button(inner, text="🔔 Test webhook", style="Info.TButton", command=do_test)
    test_btn.pack(side="right", padx=(0, 12))

    tk.Label(root, text="Editing: %s" % env_path, bg=BG, fg="#8a8f98",
             font=chip_font, anchor="w").pack(fill="x", padx=12, pady=(2, 6))

    # ---- bindings ----
    search_var.trace_add("write", on_search)
    mode_var.trace_add("write", lambda *a: refresh())
    custom_entry.bind("<Return>", lambda e: add_custom())
    tree.bind("<Double-1>", toggle_selected)
    tree.bind("<space>", toggle_selected)

    refresh()
    refresh_status()
    search_entry.focus_set()

    if auto_close:
        rows = len(tree.get_children())
        root.withdraw()
        settings_ok = False
        try:
            open_settings()
            settings_ok = any(isinstance(w, tk.Toplevel) for w in root.winfo_children())
            for w in root.winfo_children():
                if isinstance(w, tk.Toplevel):
                    w.destroy()
        except Exception as exc:
            print("guitest settings ERROR:", exc)
        print("guitest OK: window built, %d rows, settings=%s" % (rows, settings_ok))
        root.after(400, root.destroy)

    root.mainloop()


# --------------------------------------------------------------------------

def selftest() -> int:
    env = find_env()
    print("env_path:", env)
    items = load_items()
    print("items loaded:", len(items))
    with_rarity = sum(1 for it in items if it.get("rarity"))
    print("items with rarity:", with_rarity)
    names = {it["name"].lower() for it in items}
    for q in ("Smite VI", "Warden Heart", "Bite Rune I", "Enchanted Spider Eye"):
        print("  in list:", q.ljust(22), q.lower() in names)
    if env:
        print("current IGNORE_ITEMS:", read_ignore(env))
        print("webhook set:", bool(read_env_value(env, "DISCORD_WEBHOOK_URL")))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    run_gui(auto_close="--guitest" in sys.argv)
