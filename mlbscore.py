#!/usr/bin/env python3
"""
mlbscore.py - Classic TV-style MLB Scoreboard (Tkinter Canvas)
Features:
- Reads config.json
- Shows live, last, and next scheduled games
- Inning-by-inning scoreboard with R/H/E
- Polls StatsAPI at smart intervals (15s live, 300s scheduled, 3600s none)
- Updates countdown footer every second
- Diamond and balls/strikes/outs visualization
- Current batter and pitcher info
- Home/Away rows stylized with team colors
- Verbose debug logging with --debug
"""

import tkinter as tk
from tkinter import font as tkfont
import threading
import requests
import json
import datetime
import pathlib
import argparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# -------------------------
# Defaults
# -------------------------
DEFAULT_CONFIG = {
    "team_id": 117,
    "teams": {},
    "polling_intervals": {"live": 15, "scheduled": 300, "none": 3600},
    "lookahead_days": 7,
    "canvas": {
        "width": 1000,
        "height": 600,
        "bg_color": "#000000",
        "fg_color": "#FFFFFF",
        "accent": "#FFD700",
        "font_family": "Courier"
    },
    "ui": {"max_innings": 9},
    "debug": False
}

# Team colors (bg, fg)
TEAM_COLORS = {
    "Detroit Tigers": ("#003b5c", "#fa4616"),
    "New York Yankees": ("#003b5c", "#e4002b"),
    "Cleveland Guardians": ("#00385d", "#e31937"),
    "Boston Red Sox": ("#bd3039", "#0c2340"),
    "Chicago Cubs": ("#0e3386", "#cc3433"),
    "Los Angeles Dodgers": ("#005a8d", "#ef3e42"),
    "Houston Astros": ("#002d62", "#eb6e1f"),
    "San Francisco Giants": ("#fdba12", "#27251f"),
    # … (add more as needed)
}

# -------------------------
# CLI
# -------------------------
parser = argparse.ArgumentParser(description="MLB Canvas Scoreboard")
parser.add_argument("--config", default="config.json", help="Path to config.json")
parser.add_argument("--team", help="Team name (overrides config team_id if found)")
parser.add_argument("--debug", action="store_true", help="Enable debug logging")
args = parser.parse_args()

# -------------------------
# Config loader
# -------------------------
def load_config(path):
    cfg = DEFAULT_CONFIG.copy()
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

TEAM_ID = CONFIG["team_id"]
if args.team and args.team in CONFIG.get("teams", {}):
    TEAM_ID = CONFIG["teams"][args.team]

POLLING = CONFIG["polling_intervals"]
LOOKAHEAD_DAYS = CONFIG["lookahead_days"]
CANVAS_CFG = CONFIG["canvas"]
UI_CFG = CONFIG["ui"]
DEBUG = CONFIG["debug"]

# -------------------------
# Networking
# -------------------------
def _make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(['GET']))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": "mlbscore-canvas/1.0"})
    return s

def _parse_dt(dtstr):
    try:
        dt = datetime.datetime.fromisoformat(dtstr.replace("Z", "+00:00"))
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None

def fetch_schedule(team_id=TEAM_ID, lookahead=LOOKAHEAD_DAYS):
    session = _make_session()
    today = datetime.datetime.now(datetime.timezone.utc).date()
    start = today - datetime.timedelta(days=1)
    end = today + datetime.timedelta(days=lookahead)
    url = "https://statsapi.mlb.com/api/v1/schedule"
    params = {
        "sportId": 1, "teamId": team_id,
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d"),
        "hydrate": "team,linescore"
    }
    try:
        r = session.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        if DEBUG:
            print("[DEBUG] fetch_schedule error:", e)
        return []
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            gd = _parse_dt(g.get("gameDate"))
            if gd:
                g["gameDate_dt"] = gd
                games.append(g)
    return sorted(games, key=lambda g: g["gameDate_dt"])

def fetch_live_feed(gamePk):
    if not gamePk:
        return None
    url = f"https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "mlbscore-canvas/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if DEBUG:
            print("[DEBUG] fetch_live_feed error:", e)
        return None

# -------------------------
# Helpers
# -------------------------
def get_team_name(team_entry):
    if not team_entry:
        return "UNKNOWN"
    if isinstance(team_entry, dict):
        if "team" in team_entry and isinstance(team_entry["team"], dict):
            return team_entry["team"].get("name", "UNKNOWN")
        if "name" in team_entry:
            return team_entry["name"]
        if "teamName" in team_entry:
            return team_entry["teamName"]
    return str(team_entry)

# -------------------------
# Scoreboard App
# -------------------------
class ScoreboardApp:
    def __init__(self, root, config):
        self.root = root
        self.team_id = TEAM_ID
        self.polling = POLLING
        self.debug = DEBUG
        self.width = CANVAS_CFG["width"]
        self.height = CANVAS_CFG["height"]
        self.bg = CANVAS_CFG["bg_color"]
        self.fg = CANVAS_CFG["fg_color"]
        self.accent = CANVAS_CFG["accent"]
        self.font_family = CANVAS_CFG["font_family"]

        self.canvas = tk.Canvas(root, width=self.width, height=self.height,
                                bg=self.bg, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.font_title = tkfont.Font(family=self.font_family, size=16, weight="bold")
        self.font_team = tkfont.Font(family=self.font_family, size=14, weight="bold")
        self.font_small = tkfont.Font(family=self.font_family, size=10, weight="bold")
        self.font_status = tkfont.Font(family=self.font_family, size=12, weight="bold")

        self.games = []
        self.last_game = None
        self.next_game = None
        self.live_feed = None
        self.poll_interval = POLLING["none"]
        self.next_update_in = 0
        self.running_fetch = False

        self.root.after(100, self.update_loop)

    # Logging system
    def log(self, *args, verbose=False):
        if verbose and self.debug:
            print("[DEBUG]", *args)
        elif not verbose:
            print("[INFO]", *args)

    def render(self, full=True):
        if not full:
            self.canvas.delete("footer")
            footer_y = self.height - 24
            footer = f"Next update in: {self.next_update_in}s"
            if self.next_game and "gameDate_dt" in self.next_game:
                dt = self.next_game["gameDate_dt"].strftime("%Y-%m-%d %H:%M UTC")
                footer += f" | Next: {get_team_name(self.next_game['teams']['away'])} @ {get_team_name(self.next_game['teams']['home'])} {dt}"
            self.canvas.create_text(20, footer_y, text=footer,
                                    font=self.font_small, fill=self.accent,
                                    anchor="w", tags="footer")
            return

        self.canvas.delete("all")
        game = None
        linescore = {}
        if self.live_feed:
            game = self.live_feed.get("gameData", {})
            linescore = self.live_feed.get("liveData", {}).get("linescore", {})
        elif self.last_game:
            game = self.last_game
            linescore = self.last_game.get("linescore", {})

        if not game:
            self.canvas.create_text(self.width//2, self.height//2,
                                    text="No game data",
                                    font=self.font_title, fill=self.fg)
            return

        away = get_team_name(game.get("teams", {}).get("away", {}))
        home = get_team_name(game.get("teams", {}).get("home", {}))
        innings = linescore.get("innings", [])
        max_innings = max(len(innings), UI_CFG.get("max_innings", 9))

        # Dynamic layout
        longest_name = max(len(away), len(home), len("TEAM"))
        char_width = self.font_team.measure("W")
        team_x = 100
        score_start_x = team_x + longest_name*char_width + 20
        col_width = 40
        y_start = 80
        y_step = 40

        # Header
        self.canvas.create_text(team_x, y_start, text="TEAM",
                                font=self.font_small, fill=self.accent, anchor="w")
        for i in range(max_innings):
            self.canvas.create_text(score_start_x + i*col_width, y_start,
                                    text=str(i+1), font=self.font_small, fill=self.accent)
        self.canvas.create_text(score_start_x + max_innings*col_width, y_start, text="R", font=self.font_small, fill=self.accent)
        self.canvas.create_text(score_start_x + (max_innings+1)*col_width, y_start, text="H", font=self.font_small, fill=self.accent)
        self.canvas.create_text(score_start_x + (max_innings+2)*col_width, y_start, text="E", font=self.font_small, fill=self.accent)

        # Away row
        y_away = y_start + y_step
        away_bg, away_fg = TEAM_COLORS.get(away, (self.bg, self.fg))
        self.canvas.create_rectangle(team_x-5, y_away-15,
                                     score_start_x+(max_innings+3)*col_width+5, y_away+15,
                                     fill=away_bg, outline="")
        self.canvas.create_text(team_x, y_away, text=away,
                                font=self.font_team, fill=away_fg, anchor="w")
        for i in range(max_innings):
            run = "-"
            if i < len(innings) and "away" in innings[i]:
                run = innings[i]["away"].get("runs", "-")
            self.canvas.create_text(score_start_x + i*col_width, y_away,
                                    text=str(run), font=self.font_team, fill=away_fg)
        self.canvas.create_text(score_start_x + max_innings*col_width, y_away,
                                text=str(linescore.get("teams", {}).get("away", {}).get("runs", "-")),
                                font=self.font_team, fill=away_fg)

        # Home row
        y_home = y_start + 2*y_step
        home_bg, home_fg = TEAM_COLORS.get(home, (self.bg, self.fg))
        self.canvas.create_rectangle(team_x-5, y_home-15,
                                     score_start_x+(max_innings+3)*col_width+5, y_home+15,
                                     fill=home_bg, outline="")
        self.canvas.create_text(team_x, y_home, text=home,
                                font=self.font_team, fill=home_fg, anchor="w")
        for i in range(max_innings):
            run = "-"
            if i < len(innings) and "home" in innings[i]:
                run = innings[i]["home"].get("runs", "-")
            self.canvas.create_text(score_start_x + i*col_width, y_home,
                                    text=str(run), font=self.font_team, fill=home_fg)
        self.canvas.create_text(score_start_x + max_innings*col_width, y_home,
                                text=str(linescore.get("teams", {}).get("home", {}).get("runs", "-")),
                                font=self.font_team, fill=home_fg)

        # Diamond
        diamond_center_x = self.width // 2
        diamond_center_y = y_home + y_step + 20
        diamond_size = 20
        self.canvas.create_polygon(
            diamond_center_x, diamond_center_y - diamond_size,
            diamond_center_x + diamond_size, diamond_center_y,
            diamond_center_x, diamond_center_y + diamond_size,
            diamond_center_x - diamond_size, diamond_center_y,
            outline=self.accent, fill="", width=2
        )

        # Balls/Strikes/Outs
        bso_text = "B: -  S: -  O: -"
        if self.live_feed:
            counts = self.live_feed.get("liveData", {}).get("plays", {}).get("currentPlay", {}).get("count", {})
            balls = counts.get("balls", "-")
            strikes = counts.get("strikes", "-")
            outs = self.live_feed.get("liveData", {}).get("linescore", {}).get("outs", "-")
            bso_text = f"B: {balls}  S: {strikes}  O: {outs}"
            self.log("B/S/O counts:", bso_text, verbose=True)
        self.canvas.create_text(diamond_center_x, diamond_center_y+40,
                                text=bso_text, font=self.font_status, fill=self.accent)

        # Batter / Pitcher info
        batter_text = "Batter: -"
        pitcher_text = "Pitcher: -"
        if self.live_feed:
            currentPlay = self.live_feed.get("liveData", {}).get("plays", {}).get("currentPlay", {})
            matchup = currentPlay.get("matchup", {})
            batter = matchup.get("batter", {}).get("fullName")
            pitcher = matchup.get("pitcher", {}).get("fullName")
            if batter:
                batter_text = f"Batter: {batter}"
            if pitcher:
                pitcher_text = f"Pitcher: {pitcher}"
            self.log("Matchup data:", batter_text, pitcher_text, verbose=True)

        self.canvas.create_text(self.width//2, diamond_center_y+70,
                                text=batter_text, font=self.font_small, fill=self.fg)
        self.canvas.create_text(self.width//2, diamond_center_y+90,
                                text=pitcher_text, font=self.font_small, fill=self.fg)

        self.render(full=False)

    def update_loop(self):
        if self.next_update_in <= 0 and not self.running_fetch:
            t = threading.Thread(target=self.fetch_and_schedule, daemon=True)
            t.start()
        if self.next_update_in > 0:
            self.next_update_in -= 1
        self.render(full=False)
        self.root.after(1000, self.update_loop)

    def fetch_and_schedule(self):
        self.running_fetch = True
        try:
            games = fetch_schedule(self.team_id)
            self.games = games
            now = datetime.datetime.now(datetime.timezone.utc)
            live, last, nxt = None, None, None
            for g in games:
                gd = g["gameDate_dt"]
                state = g["status"]["detailedState"]
                if state in ("In Progress", "Final", "Game Over") and gd <= now:
                    last = g
                if gd >= now and not nxt:
                    nxt = g
                if state == "In Progress":
                    live = g
            self.last_game = last
            self.next_game = nxt
            self.live_feed = fetch_live_feed(live["gamePk"]) if live else None
            if live:
                self.poll_interval = POLLING["live"]
                self.log(f"Game in progress, next poll in {self.poll_interval}s")
            elif last:
                self.poll_interval = POLLING["none"]
                self.log("No live game, showing last result")
            elif nxt:
                self.last_game = nxt
                self.poll_interval = POLLING["scheduled"]
                self.log(f"No live game, next scheduled game at {nxt['gameDate']}")
            else:
                self.poll_interval = POLLING["none"]
                self.log("No games found")
            self.next_update_in = self.poll_interval
            self.render(full=True)
        finally:
            self.running_fetch = False

# -------------------------
# Entry
# -------------------------
def main():
    root = tk.Tk()
    root.title("MLB Canvas Scoreboard")
    app = ScoreboardApp(root, CONFIG)
    root.mainloop()

if __name__ == "__main__":
    main()
