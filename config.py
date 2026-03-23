#!/usr/bin/env python3
"""
mlbconfig.py — MLB Scoreboard Configuration Editor

Usage:
    python3 mlbconfig.py                    # edit config.json in current dir
    python3 mlbconfig.py --config path.json # edit a specific config file
"""

import tkinter as tk
from tkinter import ttk, colorchooser, messagebox
import json
import pathlib
import argparse
import datetime
import threading
import requests
from copy import deepcopy

# ── Defaults ───────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "team_id": 117,
    "teams": {},
    "team_colors": {},
    "polling_intervals": {"live": 30, "pre_game": 60, "scheduled": 300, "none": 3600},
    "lookahead_days": 7,
    "canvas": {
        "width": 1100, "height": 700,
        "bg_color": "#0b162a", "fg_color": "#eaeaea",
        "accent": "#FFD700", "font_family": "Courier"
    },
    "ui": {"max_innings": 9},
    "debug": False
}

ALL_TEAMS = {
    "Arizona Diamondbacks": 109, "Atlanta Braves": 144, "Baltimore Orioles": 110,
    "Boston Red Sox": 111,       "Chicago Cubs": 112,   "Chicago White Sox": 145,
    "Cincinnati Reds": 113,      "Cleveland Guardians": 114, "Colorado Rockies": 115,
    "Detroit Tigers": 116,       "Houston Astros": 117, "Kansas City Royals": 118,
    "Los Angeles Angels": 108,   "Los Angeles Dodgers": 119, "Miami Marlins": 146,
    "Milwaukee Brewers": 158,    "Minnesota Twins": 142, "New York Mets": 121,
    "New York Yankees": 147,     "Philadelphia Phillies": 143, "Pittsburgh Pirates": 134,
    "Sacramento Athletics": 133, "San Diego Padres": 135, "San Francisco Giants": 137,
    "Seattle Mariners": 136,     "St. Louis Cardinals": 138, "Tampa Bay Rays": 139,
    "Texas Rangers": 140,        "Toronto Blue Jays": 141, "Washington Nationals": 120,
}

FONT_FAMILIES = [
    "Courier", "Courier New", "Consolas", "Menlo", "Monaco",
    "Lucida Console", "DejaVu Sans Mono", "Liberation Mono"
]

# ── Theme ──────────────────────────────────────────────────────────────────────
BG      = "#0b162a"
BG2     = "#111f38"
BG3     = "#172540"
ACCENT  = "#FFD700"
FG      = "#eaeaea"
FG_DIM  = "#7a8eaa"
RED     = "#c0392b"
GREEN   = "#27ae60"
BORDER  = "#1e3a5f"
FONT    = "Courier"


# ── Config I/O ─────────────────────────────────────────────────────────────────
def load_config(path):
    cfg = deepcopy(DEFAULT_CONFIG)
    p = pathlib.Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for k, v in data.items():
                if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            messagebox.showerror("Config Error", f"Could not read:\n{e}")
    return cfg


def save_config(path, cfg):
    pathlib.Path(path).write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── Colour helpers ─────────────────────────────────────────────────────────────
def contrast(hex_col):
    try:
        r, g, b = int(hex_col[1:3],16), int(hex_col[3:5],16), int(hex_col[5:7],16)
        return "#000000" if (r*299 + g*587 + b*114)/1000 > 128 else "#ffffff"
    except Exception:
        return "#ffffff"


class ColourPicker(tk.Frame):
    """Colour swatch + hex entry bound to a StringVar."""
    def __init__(self, parent, var, **kw):
        super().__init__(parent, bg=BG2, **kw)
        self.var = var

        self.swatch = tk.Label(self, width=3, text="▐▌", cursor="hand2",
                               font=(FONT, 10), relief="flat",
                               highlightthickness=1, highlightbackground=BORDER)
        self.swatch.pack(side="left", padx=(0, 5))

        self.ent = tk.Entry(self, textvariable=var, width=9, font=(FONT, 10),
                            bg=BG3, fg=FG, insertbackground=ACCENT,
                            relief="flat", bd=4,
                            highlightthickness=1, highlightbackground=BORDER,
                            highlightcolor=ACCENT)
        self.ent.pack(side="left")

        var.trace_add("write", lambda *_: self._refresh())
        self.swatch.bind("<Button-1>", self._pick)
        self.ent.bind("<FocusOut>", lambda *_: self._refresh())
        self._refresh()

    def _refresh(self):
        v = self.var.get()
        try:
            self.swatch.configure(bg=v, fg=contrast(v))
        except Exception:
            self.swatch.configure(bg=BG3, fg=FG)

    def _pick(self, _=None):
        res = colorchooser.askcolor(color=self.var.get(), title="Pick colour")
        if res and res[1]:
            self.var.set(res[1].upper())


# ── Section ────────────────────────────────────────────────────────────────────
class Section(tk.Frame):
    """
    Titled section. All content is placed via .add_row() which uses grid()
    with two fixed columns: col 0 = label (fixed px width), col 1 = widget.
    A third col 2 holds optional dim tip text.

    Because every Section uses the same col 0 pixel width, all labels across
    all sections align visually as long as sections have the same left padding.
    """
    LABEL_PX = 190  # pixel width of the label column

    def __init__(self, parent, title, **kw):
        super().__init__(parent, bg=BG2, **kw)

        # Header bar
        hdr = tk.Frame(self, bg=BG3)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"  {title}", font=(FONT, 10, "bold"),
                 fg=ACCENT, bg=BG3, anchor="w", pady=8).pack(fill="x")

        # Body grid
        self.body = tk.Frame(self, bg=BG2, padx=18, pady=10)
        self.body.pack(fill="x")
        self.body.columnconfigure(0, minsize=self.LABEL_PX)  # fixed label col
        self.body.columnconfigure(1, weight=0)               # widget col
        self.body.columnconfigure(2, weight=1)               # tip col expands
        self._r = 0

    def add_row(self, label, widget, tip=""):
        tk.Label(self.body, text=label, font=(FONT, 10), fg=FG, bg=BG2,
                 anchor="w").grid(row=self._r, column=0, sticky="w", pady=4)
        widget.grid(row=self._r, column=1, sticky="w", pady=4, padx=(0, 0))
        if tip:
            tk.Label(self.body, text=tip, font=(FONT, 9), fg=FG_DIM, bg=BG2,
                     anchor="w").grid(row=self._r, column=2, sticky="w",
                                      padx=(14, 0), pady=4)
        self._r += 1

    def add_divider(self):
        tk.Frame(self.body, bg=BORDER, height=1).grid(
            row=self._r, column=0, columnspan=3, sticky="ew", pady=3)
        self._r += 1

    def add_note(self, text):
        tk.Label(self.body, text=text, font=(FONT, 9), fg=FG_DIM, bg=BG2,
                 anchor="w").grid(row=self._r, column=0, columnspan=3,
                                  sticky="w", pady=(0, 6))
        self._r += 1

    def reserve_row(self):
        """Reserve a row index for manual widget placement, return it."""
        r = self._r
        self._r += 1
        return r


# ── Widget factories ───────────────────────────────────────────────────────────
def mk_spin(parent, var, lo, hi, width=7):
    return tk.Spinbox(parent, textvariable=var, from_=lo, to=hi, width=width,
                      font=(FONT, 10), bg=BG3, fg=FG, buttonbackground=BG3,
                      relief="flat", bd=4, insertbackground=ACCENT,
                      highlightthickness=1, highlightbackground=BORDER,
                      highlightcolor=ACCENT)


def mk_combo(parent, var, values, width=28):
    return ttk.Combobox(parent, textvariable=var, values=values,
                        width=width, font=(FONT, 10), state="readonly")


def mk_btn(parent, text, cmd, accent=False, danger=False):
    bg = ACCENT if accent else (RED if danger else BG3)
    fg = "#000000" if accent else FG
    return tk.Button(parent, text=text, command=cmd,
                     font=(FONT, 10, "bold"), bg=bg, fg=fg,
                     activebackground=ACCENT, activeforeground="#000000",
                     relief="flat", bd=0, padx=14, pady=6, cursor="hand2")


# ── Main editor ────────────────────────────────────────────────────────────────
class ConfigEditor(tk.Tk):
    def __init__(self, config_path):
        super().__init__()
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self._saved = False

        self.title(f"MLB Scoreboard Setup  ·  {config_path}")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(600, 480)

        self._init_vars()
        self._init_styles()
        self._build()
        self._center()

    # ── Variables ──────────────────────────────────────────────────────────────
    def _init_vars(self):
        c   = self.cfg
        cvs = c.get("canvas", {})
        pol = c.get("polling_intervals", {})
        ui  = c.get("ui", {})

        id2name = {v: k for k, v in ALL_TEAMS.items()}
        self.v_team      = tk.StringVar(value=id2name.get(c.get("team_id"), "Houston Astros"))

        self.v_width     = tk.IntVar(value=cvs.get("width",       1100))
        self.v_height    = tk.IntVar(value=cvs.get("height",       700))
        self.v_bg        = tk.StringVar(value=cvs.get("bg_color",  "#0b162a"))
        self.v_fg        = tk.StringVar(value=cvs.get("fg_color",  "#eaeaea"))
        self.v_accent    = tk.StringVar(value=cvs.get("accent",    "#FFD700"))
        self.v_font      = tk.StringVar(value=cvs.get("font_family","Courier"))

        # Check if current colours already match the followed team — pre-tick if so
        team_colors = c.get("team_colors", {})
        team_name = id2name.get(c.get("team_id"), "")
        tc = team_colors.get(team_name, {})
        colors_match = (
            tc and
            cvs.get("bg_color", "").lower() == tc.get("primary", "").lower() and
            cvs.get("accent", "").lower()    == tc.get("accent", "").lower()
        )
        self.v_use_team_colors = tk.BooleanVar(value=bool(colors_match))

        self.v_poll_live = tk.IntVar(value=pol.get("live",       30))
        self.v_poll_pre  = tk.IntVar(value=pol.get("pre_game",   60))
        self.v_poll_sch  = tk.IntVar(value=pol.get("scheduled", 300))
        self.v_poll_none = tk.IntVar(value=pol.get("none",      3600))

        self.v_max_inn   = tk.IntVar(value=ui.get("max_innings", 9))
        self.v_lookahead = tk.IntVar(value=c.get("lookahead_days", 7))
        self.v_debug     = tk.BooleanVar(value=bool(c.get("debug", False)))

    # ── ttk styles ─────────────────────────────────────────────────────────────
    def _init_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TCombobox",
                    fieldbackground=BG3, background=BG3,
                    foreground=FG, selectbackground=ACCENT,
                    selectforeground="#000000",
                    bordercolor=BORDER, arrowcolor=ACCENT, padding=5)
        s.map("TCombobox", fieldbackground=[("readonly", BG3)])
        s.configure("Vertical.TScrollbar",
                    background=BG3, troughcolor=BG,
                    bordercolor=BG, arrowcolor=FG_DIM)

    # ── Build UI ───────────────────────────────────────────────────────────────
    def _build(self):
        # Title strip
        title_bar = tk.Frame(self, bg=BG, pady=14, padx=20)
        title_bar.pack(fill="x")
        tk.Label(title_bar, text="⚾  MLB SCOREBOARD",
                 font=(FONT, 15, "bold"), fg=ACCENT, bg=BG).pack(side="left")
        tk.Label(title_bar, text="  SETUP",
                 font=(FONT, 15), fg=FG_DIM, bg=BG).pack(side="left")
        tk.Label(title_bar, text=self.config_path,
                 font=(FONT, 9), fg=FG_DIM, bg=BG).pack(side="right")

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # Scrollable body
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True)

        self._cv = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self._cv.yview)
        self._cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._cv.pack(side="left", fill="both", expand=True)

        self._body = tk.Frame(self._cv, bg=BG)
        self._wid  = self._cv.create_window((0, 0), window=self._body, anchor="nw")
        self._body.bind("<Configure>",
            lambda e: self._cv.configure(scrollregion=self._cv.bbox("all")))
        self._cv.bind("<Configure>",
            lambda e: self._cv.itemconfig(self._wid, width=e.width))
        self._cv.bind_all("<MouseWheel>",
            lambda e: self._cv.yview_scroll(-(e.delta//120), "units"))

        SP = dict(fill="x", padx=16, pady=5)

        # ── Section 0: Upcoming Schedule (debug hub) ───────────────────────────
        s0 = Section(self._body, "UPCOMING SCHEDULE  —  DEBUG HUB")
        s0.pack(**SP)
        s0.add_note("Next scheduled games for the followed team. Useful for planning live test sessions.")

        # Schedule display frame — rows added dynamically by _refresh_schedule
        self._sched_frame = tk.Frame(s0.body, bg=BG2)
        self._sched_frame.grid(row=s0._r, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        s0._r += 1

        # Status label + refresh button on same row
        sched_ctrl = tk.Frame(s0.body, bg=BG2)
        sched_ctrl.grid(row=s0._r, column=0, columnspan=3, sticky="w", pady=(2, 0))
        s0._r += 1
        self._sched_status = tk.StringVar(value="Fetching schedule…")
        tk.Label(sched_ctrl, textvariable=self._sched_status,
                 font=(FONT, 9), fg=FG_DIM, bg=BG2).pack(side="left", padx=(0, 12))
        mk_btn(sched_ctrl, "↻  Refresh", self._refresh_schedule).pack(side="left")

        # Kick off background fetch
        self.after(200, self._refresh_schedule)

        # ── Section 1: Followed Team ───────────────────────────────────────────
        s1 = Section(self._body, "FOLLOWED TEAM")
        s1.pack(**SP)
        s1.add_row("Followed team",
                   mk_combo(s1.body, self.v_team, sorted(ALL_TEAMS.keys()), width=30))
        self.v_team.trace_add("write", lambda *_: self._on_team_color_toggle())

        # ── Section 2: Window & Display ───────────────────────────────────────
        s2 = Section(self._body, "WINDOW & DISPLAY")
        s2.pack(**SP)
        s2.add_row("Window width (px)",  mk_spin(s2.body, self.v_width,  400, 3840))
        s2.add_row("Window height (px)", mk_spin(s2.body, self.v_height, 300, 2160))
        s2.add_divider()
        s2.add_row("Font family",        mk_combo(s2.body, self.v_font, FONT_FAMILIES, width=22))
        s2.add_divider()

        # Team colour scheme checkbox
        chk_team = tk.Checkbutton(s2.body, variable=self.v_use_team_colors,
                                  text=" Use followed team's colour scheme",
                                  font=(FONT, 10), fg=FG, bg=BG2,
                                  selectcolor=BG3, activebackground=BG2,
                                  activeforeground=FG, highlightthickness=0,
                                  cursor="hand2",
                                  command=self._on_team_color_toggle)
        s2.add_row("Team colours", chk_team)
        s2.add_divider()

        s2.add_row("Background colour",  ColourPicker(s2.body, self.v_bg))
        s2.add_row("Foreground colour",  ColourPicker(s2.body, self.v_fg))
        s2.add_row("Accent colour",      ColourPicker(s2.body, self.v_accent))

        # Preview row — label in col 0, swatches in col 1
        tk.Label(s2.body, text="Preview", font=(FONT, 9), fg=FG_DIM, bg=BG2,
                 anchor="w").grid(row=s2._r, column=0, sticky="w", pady=(2, 6))
        self._preview = tk.Frame(s2.body, bg=BG2)
        self._preview.grid(row=s2._r, column=1, columnspan=2, sticky="w",
                           pady=(2, 6))
        s2._r += 1
        self._refresh_preview()
        for v in (self.v_bg, self.v_fg, self.v_accent):
            v.trace_add("write", lambda *_: self._refresh_preview())

        # ── Section 3: Polling ─────────────────────────────────────────────────
        s3 = Section(self._body, "POLLING INTERVALS (seconds)")
        s3.pack(**SP)
        s3.add_note("How often to fetch live data from the MLB Stats API.")
        s3.add_row("Live game",
                   mk_spin(s3.body, self.v_poll_live,   5,  300),
                   tip="While game is in progress")
        s3.add_row("Pre-game",
                   mk_spin(s3.body, self.v_poll_pre,   15,  600),
                   tip="< 1 hr before first pitch")
        s3.add_row("Scheduled",
                   mk_spin(s3.body, self.v_poll_sch,   60, 3600),
                   tip="> 1 hr before next game")
        s3.add_row("No game",
                   mk_spin(s3.body, self.v_poll_none, 300, 86400),
                   tip="Off-day / no upcoming games")

        # ── Section 4: Scoreboard UI ───────────────────────────────────────────
        s4 = Section(self._body, "SCOREBOARD UI")
        s4.pack(**SP)
        s4.add_row("Max innings shown",
                   mk_spin(s4.body, self.v_max_inn, 9, 15))
        s4.add_row("Schedule lookahead",
                   mk_spin(s4.body, self.v_lookahead, 1, 30),
                   tip="Days ahead to search for next game")
        s4.add_divider()
        chk = tk.Checkbutton(s4.body, variable=self.v_debug,
                             text=" Enable verbose debug logging",
                             font=(FONT, 10), fg=FG, bg=BG2,
                             selectcolor=BG3, activebackground=BG2,
                             activeforeground=FG, highlightthickness=0,
                             cursor="hand2")
        s4.add_row("Debug mode", chk)

        tk.Frame(self._body, bg=BG, height=8).pack()

        # ── Bottom bar ─────────────────────────────────────────────────────────
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        bar = tk.Frame(self, bg=BG, pady=10, padx=16)
        bar.pack(fill="x")

        self.v_status = tk.StringVar()
        tk.Label(bar, textvariable=self.v_status, font=(FONT, 9),
                 fg=GREEN, bg=BG, anchor="w").pack(side="left")

        mk_btn(bar, "Cancel",       self.destroy).pack(side="right", padx=(6, 0))
        mk_btn(bar, "Save & Close", self._save_and_close, accent=True).pack(side="right", padx=(6, 0))
        mk_btn(bar, "Save",         self._save).pack(side="right")

    # ── Schedule / debug hub ───────────────────────────────────────────────────
    def _refresh_schedule(self):
        """Fetch upcoming schedule in a background thread and update the display."""
        self._sched_status.set("Fetching…")
        for w in self._sched_frame.winfo_children():
            w.destroy()
        threading.Thread(target=self._fetch_schedule_bg, daemon=True).start()

    def _fetch_schedule_bg(self):
        """Background thread: fetch next 6 games from MLB Stats API."""
        try:
            team_id = ALL_TEAMS.get(self.v_team.get(), self.cfg.get("team_id", 117))
            today   = datetime.date.today()
            end     = today + datetime.timedelta(days=30)
            r = requests.get(
                "https://statsapi.mlb.com/api/v1/schedule",
                params={
                    "sportId":   1,
                    "teamId":    team_id,
                    "startDate": today.strftime("%Y-%m-%d"),
                    "endDate":   end.strftime("%Y-%m-%d"),
                    "hydrate":   "team,venue",
                    "fields":    ("dates,date,games,gamePk,gameDate,status,"
                                  "teams,away,home,team,name,record,wins,losses,"
                                  "venue,name"),
                },
                timeout=8,
            )
            r.raise_for_status()
            dates = r.json().get("dates", [])
            games = [g for d in dates for g in d.get("games", [])][:6]
            self.after(0, lambda: self._render_schedule(games))
        except Exception as e:
            self.after(0, lambda: self._sched_status.set(f"⚠  {e}"))

    def _render_schedule(self, games):
        """Render fetched games into the schedule frame."""
        for w in self._sched_frame.winfo_children():
            w.destroy()

        if not games:
            tk.Label(self._sched_frame, text="No upcoming games found.",
                     font=(FONT, 9), fg=FG_DIM, bg=BG2).pack(anchor="w")
            self._sched_status.set("No games found.")
            return

        # Column headers
        hdr = tk.Frame(self._sched_frame, bg=BG3)
        hdr.pack(fill="x", pady=(0, 2))
        for col, w, anchor in [
            ("DATE / TIME",   18, "w"),
            ("MATCHUP",       34, "w"),
            ("VENUE",         22, "w"),
            ("STATUS",        18, "w"),
            ("GAME PK",        9, "w"),
        ]:
            tk.Label(hdr, text=col, font=(FONT, 8, "bold"),
                     fg=ACCENT, bg=BG3, width=w, anchor=anchor).pack(side="left", padx=2)

        # Game rows
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for i, g in enumerate(games):
            row_bg = BG3 if i % 2 == 0 else BG2
            row = tk.Frame(self._sched_frame, bg=row_bg, cursor="hand2")
            row.pack(fill="x", pady=1)

            # Parse game time
            try:
                gdt = datetime.datetime.fromisoformat(
                    g["gameDate"].replace("Z", "+00:00"))
                local_dt = gdt.astimezone()
                date_str = local_dt.strftime("%a %b %d  %I:%M %p")
                # Time-to-game for planning
                delta = gdt - now_utc
                total_s = int(delta.total_seconds())
                if total_s < 0:
                    eta = "now / in progress"
                elif total_s < 3600:
                    eta = f"in {total_s//60}m"
                elif total_s < 86400:
                    eta = f"in {total_s//3600}h {(total_s%3600)//60}m"
                else:
                    eta = f"in {total_s//86400}d {(total_s%86400)//3600}h"
            except Exception:
                date_str = g.get("gameDate", "")[:16]
                eta = ""

            away   = g.get("teams", {}).get("away", {}).get("team", {}).get("name", "?")
            home   = g.get("teams", {}).get("home", {}).get("team", {}).get("name", "?")
            matchup = f"{away} @ {home}"

            venue  = g.get("venue", {}).get("name", "—")
            status = g.get("status", {}).get("detailedState", "—")
            pk     = str(g.get("gamePk", "—"))

            # Status colour
            if "Progress" in status or "Live" in status:
                st_col = "#2ecc71"
            elif "Final" in status:
                st_col = "#7f8c8d"
            elif "Scheduled" in status or "Pre" in status:
                st_col = ACCENT
            else:
                st_col = FG_DIM

            for text, w, fg_c, anchor in [
                (f"{date_str}  ({eta})", 18, FG,     "w"),
                (matchup,                34, FG,     "w"),
                (venue[:28],             22, FG_DIM, "w"),
                (status,                 18, st_col, "w"),
                (pk,                      9, FG_DIM, "w"),
            ]:
                tk.Label(row, text=text, font=(FONT, 9),
                         fg=fg_c, bg=row_bg, width=w, anchor=anchor).pack(
                             side="left", padx=2, pady=3)

            # Click row → set followed team to home team, flash gold to confirm
            orig_colors = {}
            def _on_click(e, home_name=home, r=row, bg=row_bg, oc=orig_colors):
                if home_name not in ALL_TEAMS:
                    return
                # Snapshot original label colours before flash
                if not oc:
                    for c in r.winfo_children():
                        oc[c] = c.cget("fg")
                # Flash row gold
                try:
                    r.configure(bg=ACCENT)
                    for c in r.winfo_children():
                        c.configure(bg=ACCENT, fg="#000000")
                except Exception:
                    pass
                # Set team after flash starts; delay schedule refresh until after restore
                self.v_team.set(home_name)
                def _restore():
                    try:
                        if r.winfo_exists():
                            r.configure(bg=bg)
                            for c in r.winfo_children():
                                if c.winfo_exists():
                                    c.configure(bg=bg, fg=oc.get(c, FG))
                    except Exception:
                        pass
                    # Now safe to refresh schedule (old widgets already restored or gone)
                    self._refresh_schedule()
                self.after(400, _restore)

            row.bind("<Button-1>", _on_click)
            for child in row.winfo_children():
                child.bind("<Button-1>", _on_click)

        fetched_at = datetime.datetime.now().strftime("%H:%M:%S")
        self._sched_status.set(f"✓  {len(games)} games shown  ·  fetched {fetched_at}")

    # ── Team colour helpers ────────────────────────────────────────────────────
    def _get_team_colors(self, team_name):
        """Return (primary, accent) for the given team name, or None if not found."""
        tc = self.cfg.get("team_colors", {}).get(team_name)
        if tc and "primary" in tc and "accent" in tc:
            return tc["primary"], tc["accent"]
        return None

    def _on_team_color_toggle(self, *_):
        """Apply or release team colours based on checkbox state and current team."""
        if not self.v_use_team_colors.get():
            return
        team_name = self.v_team.get()
        colors = self._get_team_colors(team_name)
        if colors:
            primary, accent = colors
            self.v_bg.set(primary.upper())
            self.v_accent.set(accent.upper())
            # Keep foreground light — it's readable on any team background
            self.v_fg.set("#eaeaea")
        else:
            # No team colors in config — warn but leave values unchanged
            self.v_status.set(f"⚠  No colours found for {team_name} in config")
            self.after(4000, lambda: self.v_status.set(""))

    # ── Preview ────────────────────────────────────────────────────────────────
    def _refresh_preview(self):
        for w in self._preview.winfo_children():
            w.destroy()
        for name, var in [("BG", self.v_bg), ("FG", self.v_fg), ("Accent", self.v_accent)]:
            col = var.get()
            try:
                tk.Label(self._preview, text=f"  {name}  ",
                         bg=col, fg=contrast(col),
                         font=(FONT, 9, "bold"), padx=4, pady=4).pack(side="left", padx=2)
            except Exception:
                pass

    # ── Save ───────────────────────────────────────────────────────────────────
    def _collect(self):
        cfg = deepcopy(self.cfg)
        cfg["team_id"] = ALL_TEAMS.get(self.v_team.get(), cfg.get("team_id", 117))
        cfg.setdefault("canvas", {}).update({
            "width":       self.v_width.get(),
            "height":      self.v_height.get(),
            "bg_color":    self.v_bg.get(),
            "fg_color":    self.v_fg.get(),
            "accent":      self.v_accent.get(),
            "font_family": self.v_font.get(),
        })
        cfg["polling_intervals"] = {
            "live":      self.v_poll_live.get(),
            "pre_game":  self.v_poll_pre.get(),
            "scheduled": self.v_poll_sch.get(),
            "none":      self.v_poll_none.get(),
        }
        cfg.setdefault("ui", {})["max_innings"] = self.v_max_inn.get()
        cfg["lookahead_days"] = self.v_lookahead.get()
        cfg["debug"] = bool(self.v_debug.get())
        return cfg

    def _save(self):
        try:
            cfg = self._collect()
            save_config(self.config_path, cfg)
            self.cfg = cfg
            self._saved = True
            self.v_status.set(f"✓  Saved → {self.config_path}")
            self.after(4000, lambda: self.v_status.set(""))
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def _save_and_close(self):
        self._save()
        if self._saved:
            self.destroy()

    # ── Center ─────────────────────────────────────────────────────────────────
    def _center(self):
        self.update_idletasks()
        w, h = 780, 820
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")


def main():
    ap = argparse.ArgumentParser(description="MLB Scoreboard Config Editor")
    ap.add_argument("--config", default="config.json",
                    help="Path to config.json (default: config.json)")
    args = ap.parse_args()
    ConfigEditor(args.config).mainloop()


if __name__ == "__main__":
    main()
