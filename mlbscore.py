#!/usr/bin/env python3
"""
mlbscore_final_v8.py — Corrected and Streamlined Scoreboard
Features:
- Improved thread safety using ThreadPoolExecutor for network calls.
- Ensures all GUI updates are scheduled on the main Tkinter thread.
- Streamlined base/runner logic.
- Uses config.json for settings and team colors.
- Shows only two out circles (never shows 3).
- Immediately resets bases, balls, strikes, outs once a 3rd out is detected (single-trigger per inning/half).
- Keeps all visuals, runner animations, grid, and at-bat ⚾ icon.
- Throttled debug logging (only on state change or important events).
- Includes Clean Inning Highlight logic.
- NEW: Smart Polling Logic to efficiently wait for the next scheduled game (triggers 1 hour before start).
- NEW: Countdown timer display format: $days, HH:MM:SS.
"""

import tkinter as tk
from tkinter import font as tkfont
import threading
import requests
import json
import datetime
import signal
import pathlib
import argparse
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor # NEW: For cleaner thread management

# -------------------------
# Defaults
# -------------------------
DEFAULT_CONFIG = {
    "team_id": 117,
    "teams": {},
    "team_colors": {},
    "polling_intervals": {"live": 15, "scheduled": 300, "none": 3600},
    "lookahead_days": 7,
    "canvas": {
        "width": 1100,
        "height": 700,
        "bg_color": "#0b162a",
        "fg_color": "#eaeaea",
        "accent": "#FFD700",
        "font_family": "Courier"
    },
    "ui": {"max_innings": 9},
    "debug": False
}

# -------------------------
# CLI
# -------------------------
parser = argparse.ArgumentParser(description="MLB Canvas Scoreboard (final v8)")
parser.add_argument("--config", default="config.json", help="Path to config.json")
parser.add_argument("--team", help="Team name (overrides config team_id if found)")
parser.add_argument("--debug", action="store_true", help="Enable debug logging (overrides config)")
args = parser.parse_args()

# -------------------------
# Config loader
# -------------------------
def load_config(path):
    cfg = deepcopy(DEFAULT_CONFIG)
    p = pathlib.Path(path)
    if not p.exists():
        print(f"[INFO] config {path} not found; using defaults")
        return cfg
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for k, v in data.items():
            if isinstance(v, dict) and k in cfg:
                cfg[k].update(v)
            else:
                cfg[k] = v
        return cfg
    except Exception as e:
        print("[ERROR] Failed to load config:", e)
        return cfg

CONFIG = load_config(args.config)
# Allow CLI --debug to override config.json; otherwise use config value
CONFIG["debug"] = args.debug or CONFIG.get("debug", False)

TEAM_ID = CONFIG.get("team_id")
POLLING = CONFIG.get("polling_intervals", {"live": 15, "scheduled": 300, "none": 3600})
LOOKAHEAD_DAYS = CONFIG.get("lookahead_days", 7)
CANVAS_CFG = CONFIG.get("canvas", {})
UI_CFG = CONFIG.get("ui", {})
DEBUG = CONFIG.get("debug", False)
TEAM_COLORS = CONFIG.get("team_colors", {})

if args.team and args.team in CONFIG.get("teams", {}):
    TEAM_ID = CONFIG["teams"][args.team]

# -------------------------
# Networking helpers
# -------------------------
def make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.6,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(['GET']))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": "mlbscore-final-v8/1.0"})
    return s

def parse_iso_to_local(dtstr):
    if not dtstr:
        return None
    try:
        # Using fromisoformat handles 'Z' implicitly with +00:00 replacement logic.
        dt = datetime.datetime.fromisoformat(dtstr.replace("Z", "+00:00"))
        return dt.astimezone()
    except Exception:
        return None

def fetch_schedule(team_id=TEAM_ID, lookahead=LOOKAHEAD_DAYS):
    sess = make_session()
    # Use date.today() for simplicity
    today = datetime.date.today()
    start = today - datetime.timedelta(days=1)
    end = today + datetime.timedelta(days=lookahead)
    url = "https://statsapi.mlb.com/api/v1/schedule"
    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d"),
        "hydrate": "team,linescore"
    }
    try:
        r = sess.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG] fetch_schedule error: {e}")
        return []
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            gd = parse_iso_to_local(g.get("gameDate"))
            if gd:
                g["gameDate_dt"] = gd
            games.append(g)
    return sorted(games, key=lambda g: g.get("gameDate_dt") or datetime.datetime.max)

def fetch_live_feed(gamePk):
    if not gamePk:
        return None
    sess = make_session()
    # Using f-string for URL
    url = f"https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"
    try:
        r = sess.get(url, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG] fetch_live_feed error: {e}")
        return None

# -------------------------
# Helpers
# -------------------------
def get_team_name(entry):
    if not entry:
        return "UNKNOWN"
    if isinstance(entry, dict):
        if "team" in entry and isinstance(entry["team"], dict):
            return entry["team"].get("name") or entry["team"].get("teamName") or "UNKNOWN"
        return entry.get("name") or entry.get("teamName") or str(entry)
    return str(entry)

def team_color_for(name):
    if not name:
        return (CANVAS_CFG.get("bg_color", "#000000"), CANVAS_CFG.get("accent", "#FFFFFF"))
    tc = TEAM_COLORS.get(name)
    if isinstance(tc, dict):
        prim = tc.get("primary", CANVAS_CFG.get("bg_color", "#000000"))
        acc = tc.get("accent", CANVAS_CFG.get("accent", "#FFFFFF"))
        return (prim, acc)
    # Case-insensitive fallback lookup
    for k, v in TEAM_COLORS.items():
        if k.lower() == name.lower() and isinstance(v, dict):
            return (v.get("primary", CANVAS_CFG.get("bg_color")), v.get("accent", CANVAS_CFG.get("accent")))
    return (CANVAS_CFG.get("bg_color", "#000000"), CANVAS_CFG.get("accent", "#FFFFFF"))

def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

def rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, int(x))) for x in rgb])

# Simplified color blend
def blend_colors(c1, c2, t):
    rgb1 = hex_to_rgb(c1)
    rgb2 = hex_to_rgb(c2)
    blended = [int(r1 + (r2 - r1) * t) for r1, r2 in zip(rgb1, rgb2)]
    return rgb_to_hex(blended)

# -------------------------
# GUI App
# -------------------------
class ScoreboardApp:
    def __init__(self, root):
        self.root = root
        self.team_id = TEAM_ID
        self.polling = POLLING
        self.debug = DEBUG
        self.balls = 0
        self.strikes = 0
        self.outs = 0
        self.next_update_in = 0

        # canvas config
        self.width = CANVAS_CFG.get("width", 1100)
        self.height = CANVAS_CFG.get("height", 700)
        self.bg = CANVAS_CFG.get("bg_color", "#0b162a")
        self.fg = CANVAS_CFG.get("fg_color", "#eaeaea")
        self.accent = CANVAS_CFG.get("accent", "#FFD700")
        self.font_family = CANVAS_CFG.get("font_family", "Courier")

        self.canvas = tk.Canvas(root, width=self.width, height=self.height,
                                bg=self.bg, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # fonts
        self.font_title = tkfont.Font(family=self.font_family, size=18, weight="bold")
        self.font_header = tkfont.Font(family=self.font_family, size=11, weight="bold")
        self.font_team = tkfont.Font(family=self.font_family, size=13, weight="bold")
        self.font_small = tkfont.Font(family=self.font_family, size=10)
        self.font_status = tkfont.Font(family=self.font_family, size=12, weight="bold")

        # ThreadPoolExecutor for network operations
        self.executor = ThreadPoolExecutor(max_workers=1)

        # state
        self.games = []
        self.last_game = None
        self.next_game = None
        self.live_game = None
        self.live_feed = None
        self.poll_interval = self.polling.get("none", 3600)
        self.next_update_in = 0
        self.running_fetch = False

        # base state
        self.bases = {
            "1B": {"occupied": False, "team": None, "anim": None},
            "2B": {"occupied": False, "team": None, "anim": None},
            "3B": {"occupied": False, "team": None, "anim": None},
        }
        self.empty_base_fill = "#d0d0d0"

        # runner animation state
        self.runners = {} # {rkey: {"cid": tk_id, "base": "1B", "color": "#HEX"}}
        self.runners_by_base = {} # {"1B": rkey}
        self._next_runner_key = 1

        self.current_batter = "Batter: -"
        self.current_pitcher = "Pitcher: -"

        # followed team name
        self.followed_team_name = None
        for name, tid in CONFIG.get("teams", {}).items():
            if tid == self.team_id:
                self.followed_team_name = name
                break
        if not self.followed_team_name:
            self.followed_team_name = f"Team {self.team_id}"

        # BSO/out tracking
        self._last_outs = 0
        self._outs_reset_pending = False
        self._inning_reset_done = False
        self._last_inning = None
        self._last_inning_half = None

        # layout caches
        self.left_margin = 60
        self.top_margin = 60
        self.score_start_x = 320
        self.col_width = 44
        self.row_height = 42
        self.diamond_cx = None
        self.diamond_cy = None
        self.diamond_ds = None
        self.base_positions = {}

        # initial loop
        self.root.after(100, self.update_loop)

        # limited debug trackers
        self._last_poll_time = 0
        self._last_runner_state = {}
        self._last_log_state = None

    def log(self, *args, verbose=False, level="info"):
        if verbose:
            if not self.debug:
                return
            print("[DEBUG]", *args)
        else:
            # Using f-string for error logging
            if level and str(level).lower() == "error":
                print(f"[ERROR]", *args)
                return
            if self.debug:
                # Using f-string for info logging
                print(f"[{str(level).upper()}]", *args)

    # runner helpers
    def compute_base_positions(self):
        ds = self.diamond_ds or 120
        # Improved default positioning for robustness
        cx = self.diamond_cx or (self.left_margin + 180)
        cy = self.diamond_cy or (self.top_margin + 300)
        inset = ds * 0.6
        self.base_positions = {
            "2B": (cx, cy - inset),
            "1B": (cx + inset, cy),
            "3B": (cx - inset, cy),
            "Home": (cx, cy + inset)
        }

    def spawn_runner_at_base(self, base_key, color=None):
        if base_key == "Home" or base_key in self.runners_by_base:
            return None
        self.compute_base_positions()
        pos = self.base_positions.get(base_key)
        if pos is None:
            return None
        bx, by = pos
        color = color or self.accent
        rkey = f"r{self._next_runner_key}"
        self._next_runner_key += 1
        # Runner is a simple circle on the canvas
        cid = self.canvas.create_oval(bx - 8, by - 8, bx + 8, by + 8,
                                      fill=color, outline="white", width=2)
        self.runners[rkey] = {"cid": cid, "base": base_key, "color": color}
        self.runners_by_base[base_key] = rkey
        self.log(f"Runner spawned: {rkey} at {base_key}", verbose=True)
        return rkey

    # Modified to always remove from runners_by_base if present
    def move_runner_base(self, from_base, to_base, steps=12):
        rkey = self.runners_by_base.pop(from_base, None)
        runner = self.runners.get(rkey)

        if not rkey or not runner:
            self.log(f"Move requested from {from_base} but no runner found/present; checking {to_base}.", verbose=True)
            if to_base != "Home":
                # If a runner mysteriously disappeared or was missed, spawn one at the destination
                return self.spawn_runner_at_base(to_base, color=self.accent)
            return None # Scored/Out, no action needed

        self.compute_base_positions()
        start = self.base_positions.get(from_base)
        end = self.base_positions.get(to_base)
        color = runner.get("color", self.accent)

        # Clear old canvas object and pop runner from self.runners immediately
        try:
            self.canvas.delete(runner["cid"])
        except Exception:
            pass
        self.runners.pop(rkey, None)

        if not start or not end:
            # If positions are unknown, just spawn the new runner and log a warning
            self.log(f"Error: Base positions unknown for {from_base} or {to_base}. Spawning at destination.", level="error")
            if to_base != "Home":
                return self.spawn_runner_at_base(to_base, color=color)
            return None

        sx, sy = start
        tx, ty = end
        dx = (tx - sx) / float(steps)
        dy = (ty - sy) / float(steps)

        # Create the temporary moving object
        temp_cid = self.canvas.create_oval(sx - 8, sy - 8, sx + 8, sy + 8, fill=color, outline="white", width=2)

        def _step(i=0):
            if i >= steps:
                self.canvas.delete(temp_cid)
                if to_base != "Home":
                    # Spawn the static runner at the new base
                    new_key = self.spawn_runner_at_base(to_base, color=color)
                    self.log(f"Runner moved: {rkey} {from_base} -> {to_base} as {new_key}", verbose=True)
                    return
                else:
                    # Runner scored, do the fade out animation
                    shrink_id = self.canvas.create_oval(tx - 8, ty - 8, tx + 8, ty + 8, fill=color, outline="white", width=2)
                    def _shrink(step=0, maxs=6):
                        if step >= maxs:
                            try:
                                self.canvas.delete(shrink_id)
                            except Exception:
                                pass
                            return
                        scale = 1 - (step / float(maxs))
                        w = int(8 * scale)
                        self.canvas.coords(shrink_id, tx - w, ty - w, tx + w, ty + w)
                        self.root.after(40, lambda: _shrink(step + 1, maxs))
                    _shrink()
                    self.log(f"Runner {rkey} scored at Home", verbose=True)
                    return
            try:
                self.canvas.move(temp_cid, dx, dy)
            except Exception:
                pass
            # Always schedule GUI updates using self.root.after in animation
            self.root.after(30, lambda: _step(i + 1))

        _step()
        return rkey

    def clear_all_runners(self):
        for rkey, info in list(self.runners.items()):
            try:
                self.canvas.delete(info.get("cid"))
            except Exception:
                pass
        self.runners.clear()
        self.runners_by_base.clear()
        self.log("All runners cleared", verbose=True)

    def render_full_gui(self):
        """Wrapper to ensure full render is called on the main thread."""
        self.render(full=True)

    def format_seconds_to_dhms_string(self, seconds):
        """Formats an integer number of seconds into '$days, HH:MM:SS' string."""
        seconds = int(seconds)
        if seconds <= 0:
            return "00:00:00"

        td = datetime.timedelta(seconds=seconds)
        hours = td.seconds // 3600
        minutes = (td.seconds % 3600) // 60
        secs = td.seconds % 60
        time_part = f"{hours:02}:{minutes:02}:{secs:02}"
        return f"{td.days}d, {time_part}" if td.days > 0 else time_part

    # rendering
    def render(self, full=True):
        if full:
            self.canvas.delete("all")
        else:
            # Using specific tag for footer
            self.canvas.delete("footer")

        game_src = None
        linescore = {}
        if self.live_feed:
            game_src = self.live_feed.get("gameData", {}) or {}
            linescore = self.live_feed.get("liveData", {}).get("linescore", {}) or {}
        elif self.last_game:
            game_src = self.last_game
            linescore = self.last_game.get("linescore", {}) or {}
        elif self.next_game:
            game_src = self.next_game
            linescore = self.next_game.get("linescore", {}) or {}

        if not game_src:
            msg = f"Waiting for game data for {self.followed_team_name}"
            self.canvas.create_text(self.width // 2, self.height // 2,
                                    text=msg, font=self.font_title, fill=self.fg)
            time_display = self.format_seconds_to_dhms_string(self.next_update_in)
            footer = f"{msg} | Next update in: {time_display}"
            self.canvas.create_text(self.width // 2, self.height - 20,
                                    text=footer, font=self.font_small, fill=self.accent, tags="footer")
            return
            
        # Get current inning index for highlighting
        active_inning_idx = -1
        if self.live_feed:
            ls = self.live_feed.get("liveData", {}).get("linescore", {}) or {}
            active_inning_idx = ls.get("currentInning", 0) - 1

        away = get_team_name(game_src.get("teams", {}).get("away", {}))
        home = get_team_name(game_src.get("teams", {}).get("home", {}))
        innings = linescore.get("innings", []) if linescore else []
        max_innings = max(len(innings), UI_CFG.get("max_innings", 9))

        left_margin = self.left_margin
        top_margin = self.top_margin
        team_x = left_margin
        score_start_x = self.score_start_x
        col_width = self.col_width
        row_height = self.row_height

        title_text = f"{self.followed_team_name} — MLB Scoreboard"
        self.canvas.create_text(self.width // 2, 22, text=title_text, font=self.font_title, fill=self.accent)

        # header team cell
        self.canvas.create_rectangle(team_x - 8, top_margin - 18, score_start_x - 4, top_margin + 18,
                                     fill=self.bg, outline="black")
        self.canvas.create_text(team_x, top_margin, text="TEAM", font=self.font_header, fill=self.accent, anchor="w")

        # inning header cells
        for i in range(max_innings):
            x_center = score_start_x + i * col_width
            bg_fill = blend_colors(self.accent, self.bg, 0.9) if i == active_inning_idx else self.bg
            text_fill = self.fg if i == active_inning_idx else self.accent
            self.canvas.create_rectangle(x_center - col_width // 2, top_margin - 18,
                                         x_center + col_width // 2, top_margin + 18,
                                         fill=bg_fill, outline="black")
            self.canvas.create_text(x_center, top_margin, text=str(i + 1), font=self.font_header, fill=text_fill)

        # totals headers: R, H, E, extra (bat icon column)
        totals_labels = ("R", "H", "E", "⚾")
        for j, label in enumerate(totals_labels):
            x_center = score_start_x + (max_innings + j) * col_width
            self.canvas.create_rectangle(x_center - col_width // 2, top_margin - 18,
                                         x_center + col_width // 2, top_margin + 18,
                                         fill=self.bg, outline="black")
            self.canvas.create_text(x_center, top_margin, text=label if label != "⚾" else "🦇", font=self.font_header, fill=self.accent)

        # draw team rows (colored) and per-inning values
        def draw_team_row(y, name, side, active_idx):
            bg_col, fg_col = team_color_for(name)
            self.canvas.create_rectangle(team_x - 8, y - 18, score_start_x - 4, y + 18, fill=bg_col, outline="black")
            self.canvas.create_text(team_x, y, text=name, font=self.font_team, fill=fg_col, anchor="w")
            for i in range(max_innings):
                run_val = "-"
                if innings and i < len(innings):
                    inning = innings[i]
                    if side == "away" and "away" in inning:
                        run_val = inning["away"].get("runs", "-")
                    if side == "home" and "home" in inning:
                        run_val = inning["home"].get("runs", "-")
                x1 = score_start_x + i * col_width - col_width // 2
                x2 = score_start_x + i * col_width + col_width // 2
                cell_bg = blend_colors(bg_col, self.accent, 0.25) if i == active_idx else bg_col
                self.canvas.create_rectangle(x1, y - 18, x2, y + 18, fill=cell_bg, outline="black")
                self.canvas.create_text(score_start_x + i * col_width, y, text=str(run_val), font=self.font_team,
                                        fill=fg_col)
            totals = linescore.get("teams", {}).get(side, {})
            for j, key in enumerate(("runs", "hits", "errors")):
                val = str(totals.get(key, "-"))
                x_center = score_start_x + (max_innings + j) * col_width
                self.canvas.create_rectangle(x_center - col_width // 2, y - 18, x_center + col_width // 2, y + 18,
                                             fill=bg_col, outline="black")
                self.canvas.create_text(x_center, y, text=val, font=self.font_team, fill=fg_col)
            # extra icon cell (leave blank / will draw bat icon separately)
            x_icon = score_start_x + (max_innings + 3) * col_width
            self.canvas.create_rectangle(x_icon - col_width // 2, y - 18, x_icon + col_width // 2, y + 18,
                                         fill=bg_col, outline="black")

        y_away = top_margin + row_height
        y_home = y_away + row_height
        draw_team_row(y_away, away, "away", active_inning_idx)
        draw_team_row(y_home, home, "home", active_inning_idx)

        # --- Clean, properly aligned grid overlay ---
        grid_left = team_x - 8
        grid_top = top_margin - 18
        grid_right = score_start_x + (max_innings + 3) * col_width + col_width // 2
        grid_bottom = grid_top + row_height * 3  # header + away + home full enclosure

        for i in range(max_innings + 4):
            x = score_start_x + (i - 0.5) * col_width
            self.canvas.create_line(x, grid_top, x, grid_bottom, fill="#38444d", width=1)

        for j in range(3):
            y = grid_top + (j + 1) * row_height
            self.canvas.create_line(grid_left, y, grid_right, y, fill="#38444d", width=1)

        self.canvas.create_rectangle(grid_left, grid_top, grid_right, grid_bottom, outline="#55606b", width=2)

        # Diamond and bases
        self.diamond_cx = self.left_margin + 180
        self.diamond_cy = y_home + row_height + 140
        self.diamond_ds = 120
        ds = self.diamond_ds
        diamond_pts = [self.diamond_cx, self.diamond_cy - ds, self.diamond_cx + ds, self.diamond_cy,
                       self.diamond_cx, self.diamond_cy + ds, self.diamond_cx - ds, self.diamond_cy]
        self.canvas.create_polygon(diamond_pts, outline=self.accent, fill="#6b8f57", width=3)

        inset = ds * 0.6
        self.base_positions = {"2B": (self.diamond_cx, self.diamond_cy - inset),
                              "1B": (self.diamond_cx + inset, self.diamond_cy),
                              "3B": (self.diamond_cx - inset, self.diamond_cy),
                              "Home": (self.diamond_cx, self.diamond_cy + inset)}
        base_half = 18
        for bname, (bx, by) in self.base_positions.items():
            b = self.bases.get(bname if bname in self.bases else bname, {"occupied": False, "team": None, "anim": None})
            fill = self.empty_base_fill
            anim = b.get("anim")
            if anim:
                fill = anim.get("current", self.empty_base_fill)
            else:
                # Use runners_by_base to determine base fill color
                if bname in self.runners_by_base:
                    rkey = self.runners_by_base.get(bname)
                    r_info = self.runners.get(rkey)
                    if r_info:
                        # Use the runner's color for the base fill when no animation is running
                        fill = team_color_for(b["team"])[0] if b["team"] else r_info["color"]
                    else:
                        fill = team_color_for(b["team"])[0] if b["team"] else self.accent
                elif b.get("occupied"):
                    # Fallback to team color if the logic for runners is skipped/failed
                    fill = team_color_for(b["team"])[0] if b["team"] else self.accent
                    
            pts = [bx, by - base_half, bx + base_half, by, bx, by + base_half, bx - base_half, by]
            self.canvas.create_polygon(pts, fill=fill, outline="white", width=2)
            self.canvas.create_text(bx, by, text=bname, font=self.font_small, fill=self.fg)

        # Draw runner icons (static ones) on bases
        # Only draw runners that exist in self.runners (moved to draw the base itself)
        for base_key, rkey in list(self.runners_by_base.items()):
            info = self.runners.get(rkey)
            if not info:
                continue
            pos = self.base_positions.get(base_key)
            if not pos:
                continue
            bx, by = pos
            # This creates the actual runner circle *over* the base icon
            self.canvas.create_oval(bx - 8, by - 8, bx + 8, by + 8, fill=info.get("color", self.accent),
                                    outline="white", width=2)

        # Compute who is at bat and draw bat icon in the extra column
        batting_team = None
        if self.live_feed:
            ls = self.live_feed.get("liveData", {}).get("linescore", {}) or {}
            inning_half = ls.get("inningHalf") or None
            if inning_half:
                if str(inning_half).lower() == "top":
                    batting_team = away
                elif str(inning_half).lower() == "bottom":
                    batting_team = home
        # Draw bat icon ⚾ in the extra column for the batting team
        if batting_team:
            icon = "⚾"
            x_icon = score_start_x + (max_innings + 3) * col_width
            if batting_team == away:
                y_icon = y_away
            else:
                y_icon = y_home
            self.canvas.create_text(x_icon, y_icon, text=icon, font=self.font_team, fill=self.accent)

        # B/S/O to the right of the diamond
        bso_x = self.diamond_cx + ds + 120
        balls = strikes = outs = None
        raw_balls = raw_strikes = raw_outs = 0
        if self.live_feed:
            counts = self.live_feed.get("liveData", {}).get("plays", {}).get("currentPlay", {}).get("count", {}) or {}
            try:
                raw_balls = int(counts.get("balls", 0))
            except Exception:
                raw_balls = 0
            try:
                raw_strikes = int(counts.get("strikes", 0))
            except Exception:
                raw_strikes = 0
            try:
                raw_outs = int(self.live_feed.get("liveData", {}).get("linescore", {}).get("outs", 0))
            except Exception:
                raw_outs = 0

            # immediate single-trigger reset on 3rd out (and never show 3)
            ls_hdr = self.live_feed.get("liveData", {}).get("linescore", {}) or {}
            curr_inning = ls_hdr.get("currentInning")
            curr_half = ls_hdr.get("inningHalf")
            if (curr_inning, curr_half) != (self._last_inning, self._last_inning_half):
                self._inning_reset_done = False
                self._last_inning = curr_inning
                self._last_inning_half = curr_half

            if raw_outs >= 3 and not self._inning_reset_done:
                # immediate reset right here (GUI update in main loop)
                self.log("Third out detected — resetting counts and clearing bases.", verbose=True)
                self.reset_after_third_out()
                # reset internal counts to zero so display reflects it immediately
                balls = 0
                strikes = 0
                outs = 0
                self._inning_reset_done = True
            else:
                # Cleaned up BSO assignment to max/min
                balls = max(0, min(3, raw_balls))
                strikes = max(0, min(2, raw_strikes))
                outs = max(0, min(2, raw_outs))
        else:
            balls = strikes = outs = None

        def bso_color(kind, value):
            if value is None:
                return "#7f8c8d"
            if kind == "balls":
                if value == 0:
                    return "#00a651"
                elif 1 <= value <= 2:
                    return "#f1c40f"
                else:
                    return "#e74c3c"
            elif kind == "strikes":
                if value == 0:
                    return "#00a651"
                elif value == 1:
                    return "#f1c40f"
                else:
                    return "#e74c3c"
            elif kind == "outs":
                if value == 0:
                    return "#00a651"
                elif 1 <= value <= 2:
                    return "#f1c40f"
                else:
                    return "#e74c3c"
            return "#7f8c8d"

        dot_r = 8
        spacing = 28
        top_of_bso = self.diamond_cy - spacing
        self.canvas.create_text(bso_x, top_of_bso - spacing, text="BALLS", font=self.font_small, fill=self.fg, anchor="w")
        for i in range(3):
            cx_dot = bso_x + 70 + i * (dot_r * 2 + 6)
            if balls is not None and i < balls:
                fill_c = bso_color("balls", balls)
            else:
                fill_c = "#2c3e50"
            self.canvas.create_oval(cx_dot - dot_r, top_of_bso - spacing - dot_r, cx_dot + dot_r, top_of_bso - spacing + dot_r,
                                    fill=fill_c, outline="white")
        self.canvas.create_text(bso_x, top_of_bso + spacing, text="STRIKES", font=self.font_small, fill=self.fg, anchor="w")
        for i in range(2):
            cx_dot = bso_x + 70 + i * (dot_r * 2 + 6)
            if strikes is not None and i < strikes:
                fill_c = bso_color("strikes", strikes)
            else:
                fill_c = "#2c3e50"
            self.canvas.create_oval(cx_dot - dot_r, top_of_bso + spacing - dot_r, cx_dot + dot_r, top_of_bso + spacing + dot_r,
                                    fill=fill_c, outline="white")
        self.canvas.create_text(bso_x, top_of_bso + spacing * 3, text="OUTS", font=self.font_small, fill=self.fg, anchor="w")
        # draw only two outs visually
        for i in range(2):
            cx_dot = bso_x + 70 + i * (dot_r * 2 + 6)
            if outs is not None and i < outs:
                fill_c = bso_color("outs", outs)
            else:
                fill_c = "#2c3e50"
            self.canvas.create_oval(
                cx_dot - dot_r,
                top_of_bso + spacing * 3 - dot_r,
                cx_dot + dot_r,
                top_of_bso + spacing * 3 + dot_r,
                fill=fill_c,
                outline="white",
            )

        # continue layout placement after OUTS
        pb_x = bso_x
        pb_y = top_of_bso + spacing * 5
        self.canvas.create_text(pb_x, pb_y, text=self.current_pitcher, font=self.font_small, fill=self.fg, anchor="w")
        self.canvas.create_text(pb_x, pb_y + 18, text=self.current_batter, font=self.font_small, fill=self.fg, anchor="w")

        footer_y = self.height - 24
        footer_text = ""
        is_live_now = False
        
        # Format the time display for the footer
        time_display = self.format_seconds_to_dhms_string(self.next_update_in)
        
        if self.live_feed:
            state = self.live_feed.get("gameData", {}).get("status", {}).get("detailedState", "") or ""
            if "In Progress" in state or "Live" in state:
                is_live_now = True
        if is_live_now:
            r = 6
            cx = 120
            cy = footer_y
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill="red", outline="")
            self.canvas.create_text(cx + 14, cy, text="LIVE", font=self.font_small, fill="red", anchor="w")
            footer_text = f"Next update in: {time_display}"
        else:
            if self.next_game and "gameDate_dt" in self.next_game:
                dt = self.next_game["gameDate_dt"]
                away_n = get_team_name(self.next_game["teams"]["away"])
                home_n = get_team_name(self.next_game["teams"]["home"])
                try:
                    footer_text = f"Next: {away_n} @ {home_n} {dt.strftime('%a %b %d, %I:%M %p %Z')} | Next update in: {time_display}"
                except Exception:
                    # Using f-string for robustness
                    footer_text = f"Next: {away_n} @ {home_n} | Next update in: {time_display}"
            else:
                footer_text = f"Waiting for game data for {self.followed_team_name} | Next update in: {time_display}"
        
        self.canvas.create_text(self.width // 2, footer_y, text=footer_text, font=self.font_small, fill=self.fg, tags="footer")
        
        self.balls = balls
        self.strikes = strikes
        self.outs = outs

    def start_fade(self, base_key, team_color, duration_ms=600, steps=8):
        start = self.empty_base_fill
        end = team_color or self.accent
        step_ms = max(20, int(duration_ms / steps))
        anim = {"step": 0, "steps": steps, "start": start, "end": end, "current": start, "finished": False}
        self.bases[base_key]["anim"] = anim

        def _step():
            s = anim["step"]
            t = s / float(anim["steps"])
            anim["current"] = blend_colors(anim["start"], anim["end"], t)
            self.render(full=False)
            anim["step"] += 1
            if anim["step"] <= anim["steps"]:
                # Always schedule GUI updates using self.root.after in animation
                self.root.after(step_ms, _step)
            else:
                anim["finished"] = True
                anim["current"] = anim["end"]
                self.render(full=False)

        self.root.after(0, _step)

    def update_loop(self):
        # Using executor.submit to manage the thread
        if self.next_update_in <= 0 and not self.running_fetch:
            self.running_fetch = True # Flag set before submission
            # Submit to ThreadPoolExecutor
            self.executor.submit(self.fetch_and_schedule)
            
        if self.next_update_in > 0:
            self.next_update_in -= 1
        
        # only log B/S/O changes to avoid per-second spam
        if not hasattr(self, "_last_log_state") or self._last_log_state != (self.balls, self.strikes, self.outs):
            if self.debug:
                self.log(f"State counts — B:{self.balls} S:{self.strikes} O:{self.outs}", verbose=True)
            self._last_log_state = (self.balls, self.strikes, self.outs)
            
        # Partial render for base fade animation and footer update
        self.render(full=False)
        self.root.after(1000, self.update_loop)

    def fetch_and_schedule(self):
        # This function runs in a background thread
        try:
            games = fetch_schedule(self.team_id)
            self.games = games
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            live_game = None
            last_game = None
            next_game = None
            for g in games:
                gd = g.get("gameDate_dt")
                state = g.get("status", {}).get("detailedState", "") or ""
                # Find the most recent "finished" game or the current "live" game
                if gd and state in ("In Progress", "Final", "Game Over") and gd.astimezone(datetime.timezone.utc) <= now_utc:
                    last_game = g
                # Find the *next* scheduled game (since games are sorted, first match is the next)
                if gd and gd.astimezone(datetime.timezone.utc) >= now_utc and not next_game:
                    next_game = g
                # Identify the single currently live game
                if state == "In Progress":
                    live_game = g

            self.last_game = last_game
            self.next_game = next_game
            self.live_game = live_game

            if self.next_game and "gameDate_dt" in self.next_game:
                try:
                    self.next_game["gameDate_dt"] = self.next_game["gameDate_dt"].astimezone()
                except Exception:
                    pass

            chosen = live_game or last_game
            prev_base_runners = {k: (self.bases[k]["occupied"], self.bases[k]["team"]) for k in self.bases}
            
            if chosen:
                feed = fetch_live_feed(chosen.get("gamePk"))
                self.live_feed = feed
            else:
                self.live_feed = None

            if self.live_feed:
                try:
                    current_play = self.live_feed.get("liveData", {}).get("plays", {}).get("currentPlay", {}) or {}
                    matchup = current_play.get("matchup", {}) or {}
                    batter = matchup.get("batter", {}).get("fullName")
                    pitcher = matchup.get("pitcher", {}).get("fullName")
                    self.current_batter = f"Batter: {batter}" if batter else "Batter: -"
                    self.current_pitcher = f"Pitcher: {pitcher}" if pitcher else "Pitcher: -"
                except Exception:
                    self.current_batter = "Batter: -"
                    self.current_pitcher = "Pitcher: -"
                
                # Reset base state, then populate from live data (using linescore as source of truth for base occupancy)
                for k in self.bases:
                    self.bases[k]["occupied"] = False
                    self.bases[k]["team"] = None

                # Process currentPlay.runners for *movement/animations*
                try:
                    current_play = self.live_feed.get("liveData", {}).get("plays", {}).get("currentPlay", {}) or {}
                    runners_in_play = current_play.get("runners") or current_play.get("baseRunners") or []
                    
                    def to_key(v):
                        if not v: return None
                        s = str(v).lower()
                        if "first" in s or "1b" in s or s == "1": return "1B"
                        if "second" in s or "2b" in s or s == "2": return "2B"
                        if "third" in s or "3b" in s or s == "3": return "3B"
                        if "home" in s or "plate" in s: return "Home"
                        return None
                        
                    for r in runners_in_play:
                        if not isinstance(r, dict): continue
                        
                        team_name = (r.get("team") or {}).get("name") if isinstance(r.get("team"), dict) else r.get("team")
                        color = team_color_for(team_name)[1] if team_name else self.accent
                        
                        mv = r.get("movement") or {}
                        sk = to_key(mv.get("start"))
                        ek = to_key(mv.get("end"))
                        
                        if sk and ek:
                            # Schedule runner movement animation on the main thread
                            self.root.after(0, lambda s=sk, e=ek, c=color: self.move_runner_base(s, e))
                        elif ek and ek != "Home":
                            # Runner appeared (e.g., batter on 1B), spawn if not there
                            if ek not in self.runners_by_base:
                                self.root.after(0, lambda e=ek, c=color: self.spawn_runner_at_base(e, color=c))

                except Exception:
                    if DEBUG:
                        print("[DEBUG] Error processing currentPlay.runners for animations.", threading.get_ident())

                # Use linescore.offense for base *occupancy* (required for the diamond fill)
                try:
                    ls_off = self.live_feed.get("liveData", {}).get("linescore", {}).get("offense", {}) or {}
                    for key, bkey in (("first", "1B"), ("second", "2B"), ("third", "3B")):
                        ent = ls_off.get(key)
                        if ent:
                            self.bases[bkey]["occupied"] = True
                            t = ent.get("team") or {}
                            self.bases[bkey]["team"] = t.get("name") if isinstance(t, dict) else t
                except Exception:
                    if DEBUG:
                        print("[DEBUG] Error processing linescore.offense for base occupancy.", threading.get_ident())
                
                # Check occupancy changes to trigger fade animation and static runner spawn (if not animated)
                for b in ("1B", "2B", "3B"):
                    was_occ, was_team = prev_base_runners[b]
                    now_occ = self.bases[b]["occupied"]
                    now_team = self.bases[b]["team"]
                    
                    if now_occ and not was_occ:
                        # Runner appeared: trigger base fade and ensure a runner icon exists
                        team_col = team_color_for(now_team)[0] if now_team else self.accent
                        runner_col = team_color_for(now_team)[1] if now_team else self.accent
                        
                        # Schedule fade animation and runner spawn on the main thread
                        self.root.after(0, lambda b=b, c=team_col: self.start_fade(b, c))
                        if b not in self.runners_by_base:
                             self.root.after(0, lambda b=b, c=runner_col: self.spawn_runner_at_base(b, color=c))
                             
                    if not now_occ and was_occ:
                        # Runner disappeared: clear the runner icon on the main thread
                        if b in self.runners_by_base:
                            rkey = self.runners_by_base.pop(b, None)
                            if rkey:
                                info = self.runners.pop(rkey, None)
                                if info:
                                    self.root.after(0, lambda c=info.get("cid"): self.canvas.delete(c))
                        # Clear base animation state
                        self.bases[b]["anim"] = None


                now = time.time()
                if now - self._last_poll_time > 5:
                    self.log("Successfully polled feed and updated state", verbose=True)
                    self._last_poll_time = now
            else:
                self.current_batter = "Batter: -"
                self.current_pitcher = "Pitcher: -"
                for k in self.bases:
                    self.bases[k]["occupied"] = False
                    self.bases[k]["team"] = None
                    self.bases[k]["anim"] = None
                self.root.after(0, self.clear_all_runners)

            if live_game:
                self.poll_interval = self.polling.get("live", 15)
            elif next_game and next_game.get("gameDate_dt"):
                # Smart polling: calculate time until next scheduled game
                dt_next = next_game["gameDate_dt"].astimezone()
                dt_now = datetime.datetime.now(dt_next.tzinfo)
                time_to_next = (dt_next - dt_now).total_seconds()

                # Define poll rate bounds
                min_poll = self.polling.get("scheduled", 300)  # 5 minutes
                one_hour = 3600                             # 1 hour
                
                if time_to_next <= 0:
                    # Game is overdue or starting immediately
                    self.poll_interval = self.polling.get("live", 15)
                elif time_to_next > one_hour:
                    # Game is more than 1 hour away: Wait until the 1-hour mark.
                    wait_interval = time_to_next - one_hour
                    self.poll_interval = int(wait_interval)
                else:
                    # Game is 1 hour or less away: Switch to frequent polling (min_poll rate)
                    # This ensures a continuous countdown in the final hour.
                    self.poll_interval = min_poll 
                    
                if self.debug and self.poll_interval != self.polling.get("live", 15):
                    self.log(f"Next game in: {self.format_seconds_to_dhms_string(time_to_next)} ({time_to_next:.0f}s). Smart poll interval set to: {self.poll_interval}s.", verbose=True)
                    
            else:
                # No next game found in the schedule lookahead window
                self.poll_interval = self.polling.get("none", 3600)

            self.next_update_in = self.poll_interval
            
            # Schedule the full GUI render on the main thread
            self.root.after(0, self.render_full_gui)
            
        finally:
            self.running_fetch = False

    def reset_after_third_out(self):
        # This function must be safe to call from either thread, but only performs state changes
        for b in ("1B", "2B", "3B"):
            self.bases[b]["occupied"] = False
            self.bases[b]["team"] = None
            self.bases[b]["anim"] = None
        self.clear_all_runners()
        self._outs_reset_pending = False
        self._inning_reset_done = True
        self.log("Bases and runners cleared after 3rd out", level="info")
        # Ensure a render happens to show the cleared bases
        self.root.after(0, self.render_full_gui)

# Entrypoint
def main():
    root = tk.Tk()
    # --- Ctrl+C Signal Handler ---
    def sigint_handler(signum, frame):
        """Handles SIGINT (Ctrl+C) for clean exit."""
        print("\n\n[INFO] Caught Ctrl+C. Shutting down gracefully...")
        # Check app.running_fetch state before quitting
        if app.running_fetch:
            print("[INFO] Waiting for ongoing fetch thread to finish...")
        root.quit()

    signal.signal(signal.SIGINT, sigint_handler)

    root.title("MLB Canvas Scoreboard (final v8)")
    app = ScoreboardApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
