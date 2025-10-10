#!/usr/bin/env python3
"""
mlbscore_with_config_colors.py

Scoreboard with:
- Base / B/S/O logic (never showing “3” for strikes/balls, brief outs=3 then reset)
- Runner circle animations
- Team colors loaded from config.json (team_colors) instead of hardcoded
"""

import tkinter as tk
from tkinter import font as tkfont
import threading
import requests
import json
import datetime
import pathlib
import argparse
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from copy import deepcopy

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
parser = argparse.ArgumentParser(description="MLB Canvas Scoreboard (patched)")
parser.add_argument("--config", default="config.json", help="Path to config.json")
parser.add_argument("--team", help="Team name (overrides config team_id if found)")
parser.add_argument("--debug", action="store_true", help="Enable debug logging")
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
        data = json.loads(p.read_text())
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
if args.debug:
    CONFIG["debug"] = True

TEAM_ID = CONFIG.get("team_id")
POLLING = CONFIG.get("polling_intervals", {"live": 15, "scheduled": 300, "none": 3600})
LOOKAHEAD_DAYS = CONFIG.get("lookahead_days", 7)
CANVAS_CFG = CONFIG.get("canvas", {})
UI_CFG = CONFIG.get("ui", {})
DEBUG = CONFIG.get("debug", False)
TEAM_COLORS = CONFIG.get("team_colors", {})

# CLI --team override (if team name maps to id in config)
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
    s.headers.update({"User-Agent": "mlbscore-patched/1.0"})
    return s

def parse_iso_to_local(dtstr):
    if not dtstr:
        return None
    try:
        dt = datetime.datetime.fromisoformat(dtstr.replace("Z", "+00:00"))
        return dt.astimezone()
    except Exception:
        return None

def fetch_schedule(team_id=TEAM_ID, lookahead=LOOKAHEAD_DAYS):
    sess = make_session()
    today = datetime.datetime.now(datetime.timezone.utc).date()
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
            print("[DEBUG] fetch_schedule error:", e)
        return []
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            gd = parse_iso_to_local(g.get("gameDate"))
            if gd:
                g["gameDate_dt"] = gd
            games.append(g)
    # sort with missing dates last
    return sorted(games, key=lambda g: g.get("gameDate_dt") or datetime.datetime.max)

def fetch_live_feed(gamePk):
    if not gamePk:
        return None
    sess = make_session()
    url = f"https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"
    try:
        r = sess.get(url, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if DEBUG:
            print("[DEBUG] fetch_live_feed error:", e)
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
    """Return (primary, accent) from config team_colors; fallback to canvas settings."""
    if not name:
        return (CANVAS_CFG.get("bg_color", "#000000"), CANVAS_CFG.get("accent", "#FFFFFF"))
    tc = TEAM_COLORS.get(name)
    if isinstance(tc, dict):
        prim = tc.get("primary", CANVAS_CFG.get("bg_color", "#000000"))
        acc = tc.get("accent", CANVAS_CFG.get("accent", "#FFFFFF"))
        return (prim, acc)
    # case-insensitive fallback
    for k, v in TEAM_COLORS.items():
        if k.lower() == name.lower() and isinstance(v, dict):
            return (v.get("primary", CANVAS_CFG.get("bg_color")), v.get("accent", CANVAS_CFG.get("accent")))
    # fallback defaults
    return (CANVAS_CFG.get("bg_color", "#000000"), CANVAS_CFG.get("accent", "#FFFFFF"))

def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

def rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, int(x))) for x in rgb])

def blend_colors(c1, c2, t):
    # linear blend c1->c2 by t in [0,1]
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    return rgb_to_hex((r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t))

# -------------------------
# GUI App
# -------------------------
class ScoreboardApp:
    def __init__(self, root):
        self.root = root
        self.team_id = TEAM_ID
        self.polling = POLLING
        self.debug = DEBUG

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
        self.runners = {}  # rkey -> {"cid","base","color"}
        self.runners_by_base = {}  # base -> rkey
        self._next_runner_key = 1

        self.current_batter = "Batter: -"
        self.current_pitcher = "Pitcher: -"

        # followed team display name if mapping present
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

        # limited debug trackers (so we only log key events)
        self._last_poll_time = 0
        self._last_runner_state = {}

    def log(self, *args, verbose=False, level="info"):
        """Reduced-verbosity logger: prints DEBUG messages only with --debug.
           INFO appears only when --debug is on or for explicit errors."""
        if verbose:
            if not self.debug:
                return
            print("[DEBUG]", *args)
        else:
            if level and str(level).lower() == "error":
                print(f"[ERROR]", *args)
                return
            if self.debug:
                print(f"[{str(level).upper()}]", *args)

    # ---------- runner helpers ----------
    def compute_base_positions(self):
        """Compute base coordinates using current layout variables. Called during render."""
        ds = self.diamond_ds or 120
        cx = self.diamond_cx or (self.left_margin + 180 if self.left_margin else 300)
        cy = self.diamond_cy or (self.top_margin + 300 if self.top_margin else 300)
        inset = ds * 0.6
        self.base_positions = {
            "2B": (cx, cy - inset),
            "1B": (cx + inset, cy),
            "3B": (cx - inset, cy),
            "Home": (cx, cy + inset)
        }

    def spawn_runner_at_base(self, base_key, color=None):
        """Place a runner circle at base_key if not already occupied."""
        if base_key in self.runners_by_base:
            return None
        self.compute_base_positions()
        pos = self.base_positions.get(base_key)
        if pos is None:
            return None
        bx, by = pos
        color = color or self.accent
        rkey = f"r{self._next_runner_key}"
        self._next_runner_key += 1
        cid = self.canvas.create_oval(bx - 8, by - 8, bx + 8, by + 8,
                                      fill=color, outline="white", width=2)
        self.runners[rkey] = {"cid": cid, "base": base_key, "color": color}
        self.runners_by_base[base_key] = rkey
        # verbose debug when requested
        self.log(f"Runner spawned: {rkey} at {base_key}", verbose=True)
        return rkey

    def move_runner_base(self, from_base, to_base, steps=12):
        """Animate a runner moving from from_base to to_base.
           If no runner exists at from_base, spawn one at to_base (defensive).
        """
        if from_base not in self.runners_by_base:
            # nothing to move; spawn at destination to reflect occupancy
            self.log(f"Move requested from {from_base} but none present; spawning at {to_base}", verbose=True)
            return self.spawn_runner_at_base(to_base, color=self.accent)

        rkey = self.runners_by_base.pop(from_base)
        runner = self.runners.get(rkey)
        if not runner:
            return None

        # prepare positions
        self.compute_base_positions()
        start = self.base_positions.get(from_base)
        end = self.base_positions.get(to_base)
        if not start or not end:
            # fallback: simply set runner to to_base
            try:
                self.canvas.delete(runner["cid"])
            except Exception:
                pass
            new_key = self.spawn_runner_at_base(to_base, color=runner.get("color"))
            # remove old
            self.runners.pop(rkey, None)
            return new_key

        sx, sy = start
        tx, ty = end
        dx = (tx - sx) / float(steps)
        dy = (ty - sy) / float(steps)

        # create temporary moving dot
        color = runner.get("color", self.accent)
        temp_cid = self.canvas.create_oval(sx - 8, sy - 8, sx + 8, sy + 8, fill=color, outline="white", width=2)
        # delete static one
        try:
            self.canvas.delete(runner["cid"])
        except Exception:
            pass

        # remove runner from registry; we'll insert back when movement completes (unless scoring at Home)
        self.runners.pop(rkey, None)

        def _step(i=0):
            if i >= steps:
                # end position reached
                self.canvas.delete(temp_cid)
                if to_base != "Home":
                    # place static runner at destination
                    new_key = self.spawn_runner_at_base(to_base, color=color)
                    self.log(f"Runner moved: {rkey} {from_base} -> {to_base} as {new_key}", verbose=True)
                    return
                else:
                    # scored: no re-insertion; animate shrink then delete
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
            # move temp dot
            try:
                self.canvas.move(temp_cid, dx, dy)
            except Exception:
                pass
            self.root.after(30, lambda: _step(i + 1))

        _step()
        return rkey

    def clear_all_runners(self):
        """Remove all runner icons and reset registries."""
        for rkey, info in list(self.runners.items()):
            try:
                self.canvas.delete(info.get("cid"))
            except Exception:
                pass
        self.runners.clear()
        self.runners_by_base.clear()
        self.log("All runners cleared", verbose=True)

    # ---------- rendering ----------
    def render(self, full=True):
        if full:
            self.canvas.delete("all")
        else:
            self.canvas.delete("footer")

        # choose data source
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
            footer = f"{msg} | Next update in: {self.next_update_in}s"
            self.canvas.create_text(self.width // 2, self.height - 20,
                                    text=footer, font=self.font_small, fill=self.accent, tags="footer")
            return

        away = get_team_name(game_src.get("teams", {}).get("away", {}))
        home = get_team_name(game_src.get("teams", {}).get("home", {}))
        innings = linescore.get("innings", []) if linescore else []
        max_innings = max(len(innings), UI_CFG.get("max_innings", 9))

        # layout params
        left_margin = self.left_margin
        top_margin = self.top_margin
        team_x = left_margin
        score_start_x = self.score_start_x
        col_width = self.col_width
        row_height = self.row_height

        # title
        title_text = f"{self.followed_team_name} — MLB Scoreboard"
        self.canvas.create_text(self.width // 2, 22, text=title_text, font=self.font_title, fill=self.accent)

        # header row + inning cells
        self.canvas.create_rectangle(team_x - 8, top_margin - 18, score_start_x - 4, top_margin + 18,
                                     fill=self.bg, outline="black")
        self.canvas.create_text(team_x, top_margin, text="TEAM", font=self.font_header, fill=self.accent, anchor="w")

        for i in range(max_innings):
            x_center = score_start_x + i * col_width
            self.canvas.create_rectangle(x_center - col_width // 2, top_margin - 18,
                                         x_center + col_width // 2, top_margin + 18,
                                         fill=self.bg, outline="black")
            self.canvas.create_text(x_center, top_margin, text=str(i + 1), font=self.font_header, fill=self.accent)

        for j, label in enumerate(("R", "H", "E")):
            x_center = score_start_x + (max_innings + j) * col_width
            self.canvas.create_rectangle(x_center - col_width // 2, top_margin - 18,
                                         x_center + col_width // 2, top_margin + 18,
                                         fill=self.bg, outline="black")
            self.canvas.create_text(x_center, top_margin, text=label, font=self.font_header, fill=self.accent)

        # draw team rows (colored)
        def draw_team_row(y, name, side):
            bg_col, fg_col = team_color_for(name)
            # team cell
            self.canvas.create_rectangle(team_x - 8, y - 18, score_start_x - 4, y + 18, fill=bg_col, outline="black")
            self.canvas.create_text(team_x, y, text=name, font=self.font_team, fill=fg_col, anchor="w")
            # innings
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
                self.canvas.create_rectangle(x1, y - 18, x2, y + 18, fill=bg_col, outline="black")
                self.canvas.create_text(score_start_x + i * col_width, y, text=str(run_val), font=self.font_team,
                                        fill=fg_col)
            # totals R/H/E
            totals = linescore.get("teams", {}).get(side, {})
            for j, key in enumerate(("runs", "hits", "errors")):
                val = str(totals.get(key, "-"))
                x_center = score_start_x + (max_innings + j) * col_width
                self.canvas.create_rectangle(x_center - col_width // 2, y - 18, x_center + col_width // 2, y + 18,
                                             fill=bg_col, outline="black")
                self.canvas.create_text(x_center, y, text=val, font=self.font_team, fill=fg_col)

        y_away = top_margin + row_height
        y_home = y_away + row_height
        draw_team_row(y_away, away, "away")
        draw_team_row(y_home, home, "home")

        # --- Clean, properly aligned grid overlay ---
        grid_left = score_start_x - col_width // 2
        grid_top = top_margin - 18
        grid_right = score_start_x + (max_innings + 3) * col_width + col_width // 2
        grid_bottom = grid_top + row_height * 2  # header + away + home rows

        # subtle vertical dividers
        for i in range(max_innings + 4):
            x = score_start_x + (i - 0.5) * col_width
            self.canvas.create_line(x, grid_top, x, grid_bottom, fill="#38444d", width=1)

        # thin horizontal separators
        for j in range(3):
            y = grid_top + (j + 1) * row_height
            self.canvas.create_line(grid_left, y, grid_right, y, fill="#38444d", width=1)

        # outer border
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
                if b.get("occupied") and b.get("team"):
                    fill = team_color_for(b["team"])[0]
            pts = [bx, by - base_half, bx + base_half, by, bx, by + base_half, bx - base_half, by]
            self.canvas.create_polygon(pts, fill=fill, outline="white", width=2)
            self.canvas.create_text(bx, by, text=bname, font=self.font_small, fill=self.fg)

        # Draw runner icons (static ones) on bases
        for base_key, rkey in list(self.runners_by_base.items()):
            info = self.runners.get(rkey)
            if not info:
                continue
            pos = self.base_positions.get(base_key)
            if not pos:
                continue
            bx, by = pos
            # draw a filled circle for the runner (slightly larger to be visible)
            self.canvas.create_oval(bx - 8, by - 8, bx + 8, by + 8, fill=info.get("color", self.accent),
                                    outline="white", width=2)

        # B/S/O to the right of the diamond (centered vertically next to diamond)
        bso_x = self.diamond_cx + ds + 120
        # compute B/S/O values based on live_feed (display caps applied)
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
            # display rules:
            # balls: show 0..3 (reset to 0 when raw_balls >=4)
            balls = 0 if raw_balls >= 4 else max(0, min(3, raw_balls))
            # strikes: show 0..2 (reset to 0 when raw_strikes >=3)
            strikes = 0 if raw_strikes >= 3 else max(0, min(2, raw_strikes))
            # outs handling: show 3 briefly then reset via reset_after_third_out()
            if raw_outs >= 3:
                outs = 3
                if not self._outs_reset_pending:
                    self._outs_reset_pending = True
                    # schedule reset after short delay so "3" is visible briefly
                    self.root.after(1000, self.reset_after_third_out)
            else:
                outs = max(0, min(2, raw_outs))
                self._outs_reset_pending = False
            self._last_outs = raw_outs
        else:
            balls = strikes = outs = None

        # function to pick dot color instantly
        def bso_color(kind, value):
            # kind: 'balls'/'strikes'/'outs'; value is integer count or None
            if value is None:
                return "#7f8c8d"  # muted gray for unknown
            if kind == "balls":
                if value == 0:
                    return "#00a651"  # green
                elif 1 <= value <= 2:
                    return "#f1c40f"  # yellow
                else:  # 3
                    return "#e74c3c"  # red
            elif kind == "strikes":
                if value == 0:
                    return "#00a651"
                elif value == 1:
                    return "#f1c40f"
                else:  # 2
                    return "#e74c3c"
            elif kind == "outs":
                if value == 0:
                    return "#00a651"
                elif 1 <= value <= 2:
                    return "#f1c40f"
                else:  # 3 (brief)
                    return "#e74c3c"
            return "#7f8c8d"

        # Draw vertical BSO block (labels and colored dots)
        dot_r = 8
        spacing = 28
        top_of_bso = self.diamond_cy - spacing
        # Balls row
        self.canvas.create_text(bso_x, top_of_bso - spacing, text="BALLS", font=self.font_small, fill=self.fg, anchor="w")
        for i in range(3):
            cx_dot = bso_x + 70 + i * (dot_r * 2 + 6)
            if balls is not None and i < balls:
                fill_c = bso_color("balls", balls)
            else:
                fill_c = "#2c3e50"
            self.canvas.create_oval(cx_dot - dot_r, top_of_bso - spacing - dot_r, cx_dot + dot_r, top_of_bso - spacing + dot_r,
                                    fill=fill_c, outline="white")
        # Strikes row
        self.canvas.create_text(bso_x, top_of_bso + spacing, text="STRIKES", font=self.font_small, fill=self.fg, anchor="w")
        for i in range(2):
            cx_dot = bso_x + 70 + i * (dot_r * 2 + 6)
            if strikes is not None and i < strikes:
                fill_c = bso_color("strikes", strikes)
            else:
                fill_c = "#2c3e50"
            self.canvas.create_oval(cx_dot - dot_r, top_of_bso + spacing - dot_r, cx_dot + dot_r, top_of_bso + spacing + dot_r,
                                    fill=fill_c, outline="white")
        # Outs row
        self.canvas.create_text(bso_x, top_of_bso + spacing * 3, text="OUTS", font=self.font_small, fill=self.fg, anchor="w")
        for i in range(3):
            cx_dot = bso_x + 70 + i * (dot_r * 2 + 6)
            if outs is not None and i < outs:
                fill_c = bso_color("outs", outs)
            else:
                fill_c = "#2c3e50"
            self.canvas.create_oval(cx_dot - dot_r, top_of_bso + spacing * 3 - dot_r, cx_dot + dot_r, top_of_bso + spacing * 3 + dot_r,
                                    fill=fill_c, outline="white")

        # Pitcher / Batter under BSO block (centered under BSO area)
        pb_x = bso_x
        pb_y = top_of_bso + spacing * 5
        self.canvas.create_text(pb_x, pb_y, text=self.current_pitcher, font=self.font_small, fill=self.fg, anchor="w")
        self.canvas.create_text(pb_x, pb_y + 18, text=self.current_batter, font=self.font_small, fill=self.fg, anchor="w")

        # Footer
        footer_y = self.height - 24
        footer_text = ""
        is_live_now = False
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
            footer_text = f"Next update in: {self.next_update_in}s"
        else:
            if self.next_game and "gameDate_dt" in self.next_game:
                dt = self.next_game["gameDate_dt"]
                away_n = get_team_name(self.next_game["teams"]["away"])
                home_n = get_team_name(self.next_game["teams"]["home"])
                footer_text = f"Next: {away_n} @ {home_n} {dt.strftime('%a %b %d, %I:%M %p %Z')} | Next update in: {self.next_update_in}s"
            else:
                footer_text = f"Waiting for game data for {self.followed_team_name} | Next update in: {self.next_update_in}s"
        self.canvas.create_text(self.width // 2, footer_y, text=footer_text, font=self.font_small, fill=self.fg, tags="footer")

    # ---------- base fade animation ----------
    def start_fade(self, base_key, team_color, duration_ms=600, steps=8):
        """Start a subtle fade anim from empty_base_fill -> team_color."""
        start = self.empty_base_fill
        end = team_color or self.accent
        step_ms = max(20, int(duration_ms / steps))
        anim = {"step": 0, "steps": steps, "start": start, "end": end, "current": start, "finished": False}
        self.bases[base_key]["anim"] = anim

        def _step():
            s = anim["step"]
            t = s / float(anim["steps"])
            anim["current"] = blend_colors(anim["start"], anim["end"], t)
            # partial redraw is fine (full=False)
            self.render(full=False)
            anim["step"] += 1
            if anim["step"] <= anim["steps"]:
                self.root.after(step_ms, _step)
            else:
                anim["finished"] = True
                anim["current"] = anim["end"]
                # leave final color
                self.render(full=False)

        self.root.after(0, _step)

    # ---------- update loop ----------
    def update_loop(self):
        if self.next_update_in <= 0 and not self.running_fetch:
            # start a background poll
            threading.Thread(target=self.fetch_and_schedule, daemon=True).start()
        if self.next_update_in > 0:
            self.next_update_in -= 1
        # partial redraw so animations and footer update smoothly
        self.render(full=False)
        self.root.after(1000, self.update_loop)

    # ---------- fetch & update ----------
    def fetch_and_schedule(self):
        """Poll schedule and live feed, update internal state and trigger animations."""
        self.running_fetch = True
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
                if gd and state in ("In Progress", "Final", "Game Over") and gd.astimezone(datetime.timezone.utc) <= now_utc:
                    last_game = g
                if gd and gd.astimezone(datetime.timezone.utc) >= now_utc and not next_game:
                    next_game = g
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
            if chosen:
                feed = fetch_live_feed(chosen.get("gamePk"))
                self.live_feed = feed
            else:
                self.live_feed = None

            # update batter & pitcher & bases only on poll
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

                # bases detection
                prev = {k: (self.bases[k]["occupied"], self.bases[k]["team"]) for k in self.bases}
                for k in self.bases:
                    self.bases[k]["occupied"] = False
                    self.bases[k]["team"] = None

                # try currentPlay runners -> detect movements & occupancy
                try:
                    current_play = self.live_feed.get("liveData", {}).get("plays", {}).get("currentPlay", {}) or {}
                    runners = current_play.get("runners") or current_play.get("baseRunners") or []
                    for r in runners:
                        base_val = None
                        if isinstance(r, dict):
                            base_val = r.get("base") or (r.get("start") and r.get("start").get("base")) or r.get("currentBase")
                        base_key = None
                        if isinstance(base_val, str):
                            s = base_val.lower()
                            if "first" in s or "1b" in s or "1" == s: base_key = "1B"
                            elif "second" in s or "2b" in s or "2" == s: base_key = "2B"
                            elif "third" in s or "3b" in s or "3" == s: base_key = "3B"
                            elif "home" in s or "homeplate" in s: base_key = "Home"
                        elif isinstance(base_val, int):
                            if base_val == 1: base_key = "1B"
                            elif base_val == 2: base_key = "2B"
                            elif base_val == 3: base_key = "3B"

                        team_name = None
                        if isinstance(r.get("team"), dict):
                            team_name = r.get("team").get("name")
                        elif isinstance(r.get("team"), str):
                            team_name = r.get("team")

                        if base_key:
                            self.bases[base_key]["occupied"] = True
                            if team_name:
                                self.bases[base_key]["team"] = team_name

                    # detect explicit movement events for animation
                    for r in runners:
                        if not isinstance(r, dict):
                            continue
                        mv = r.get("movement") or {}
                        start = mv.get("start")
                        end = mv.get("end")
                        if start or end:
                            def to_key(v):
                                if not v:
                                    return None
                                s = str(v).lower()
                                if "first" in s or "1b" in s or s == "1": return "1B"
                                if "second" in s or "2b" in s or s == "2": return "2B"
                                if "third" in s or "3b" in s or s == "3": return "3B"
                                if "home" in s or "plate" in s: return "Home"
                                return None
                            sk = to_key(start)
                            ek = to_key(end)
                            team_name = (r.get("team") or {}).get("name") if isinstance(r.get("team"), dict) else r.get("team")
                            color = team_color_for(team_name)[1] if team_name else self.accent
                            if sk and ek:
                                # move runner from sk -> ek (animate)
                                if sk not in self.runners_by_base:
                                    self.spawn_runner_at_base(sk, color=color)
                                self.move_runner_base(sk, ek)
                            elif ek:
                                # only end provided, ensure a runner exists at ek
                                if ek not in self.runners_by_base:
                                    self.spawn_runner_at_base(ek, color=color)
                except Exception:
                    if DEBUG:
                        print("[DEBUG] Error processing currentPlay.runners", exc_info=True)

                # fallback: linescore.offense
                if not any(self.bases[k]["occupied"] for k in self.bases):
                    try:
                        ls_off = self.live_feed.get("liveData", {}).get("linescore", {}).get("offense", {}) or {}
                        for key, bkey in (("first", "1B"), ("second", "2B"), ("third", "3B")):
                            ent = ls_off.get(key)
                            if ent:
                                self.bases[bkey]["occupied"] = True
                                t = ent.get("team") or {}
                                if isinstance(t, dict):
                                    self.bases[bkey]["team"] = t.get("name")
                                else:
                                    self.bases[bkey]["team"] = t
                    except Exception:
                        pass

                # ensure runner icons reflect bases: spawn for newly occupied bases, remove when empty
                for b in ("1B", "2B", "3B"):
                    was_occ, was_team = prev[b]
                    now_occ = self.bases[b]["occupied"]
                    now_team = self.bases[b]["team"]
                    if now_occ and (not was_occ or (was_team and now_team != was_team)):
                        team_col = team_color_for(now_team)[0] if now_team else self.accent
                        self.start_fade(b, team_col)
                        if b not in self.runners_by_base:
                            self.spawn_runner_at_base(b, color=team_color_for(now_team)[1] if now_team else self.accent)
                    if not now_occ and was_occ:
                        # base became empty; remove runner there if present
                        if b in self.runners_by_base:
                            rkey = self.runners_by_base.pop(b, None)
                            if rkey:
                                info = self.runners.pop(rkey, None)
                                if info:
                                    try:
                                        self.canvas.delete(info.get("cid"))
                                    except Exception:
                                        pass

                # log once per successful poll (not every render)
                now = time.time()
                if now - self._last_poll_time > 5:
                    self.log("Successfully polled feed and updated state", verbose=True)
                    self._last_poll_time = now
            else:
                # clear state when no feed
                self.current_batter = "Batter: -"
                self.current_pitcher = "Pitcher: -"
                for k in self.bases:
                    self.bases[k]["occupied"] = False
                    self.bases[k]["team"] = None
                    self.bases[k]["anim"] = None
                # clear runners too
                self.clear_all_runners()

            # decide polling interval
            if live_game:
                self.poll_interval = self.polling.get("live", 15)
            elif next_game:
                self.poll_interval = self.polling.get("scheduled", 300)
            elif last_game:
                self.poll_interval = self.polling.get("none", 3600)
            else:
                self.poll_interval = self.polling.get("none", 3600)

            self.next_update_in = self.poll_interval
            # render full after poll
            self.render(full=True)
        finally:
            self.running_fetch = False

    def reset_after_third_out(self):
        """Reset bases and outs after showing 3rd out briefly."""
        for b in ("1B", "2B", "3B"):
            self.bases[b]["occupied"] = False
            self.bases[b]["team"] = None
            self.bases[b]["anim"] = None
        # clear visual runner icons too
        self.clear_all_runners()
        self._outs_reset_pending = False
        self.log("Bases and runners cleared after 3rd out", level="info")

# -------------------------
# Entrypoint
# -------------------------
def main():
    root = tk.Tk()
    root.title("MLB Canvas Scoreboard (final)")
    app = ScoreboardApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
