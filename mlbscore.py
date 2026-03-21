#!/usr/bin/env python3
"""
mlbscore.py — MLB Canvas Scoreboard
Features:
- Thread safety: ThreadPoolExecutor + _STATE_LOCK protecting shared state.
- Singleton requests.Session for connection pooling across all fetches.
- Precise API field filtering on both schedule and live feed endpoints.
- Smart polling capped at 86400s/day; per-gamePk recording header tracking.
- Canvas <Configure> binding: layout recalculates on window resize.
- Two-row team-gradient status bar: LIVE pill, inning, weather, next poll,
  last pitch speed/type, smoothed win-probability bar, batter AVG/OBP, pitch count.
- Scanline texture background for retro scoreboard feel.
- Clock displayed top-right; large live score in top-right corner when live.
- Followed team row highlighted with accent border in scoreboard grid.
- Active inning column: brighter header highlight.
- W/L indicator shown in icon cell when game is final.
- Score flash: run total pulses gold when a run scores.
- Base path dashed lines connecting all four bases on the diamond.
- Runner last name (up to 3 chars) shown inside occupied base squares.
- BSO redesigned: diamond shapes for balls, squares for strikes, red squares for outs.
- No-game screen shows recent results strip (last 5 W/L with score and opponent).
- Home plate pentagon drawn on diamond; clean-inning green glow ring.
- Player name truncation; "Starting soon / Delayed" for negative countdowns.
- Fetch error amber indicator; window title reflects followed team.
- Sacramento Athletics (ID 133) replaces Oakland Athletics.
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
import os # Added os import for record_live_feed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor # NEW: For cleaner thread management

# Lock protecting all shared mutable state written by the background thread
_STATE_LOCK = threading.Lock()

def _ts():
    """Return current HH:MM:SS timestamp string."""
    return datetime.datetime.now().strftime("%H:%M:%S")

def _log(level, cat, *args):
    """Module-level structured log line."""
    msg = " ".join(str(a) for a in args)
    print(f"{_ts()} [{level.upper()}/{cat.upper()}] {msg}")

# -------------------------
# Defaults
# -------------------------
DEFAULT_CONFIG = {
    "team_id": 117,
    "teams": {},
    "team_colors": {},
    "polling_intervals": {"live": 30, "pre_game": 60, "scheduled": 300, "none": 3600},
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
parser = argparse.ArgumentParser(description="MLB Canvas Scoreboard")
parser.add_argument("--config", default="config.json", help="Path to config.json")
parser.add_argument("--team", help="Team name (overrides config team_id if found)")
parser.add_argument("--debug", action="store_true", help="Enable debug logging (overrides config)")
parser.add_argument("--record", nargs="?", const="record_log.json",
                    help="Record game data when events change (default: record_log.json)")
parser.add_argument("--record-full", nargs="?", const="record_full_log.json",
                    help="Record every polling snapshot for analysis (default: record_full_log.json)")
args = parser.parse_args()
RECORD_PATH = args.record
RECORD_FULL_PATH = args.record_full

_last_record_state = None
_header_written_for_pk = None  # Fix #5: track per-game rather than a single boolean
_last_record_time = None

# -------------------------
# Config loader
# -------------------------
def load_config(path):
    cfg = deepcopy(DEFAULT_CONFIG)
    p = pathlib.Path(path)
    if not p.exists():
        _log("INFO", "CONFIG", f"config {path} not found; using defaults")
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
        _log("ERROR", "CONFIG", "Failed to load config:", e)
        return cfg

CONFIG = load_config(args.config)
# Allow CLI --debug to override config.json; otherwise use config value
CONFIG["debug"] = args.debug or CONFIG.get("debug", False)

TEAM_ID = CONFIG.get("team_id")
POLLING = CONFIG.get("polling_intervals", {"live": 30, "pre_game": 60, "scheduled": 300, "none": 3600})
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

# Reusable session singleton — avoids recreating adapters/pools on every fetch
_SESSION = make_session()

def parse_iso_to_local(dtstr):
    if not dtstr:
        return None
    try:
        # Using fromisoformat handles 'Z' implicitly with +00:00 replacement logic.
        dt = datetime.datetime.fromisoformat(dtstr.replace("Z", "+00:00"))
        return dt.astimezone()
    except Exception:
        return None

def fetch_schedule(team_id=None, lookahead=LOOKAHEAD_DAYS):
    if team_id is None:
        team_id = TEAM_ID
    today = datetime.date.today()
    start = today - datetime.timedelta(days=1)
    end = today + datetime.timedelta(days=lookahead)
    url = "https://statsapi.mlb.com/api/v1/schedule"
    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d"),
        # Only hydrate 'team' — linescore is fetched in full via the live feed endpoint
        # and is redundant here. Dropping it meaningfully reduces schedule response size.
        "hydrate": "team",
        # Restrict to exactly the fields the app reads from each game object:
        #   gamePk, gameDate, status.detailedState, teams.{away,home}.team.name
        "fields": (
            "dates,date,games,gamePk,gameDate,"
            "status,detailedState,"
            "teams,away,home,team,name,"
            "teams,away,home,isWinner,"
            "teams,away,home,score"
        ),
    }
    try:
        r = _SESSION.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        if DEBUG:
            _log("DEBUG", "POLL", f"fetch_schedule error: {e}")
        return []
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            gd = parse_iso_to_local(g.get("gameDate"))
            if gd:
                g["gameDate_dt"] = gd
            games.append(g)
    return sorted(games, key=lambda g: g.get("gameDate_dt") or datetime.datetime.max)

# Precise field filter for the live feed — only the paths the app actually reads.
# Structured as a comma-separated list matching the MLB Stats API 'fields' param format.
_LIVE_FEED_FIELDS = (
    # gameData branch
    "gameData,"
    "gameData,status,detailedState,"
    "gameData,teams,home,name,"
    "gameData,teams,away,name,"
    "gameData,venue,name,"
    "gameData,weather,condition,"
    "gameData,weather,temp,"
    "gameData,gameInfo,gameDurationMinutes,"
    # metaData for timestamp-based skip-render
    "metaData,timeStamp,"
    # liveData.linescore branch
    "liveData,"
    "liveData,linescore,"
    "liveData,linescore,currentInning,"
    "liveData,linescore,inningHalf,"
    "liveData,linescore,inningState,"
    "liveData,linescore,outs,"
    "liveData,linescore,innings,"
    "liveData,linescore,innings,away,runs,"
    "liveData,linescore,innings,home,runs,"
    "liveData,linescore,teams,"
    "liveData,linescore,teams,away,runs,"
    "liveData,linescore,teams,away,hits,"
    "liveData,linescore,teams,away,errors,"
    "liveData,linescore,teams,home,runs,"
    "liveData,linescore,teams,home,hits,"
    "liveData,linescore,teams,home,errors,"
    "liveData,linescore,offense,"
    "liveData,linescore,offense,first,"
    "liveData,linescore,offense,second,"
    "liveData,linescore,offense,third,"
    "liveData,linescore,offense,first,team,name,"
    "liveData,linescore,offense,second,team,name,"
    "liveData,linescore,offense,third,team,name,"
    "liveData,linescore,offense,first,lastName,"
    "liveData,linescore,offense,second,lastName,"
    "liveData,linescore,offense,third,lastName,"
    # liveData.plays.currentPlay branch
    "liveData,plays,currentPlay,"
    "liveData,plays,currentPlay,count,balls,"
    "liveData,plays,currentPlay,count,strikes,"
    "liveData,plays,currentPlay,count,pitches,"
    "liveData,plays,currentPlay,result,description,"
    "liveData,plays,currentPlay,matchup,batter,fullName,"
    "liveData,plays,currentPlay,matchup,pitcher,fullName,"
    "liveData,plays,currentPlay,runners,"
    "liveData,plays,currentPlay,runners,movement,start,"
    "liveData,plays,currentPlay,runners,movement,end,"
    "liveData,plays,currentPlay,runners,team,name,"
    # Last pitch speed + type
    "liveData,plays,currentPlay,playEvents,"
    "liveData,plays,currentPlay,playEvents,pitchData,startSpeed,"
    "liveData,plays,currentPlay,playEvents,details,type,description,"
    "liveData,plays,currentPlay,playEvents,isPitch,"
    "liveData,plays,currentPlay,playEvents,pitchNumber,"
    # Win probability
    "gameData,teams,home,record,wins,"
    "gameData,teams,home,record,losses,"
    "gameData,teams,away,record,wins,"
    "gameData,teams,away,record,losses,"
    # Boxscore for batter season AVG/OBP and pitcher pitch count
    "liveData,boxscore,"
    "liveData,boxscore,teams,"
    "liveData,boxscore,teams,home,players,"
    "liveData,boxscore,teams,away,players,"
    "liveData,boxscore,teams,home,players,seasonStats,batting,avg,"
    "liveData,boxscore,teams,home,players,seasonStats,batting,obp,"
    "liveData,boxscore,teams,home,players,seasonStats,batting,homeRuns,"
    "liveData,boxscore,teams,home,players,seasonStats,batting,rbi,"
    "liveData,boxscore,teams,away,players,seasonStats,batting,avg,"
    "liveData,boxscore,teams,away,players,seasonStats,batting,obp,"
    "liveData,boxscore,teams,away,players,seasonStats,batting,homeRuns,"
    "liveData,boxscore,teams,away,players,seasonStats,batting,rbi,"
    "liveData,boxscore,teams,home,players,seasonStats,pitching,wins,"
    "liveData,boxscore,teams,home,players,seasonStats,pitching,era,"
    "liveData,boxscore,teams,away,players,seasonStats,pitching,wins,"
    "liveData,boxscore,teams,away,players,seasonStats,pitching,era,"
    "liveData,boxscore,teams,home,players,stats,pitching,pitchesThrown,"
    "liveData,boxscore,teams,away,players,stats,pitching,pitchesThrown,"
    "liveData,boxscore,teams,home,players,person,fullName,"
    "liveData,boxscore,teams,away,players,person,fullName,"
    "liveData,boxscore,teams,home,players,person,firstName,"
    "liveData,boxscore,teams,away,players,person,firstName,"
    "liveData,boxscore,teams,home,players,position,abbreviation,"
    "liveData,boxscore,teams,away,players,position,abbreviation,"
    "liveData,boxscore,teams,home,players,battingOrder,"
    "liveData,boxscore,teams,away,players,battingOrder"
)

def fetch_live_feed(gamePk):
    if not gamePk:
        return None
    url = f"https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"
    params = {"fields": _LIVE_FEED_FIELDS}
    try:
        r = _SESSION.get(url, params=params, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if DEBUG:
            _log("DEBUG", "FEED", f"fetch_live_feed error: {e}")
        return None

# --- CRITICAL FIX: Combined and cleaned up record_live_feed ---
def record_live_feed(feed, game_info=None, full=False):
    """Hybrid event-based recording system with auto file naming and delta timing."""
    global _last_record_state, _header_written_for_pk, _last_record_time

    # Fix #4: only run if the matching record path is actually configured
    base_path = RECORD_FULL_PATH if full else RECORD_PATH
    if not base_path or not feed:
        return

    # Use 'games' subdirectory for full/event recording
    os.makedirs("games", exist_ok=True)

    # Extract teams for file naming
    game_data = feed.get("gameData", {})
    home = game_data.get("teams", {}).get("home", {}).get("name", "Home")
    away = game_data.get("teams", {}).get("away", {}).get("name", "Away")
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    safe_home = home.replace(" ", "_").replace("/", "-")
    safe_away = away.replace(" ", "_").replace("/", "-")
    filename = f"games/{date_str}-{safe_away}-{safe_home}.log" # Ensure safe names in filename

    try:
        # Extract key fields
        linescore = feed.get("liveData", {}).get("linescore", {})
        current_play = feed.get("liveData", {}).get("plays", {}).get("currentPlay", {})
        matchup = current_play.get("matchup", {})

        now = datetime.datetime.now()
        delta_t = (now - _last_record_time).total_seconds() if _last_record_time else 0
        _last_record_time = now

        entry = {
            "timestamp": now.isoformat(),
            "delta_t": delta_t,
            "gamePk": game_info.get("gamePk") if game_info else None,
            "state": game_info.get("status", {}).get("detailedState") if game_info else None,
            "inning": linescore.get("currentInning"),
            "halfInning": linescore.get("inningState"),
            "outs": linescore.get("outs"),
            "balls": current_play.get("count", {}).get("balls"),
            "strikes": current_play.get("count", {}).get("strikes"),
            "bases": {
                "first": bool(linescore.get("offense", {}).get("first")),
                "second": bool(linescore.get("offense", {}).get("second")),
                "third": bool(linescore.get("offense", {}).get("third")),
            },
            "batter": matchup.get("batter", {}).get("fullName"),
            "pitcher": matchup.get("pitcher", {}).get("fullName"),
        }

        current_pk = entry["gamePk"]
        # Fix #5: reset header flag when a new game (different gamePk) is detected
        if not _header_written_for_pk or _header_written_for_pk != current_pk or not os.path.exists(filename):
            meta = {
                "meta": True,
                "timestamp": entry["timestamp"],
                "gamePk": entry["gamePk"],
                "home": home,
                "away": away,
                "venue": game_data.get("venue", {}).get("name"),
                "description": "MLB Scoreboard recording session",
                "mode": "full" if full else "event",
            }
            with open(filename, "a", encoding="utf-8") as f:
                f.write(json.dumps(meta) + "\n")
            _header_written_for_pk = current_pk
            if DEBUG:
                _log("DEBUG", "RECORD", f"Wrote header to {filename}")

        # Skip redundant state unless full mode
        if not full and entry == _last_record_state:
            return

        with open(filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        _last_record_state = entry

        if DEBUG:
            _log("DEBUG", "RECORD", f"Recorded {'FULL' if full else 'EVENT'} snapshot to {filename}")

    except Exception as e:
        _log("ERROR", "RECORD", f"Failed to record feed: {e}")
        
    # Redundant second recording block removed for cleanup.

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
    # Added error handling for bad hex format
    if len(hex_color) != 6:
        return (0, 0, 0)
    try:
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)

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
        # STATUS_BAR_H values are set by _compute_layout() — initialise with defaults
        self.STATUS_BAR_H_LIVE = 112
        self.STATUS_BAR_H      = 56

        self.canvas = tk.Canvas(root, width=self.width, height=self.height,
                                bg=self.bg, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # Fix #10: recalculate layout dimensions when window is resized
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.root.bind("<F11>", self._toggle_fullscreen)
        self.root.bind("<Escape>", lambda e: self._set_fullscreen(False))
        self.root.bind("<F5>", self._manual_refresh)

        # Fix #11: track fetch errors to surface them in the UI
        self._fetch_error = False
        self._fetch_error_msg = ""
        # Poll status indicator: "error" | "unchanged" | "updated" | "pending"
        self._poll_status = "pending"
        self._poll_status_set_at = time.time()  # when the current status was last written

        # fonts
        self.font_title = tkfont.Font(family=self.font_family, size=18, weight="bold")
        self.font_header = tkfont.Font(family=self.font_family, size=11, weight="bold")
        self.font_team = tkfont.Font(family=self.font_family, size=13, weight="bold")
        self.font_small = tkfont.Font(family=self.font_family, size=10)
        self.font_status = tkfont.Font(family=self.font_family, size=12, weight="bold")
        self.font_sb_label = tkfont.Font(family=self.font_family, size=8)
        self.font_sb_value = tkfont.Font(family=self.font_family, size=11, weight="bold")
        self.font_marquee = tkfont.Font(family=self.font_family, size=10)

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

        # base path pulse animations: list of active pulse dicts
        # each: {segments: [(p1,p2),...], seg_idx: int, t: float, color: str, after_id: int|None}
        self._path_pulses = []

        # base state
        self.bases = {
            "1B": {"occupied": False, "team": None, "anim": None},
            "2B": {"occupied": False, "team": None, "anim": None},
            "3B": {"occupied": False, "team": None, "anim": None},
        }
        self.empty_base_fill = "#d0d0d0"

        # runner animation state
        # rkey -> {"cid": tk_id, "base": "1B", "color": "#HEX"}
        self.runners = {} 
        # "1B" -> rkey
        self.runners_by_base = {} 
        self._next_runner_key = 1

        self.current_batter = "Batter: -"
        self.current_pitcher = "Pitcher: -"

        # Status bar state
        self.sb_last_pitch_speed = None   # float mph
        self.sb_last_pitch_type = None    # str e.g. "4-Seam Fastball"
        self.sb_away_record = None    # str e.g. "15-12 (.556)"
        self.sb_home_record = None    # str e.g. "13-14 (.481)"
        self.sb_last_play = None      # str — last play result description
        self.sb_last_play_set_at = 0.0  # timestamp when sb_last_play was last updated
        self.sb_event_msg = None      # str — transient event override (challenge, delay, etc.)
        self.sb_event_msg_set_at = 0.0  # timestamp when event_msg was set
        self.sb_batter_avg = None         # str e.g. ".285"
        self.sb_batter_obp = None         # str e.g. ".350"
        self.sb_pitch_count = None        # int total pitches thrown by current pitcher
        self.sb_weather = None            # str e.g. "72°F Sunny"

        # Marquee scroller state
        self._marquee_text = ""          # full pre-built scroll string
        self._marquee_x = 0             # current left-edge x position
        self._marquee_text_w = 0        # measured pixel width of the full string
        self._marquee_after_id = None   # handle for scheduled tick
        self._marquee_canvas_id = None  # canvas item id for the scrolling text
        self._marquee_is_live = False   # tracks live/not-live for string rebuild
        self._marquee_active = False    # True only while live

        # Roster for marquee: list of dicts per team [{name, pos, is_batter, is_pitcher}, ...]
        self.marquee_followed_roster = []
        self.marquee_opponent_roster = []
        # Season stats for the followed team — used in not-live marquee
        # Each entry: {str: display_string, full_name: str, order: int, is_pitcher: bool}
        self.followed_season_stats = []

        # Runner names per base (last name for display in runner dots)
        self.runner_names = {"1B": None, "2B": None, "3B": None}

        # Score flash: track previous run totals to detect scoring plays
        self._prev_runs = {"away": 0, "home": 0}
        self._score_flash = {}   # "away"|"home" -> remaining flash frames

        # Recent results (last 5 completed games, oldest first)
        self.recent_results = []   # list of {"wl": "W"|"L", "score": "4-2", "opp": "LAD"}

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

        # layout caches — all derived by _compute_layout() from canvas size
        self.left_margin   = 0
        self.top_margin    = 0
        self.score_start_x = 0
        self.col_width     = 0
        self.row_height    = 0
        self.header_half   = 0   # half-height of header/row cells
        self.diamond_cx    = None
        self.diamond_cy    = None
        self.diamond_ds    = None
        self.bso_zone_left  = 0
        self.bso_zone_right = 0
        self.bso_spacing    = 0
        self.bso_sz         = 0
        self.base_half     = 0
        self.base_positions = {}

        # fullscreen state
        self._is_fullscreen = False

        self._compute_layout()

        # initial loop
        self.root.after(100, self.update_loop)

        # limited debug trackers
        self._last_poll_time = 0
        self._last_runner_state = {}
        # Timestamp-based skip-render: store last seen metaData.timeStamp from live feed
        self._last_feed_timestamp = None
        # Track whether the last poll returned unchanged data — suppresses diamond/BSO redraw
        self._feed_unchanged = False
        # Track last known game status for change detection
        self._last_game_status = ""

    def _compute_layout(self):
        """Derive every layout constant proportionally from current canvas size.
        Call at startup and on every resize before rendering."""
        w, h = self.width, self.height
        ff   = self.font_family

        # ── Margins ────────────────────────────────────────────────────────────
        self.left_margin  = max(16, int(w * 0.042))
        self.top_margin   = max(36, int(h * 0.082))
        self.header_half  = max(14, int(h * 0.028))   # ½ height of header/row cells

        # ── Scoreboard grid ─────────────────────────────────────────────────────
        # team-name column ends at ~26% of width
        self.score_start_x = max(160, int(w * 0.26))
        max_cols = UI_CFG.get("max_innings", 9) + 4   # innings + R H E icon
        avail    = w - self.score_start_x - 20
        self.col_width  = max(24, avail // max_cols)
        self.row_height = max(24, int(h * 0.063))

        # ── Font sizes (scale with canvas height) ───────────────────────────────
        def _fs(ratio, lo):
            return max(lo, int(h * ratio))

        sizes = {
            "font_title":   (_fs(0.028, 10), "bold"),
            "font_header":  (_fs(0.018, 8),  "bold"),
            "font_team":    (_fs(0.020, 8),  "bold"),
            "font_small":   (_fs(0.016, 7),  "normal"),
            "font_status":  (_fs(0.020, 8),  "bold"),
            "font_sb_label":(_fs(0.013, 6),  "normal"),
            "font_sb_value":(_fs(0.017, 7),  "bold"),
            "font_marquee": (_fs(0.016, 7),  "normal"),
        }
        for attr, (sz, wt) in sizes.items():
            f = getattr(self, attr, None)
            if f:
                f.configure(family=ff, size=sz, weight=wt)

        # ── Status bar heights ──────────────────────────────────────────────────
        self.STATUS_BAR_H_LIVE = max(72, int(h * 0.17))
        self.STATUS_BAR_H      = max(36, int(h * 0.085))

        # ── Diamond ─────────────────────────────────────────────────────────────
        # Grid bottom — must match render()'s grid_bottom = top_margin - hh + row_height*3
        grid_bot = self.top_margin - self.header_half + self.row_height * 3

        # Space between grid bottom and status bar top
        body_h   = h - grid_bot - self.STATUS_BAR_H_LIVE
        body_w   = int(w * 0.52)   # diamond lives in left ~52% of canvas

        # diamond_ds = half-diagonal; fit tightly inside available area
        ds_from_h = int(body_h * 0.38)
        ds_from_w = int(body_w * 0.23)
        self.diamond_ds = max(30, min(ds_from_h, ds_from_w))
        ds = self.diamond_ds

        self.diamond_cx = self.left_margin + int(body_w * 0.38)
        self.diamond_cy = grid_bot + max(ds + 10, body_h // 2)

        # ── Base squares ───────────────────────────────────────────────────────
        self.base_half = max(8, int(ds * 0.155))

        # base_positions derived purely from geometry — kept current even when
        # _skip_dynamic suppresses the diamond redraw, so pulse animations work
        inset = ds * 0.6
        self.base_positions = {
            "2B":   (self.diamond_cx,         self.diamond_cy - inset),
            "1B":   (self.diamond_cx + inset, self.diamond_cy),
            "3B":   (self.diamond_cx - inset, self.diamond_cy),
            "Home": (self.diamond_cx,         self.diamond_cy + inset),
        }

        # ── BSO panel — left-anchored from right edge of diamond ──────────────
        diamond_right       = self.diamond_cx + ds
        self.bso_zone_left  = diamond_right
        self.bso_zone_right = w
        self.bso_spacing    = max(14, int(ds * 0.25))
        self.bso_sz         = max(5,  int(ds * 0.085))

    def _toggle_fullscreen(self, event=None):
        self._set_fullscreen(not self._is_fullscreen)

    def _set_fullscreen(self, state):
        self._is_fullscreen = bool(state)
        self.root.attributes("-fullscreen", self._is_fullscreen)

    def _manual_refresh(self, event=None):
        """F5 — reset poll timer to zero so update_loop triggers an immediate fetch."""
        if not self.running_fetch:
            with _STATE_LOCK:
                self.next_update_in = 0
            self.log("Manual refresh triggered (F5)", level="info", cat="POLL")

    def _on_canvas_resize(self, event):
        """Update size, recompute all layout, trigger full redraw."""
        new_w, new_h = event.width, event.height
        if new_w != self.width or new_h != self.height:
            self.width  = new_w
            self.height = new_h
            self._compute_layout()
            self.render_full_gui()

    def log(self, *args, verbose=False, level="info", cat="APP"):
        """
        Centralised logging with HH:MM:SS timestamp and category prefix.

        Categories: POLL, FEED, GAME, UI, RECORD, CONFIG, APP
        verbose=True lines only print in --debug mode.
        level="error" always prints regardless of debug flag.
        """
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        cat = cat.upper()

        if verbose:
            if not self.debug:
                return
            msg = " ".join(str(a) for a in args)
            print(f"{ts} [DEBUG/{cat}] {msg}")
        else:
            lvl = str(level).lower()
            if lvl == "error":
                msg = " ".join(str(a) for a in args)
                print(f"{ts} [ERROR/{cat}] {msg}")
            elif self.debug or lvl == "info":
                msg = " ".join(str(a) for a in args)
                print(f"{ts} [{lvl.upper()}/{cat}] {msg}")

    def _set_poll_status(self, status):
        """Set poll status and record the time it was set. Always call under _STATE_LOCK."""
        self._poll_status = status
        self._poll_status_set_at = time.time()

    # ── Marquee scroller ──────────────────────────────────────────────────────

    def _build_marquee_string(self):
        """Build the full marquee scroll string."""
        sep = "   •   "
        followed_name = self.followed_team_name or "TEAM"

        is_live = False
        if self.live_feed:
            st = (self.live_feed.get("gameData", {})
                  .get("status", {}).get("detailedState", "")) or ""
            is_live = "In Progress" in st or "Live" in st

        if is_live:
            opp_name = "OPP"
            if self.live_feed:
                gd = self.live_feed.get("gameData", {}).get("teams", {}) or {}
                home_n = gd.get("home", {}).get("name", "")
                away_n = gd.get("away", {}).get("name", "")
                opp_name = away_n if home_n == followed_name else home_n
            followed_body = sep.join(p["str"] for p in self.marquee_followed_roster)
            opponent_body = sep.join(p["str"] for p in self.marquee_opponent_roster)
            return (f"{followed_name.upper()} ► {followed_body}"
                    f"          ---          "
                    f"{opp_name.upper()} ► {opponent_body}")
        else:
            if not self.followed_season_stats:
                return f"{followed_name.upper()} ► No stats available"
            body = sep.join(p["str"] for p in self.followed_season_stats)
            return f"{followed_name.upper()} ►  {body}"

    def _marquee_start(self):
        """Build the marquee string and start the scroll tick if not already running."""
        text = self._build_marquee_string()
        self._marquee_text   = text
        self._marquee_text_w = self.font_marquee.measure(text)

        if not self._marquee_active:
            self._marquee_x      = self.width
            self._marquee_active = True

        if self._marquee_canvas_id:
            try:
                self.canvas.delete(self._marquee_canvas_id)
            except Exception:
                pass
            self._marquee_canvas_id = None

        self._marquee_tick()

    def _marquee_stop(self):
        """Stop the scroller and remove its canvas item."""
        self._marquee_active = False
        if self._marquee_after_id:
            self.root.after_cancel(self._marquee_after_id)
            self._marquee_after_id = None
        if self._marquee_canvas_id:
            try:
                self.canvas.delete(self._marquee_canvas_id)
            except Exception:
                pass
            self._marquee_canvas_id = None

    def _marquee_tick(self):
        """Advance the marquee by ~1.67px (50px/s at 30fps) and reschedule."""
        if not self._marquee_active:
            return

        sbh     = self.STATUS_BAR_H_LIVE
        row_h   = sbh // 4
        bar_top = self.height - sbh
        row_cy  = bar_top + row_h // 2

        # 50 px/s ÷ 30 fps ≈ 1.67 px per tick
        self._marquee_x -= 1.67

        if self._marquee_x < -self._marquee_text_w:
            self._marquee_x = self.width

        # Delete all marquee items from the previous tick
        self.canvas.delete("marquee")
        self._marquee_canvas_id = None

        self._marquee_canvas_id = self.canvas.create_text(
            int(self._marquee_x), row_cy,
            text=self._marquee_text,
            font=self.font_marquee,
            fill="#ffffff",
            anchor="nw",
            tags="marquee"
        )

        self._marquee_after_id = self.root.after(33, self._marquee_tick)

    # ─────────────────────────────────────────────────────────────────────────
    def compute_base_positions(self):
        """Calculates base coordinates relative to the diamond center."""
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
        """Spawns a runner icon at a base."""
        if threading.current_thread() != threading.main_thread():
            self.log(f"Spawn requested for {base_key} from non-main thread. Scheduling...", verbose=True, cat="UI")
            self.root.after(0, lambda: self.spawn_runner_at_base(base_key, color))
            return

        if base_key == "Home" or base_key in self.runners_by_base:
            return None
        self.compute_base_positions()
        pos = self.base_positions.get(base_key)
        if pos is None:
            return None
        bx, by = pos
        color = color or self.accent
        r = max(10, int(self.diamond_ds * 0.13))  # radius scales with diamond
        rkey = f"r{self._next_runner_key}"
        self._next_runner_key += 1
        cid = self.canvas.create_oval(bx - r, by - r, bx + r, by + r,
                                      fill=color, outline="white", width=3,
                                      tags="runner_dot")
        self.runners[rkey] = {"cid": cid, "base": base_key, "color": color, "r": r}
        self.runners_by_base[base_key] = rkey
        self.log(f"Runner spawned: {rkey} at {base_key}", verbose=True, cat="UI")
        # Pulse the base glow on arrival
        self.root.after(0, lambda: self._pulse_base_glow(base_key, color))
        return rkey

    def move_runner_base(self, from_base, to_base, color=None, steps=30):
        """Handles runner movement with animation and base state updates."""
        if threading.current_thread() != threading.main_thread():
            self.log(f"Move requested for {from_base} to {to_base} from non-main thread. Scheduling...", verbose=True, cat="UI")
            self.root.after(0, lambda: self.move_runner_base(from_base, to_base, color, steps))
            return

        rkey = self.runners_by_base.pop(from_base, None)
        runner = self.runners.get(rkey)

        if not rkey or not runner:
            self.log(f"Move requested from {from_base} but no runner found/present.", verbose=True, cat="UI")
            if to_base != "Home":
                return self.spawn_runner_at_base(to_base, color=color or self.accent)
            return None

        self.compute_base_positions()
        start = self.base_positions.get(from_base)
        end   = self.base_positions.get(to_base)
        color = runner.get("color", self.accent)
        r     = runner.get("r", max(10, int(self.diamond_ds * 0.13)))

        try:
            self.canvas.delete(runner["cid"])
        except Exception:
            pass
        self.runners.pop(rkey, None)

        if not start or not end:
            self.log(f"Error: Base positions unknown for {from_base} or {to_base}. Spawning at destination.", level="error", cat="UI")
            if to_base != "Home":
                return self.spawn_runner_at_base(to_base, color=color)
            return None

        sx, sy = start
        tx, ty = end
        dx = (tx - sx) / float(steps)
        dy = (ty - sy) / float(steps)

        # Larger, brighter moving dot with a white trailing glow outline
        temp_cid = self.canvas.create_oval(sx - r, sy - r, sx + r, sy + r,
                                           fill=color, outline="#ffffff", width=3,
                                           tags="runner_dot")
        # Outer glow ring
        glow_cid = self.canvas.create_oval(sx - r - 4, sy - r - 4,
                                            sx + r + 4, sy + r + 4,
                                            fill="", outline=color, width=2,
                                            tags="runner_dot")

        def _step(i=0):
            if i >= steps:
                try:
                    self.canvas.delete(temp_cid)
                    self.canvas.delete(glow_cid)
                except Exception:
                    pass
                if to_base != "Home":
                    new_key = self.spawn_runner_at_base(to_base, color=color)
                    self.log(f"Runner moved: {rkey} {from_base} -> {to_base} as {new_key}", verbose=True, cat="UI")
                else:
                    # Scored — gold flash at home plate fading over 2s
                    self._flash_score_at_home(tx, ty, color)
                    self.log(f"Runner {rkey} scored at Home", verbose=True, cat="UI")
                self.render_full_gui()
                return

            try:
                self.canvas.move(temp_cid, dx, dy)
                self.canvas.move(glow_cid, dx, dy)
            except Exception:
                pass
            self.root.after(30, lambda: _step(i + 1))

        _step()
        return rkey

    # Base path: diamond order used to build movement segments
    _BASE_ORDER = ["Home", "1B", "2B", "3B", "Home"]

    def _pulse_base_glow(self, base_key, color, pulses=10, period_ms=500):
        """Draw a repeating glow ring around a base on runner arrival (10 pulses × 500ms = 5s)."""
        pos = self.base_positions.get(base_key)
        if not pos:
            return
        bx, by = pos
        r_base = max(10, int(self.diamond_ds * 0.13))
        glow_id = self.canvas.create_oval(
            bx - r_base - 6, by - r_base - 6,
            bx + r_base + 6, by + r_base + 6,
            fill="", outline=color, width=3, tags="runner_dot"
        )

        def _fade(step=0, max_steps=10):
            if step >= max_steps:
                try:
                    self.canvas.delete(glow_id)
                except Exception:
                    pass
                return
            alpha = 1.0 - (step / float(max_steps))
            faded = blend_colors(color, self.bg, 1.0 - alpha)
            try:
                self.canvas.itemconfig(glow_id, outline=faded)
            except Exception:
                pass
            self.root.after(period_ms // max_steps, lambda: _fade(step + 1, max_steps))

        def _pulse(n=0):
            if n >= pulses:
                try:
                    self.canvas.delete(glow_id)
                except Exception:
                    pass
                return
            try:
                self.canvas.itemconfig(glow_id, outline=color)
            except Exception:
                return
            # Fade out over one period, then repeat
            steps = 10
            ms_per = period_ms // steps

            def _step_fade(s=0):
                if s >= steps:
                    self.root.after(50, lambda: _pulse(n + 1))
                    return
                alpha = 1.0 - (s / float(steps))
                faded = blend_colors(color, self.bg, 1.0 - alpha)
                try:
                    self.canvas.itemconfig(glow_id, outline=faded, width=max(1, int(3 * alpha)))
                except Exception:
                    return
                self.root.after(ms_per, lambda: _step_fade(s + 1))

            _step_fade()

        _pulse()

    def _flash_score_at_home(self, hx, hy, color, duration_ms=2000):
        """Gold expanding ring + shrinking dot at home plate when a runner scores."""
        r = max(10, int(self.diamond_ds * 0.13))
        # Expanding gold ring
        ring_id = self.canvas.create_oval(
            hx - r, hy - r, hx + r, hy + r,
            fill="", outline="#FFD700", width=4, tags="runner_dot"
        )
        # Filled dot in team colour
        dot_id = self.canvas.create_oval(
            hx - r, hy - r, hx + r, hy + r,
            fill=color, outline="white", width=3, tags="runner_dot"
        )

        steps = 20
        ms_per = duration_ms // steps

        def _anim(s=0):
            if s >= steps:
                for cid in (ring_id, dot_id):
                    try:
                        self.canvas.delete(cid)
                    except Exception:
                        pass
                return
            t = s / float(steps)
            # Ring expands outward
            rr = r + int(r * 2.5 * t)
            ring_col = blend_colors("#FFD700", self.bg, t)
            try:
                self.canvas.coords(ring_id, hx - rr, hy - rr, hx + rr, hy + rr)
                self.canvas.itemconfig(ring_id, outline=ring_col, width=max(1, int(4 * (1 - t))))
            except Exception:
                pass
            # Dot shrinks
            rd = max(1, int(r * (1.0 - t)))
            dot_col = blend_colors(color, self.bg, t)
            try:
                self.canvas.coords(dot_id, hx - rd, hy - rd, hx + rd, hy + rd)
                self.canvas.itemconfig(dot_id, fill=dot_col)
            except Exception:
                pass
            self.root.after(ms_per, lambda: _anim(s + 1))

        _anim()

    def animate_base_path(self, from_base, to_base, color=None):
        """
        Animate a travelling pulse along the base path lines.
        Loops enough times to fill 5–15 seconds.
        """
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, lambda: self.animate_base_path(from_base, to_base, color))
            return

        self.log(f"animate_base_path: {from_base} → {to_base}", verbose=True, cat="UI")

        order = self._BASE_ORDER
        try:
            fi = order.index(from_base)
            ti = order.index(to_base, fi + 1)
        except ValueError:
            return

        segments = [(order[i], order[i + 1]) for i in range(fi, ti)]
        if not segments:
            return

        # Each segment takes ms_per_seg ms; clamp total to 5000–15000ms
        ms_per_seg  = 400                      # ms for one segment traversal
        total_segs  = len(segments)
        one_pass_ms = total_segs * ms_per_seg
        min_passes  = max(1, -(-5000 // one_pass_ms))    # ceil(5000 / one_pass_ms)
        max_passes  = max(min_passes, 15000 // one_pass_ms)
        passes      = min_passes   # always run at least enough for 5s; cap at 15s max
        passes      = min(passes, max_passes)

        pulse = {
            "segments":  segments,
            "seg_idx":   0,
            "t":         0.0,
            "passes_left": passes,
            "color":     color or self.accent,
            "cid":       None,
            "after_id":  None,
            "ms_per_seg": ms_per_seg,
        }
        self._path_pulses.append(pulse)
        self._pulse_tick(pulse)

    def _pulse_tick(self, pulse):
        """Advance one pulse frame. ~30fps, duration controlled by ms_per_seg."""
        if pulse not in self._path_pulses:
            return

        fps    = 30
        ms_per_seg = pulse.get("ms_per_seg", 400)
        dt     = 1.0 / (ms_per_seg / (1000.0 / fps))   # progress per tick

        pulse["t"] += dt
        if pulse["t"] >= 1.0:
            pulse["t"] = 0.0
            pulse["seg_idx"] += 1
            if pulse["seg_idx"] >= len(pulse["segments"]):
                # Completed one pass
                pulse["passes_left"] -= 1
                if pulse["passes_left"] <= 0:
                    if pulse["cid"]:
                        try:
                            self.canvas.delete(pulse["cid"])
                        except Exception:
                            pass
                    if pulse in self._path_pulses:
                        self._path_pulses.remove(pulse)
                    return
                pulse["seg_idx"] = 0   # loop back to start

        seg = pulse["segments"][pulse["seg_idx"]]
        p1  = self.base_positions.get(seg[0])
        p2  = self.base_positions.get(seg[1])

        if p1 and p2:
            t    = pulse["t"]
            half = 0.12   # longer pulse = more visible
            t0   = max(0.0, t - half)
            t1   = min(1.0, t + half)
            x0 = p1[0] + (p2[0] - p1[0]) * t0
            y0 = p1[1] + (p2[1] - p1[1]) * t0
            x1 = p1[0] + (p2[0] - p1[0]) * t1
            y1 = p1[1] + (p2[1] - p1[1]) * t1

            # Brightness fades toward end of each pass
            pass_progress = pulse["seg_idx"] / max(1, len(pulse["segments"]))
            alpha = 1.0 - pass_progress * 0.3   # slight dim toward end
            line_color = blend_colors(pulse["color"], "#ffffff", 0.4 * alpha)

            if pulse["cid"]:
                try:
                    self.canvas.delete(pulse["cid"])
                except Exception:
                    pass
            pulse["cid"] = self.canvas.create_line(
                x0, y0, x1, y1,
                fill=line_color, width=5,
                tags="path_pulse"
            )

        pulse["after_id"] = self.root.after(1000 // fps, lambda: self._pulse_tick(pulse))

    def clear_all_runners(self):
        """Clears all runner icons and base path pulses from the canvas."""
        if threading.current_thread() != threading.main_thread():
             self.root.after(0, self.clear_all_runners)
             return

        for rkey, info in list(self.runners.items()):
            try:
                self.canvas.delete(info.get("cid"))
            except Exception:
                pass
        self.runners.clear()
        self.runners_by_base.clear()

        # Cancel and remove all active path pulses
        for pulse in list(self._path_pulses):
            if pulse.get("after_id"):
                try:
                    self.root.after_cancel(pulse["after_id"])
                except Exception:
                    pass
            if pulse.get("cid"):
                try:
                    self.canvas.delete(pulse["cid"])
                except Exception:
                    pass
        self._path_pulses.clear()
        self.canvas.delete("path_pulse")
        self.log("All runners and path pulses cleared", verbose=True, cat="UI")

    def render_full_gui(self):
        """Wrapper to ensure full render is called on the main thread."""
        if threading.current_thread() != threading.main_thread():
             self.root.after(0, self.render_full_gui)
             return
        self.render(full=True)

    def format_seconds_to_dhms_string(self, seconds):
        """Formats an integer number of seconds into '$days, HH:MM:SS' string."""
        seconds = int(seconds)
        # Fix #15: negative values mean the scheduled start has passed without going live
        if seconds < 0:
            return "Starting soon / Delayed"
        if seconds == 0:
            return "00:00:00"

        td = datetime.timedelta(seconds=seconds)
        hours = td.seconds // 3600
        minutes = (td.seconds % 3600) // 60
        secs = td.seconds % 60
        time_part = f"{hours:02}:{minutes:02}:{secs:02}"
        return f"{td.days}d, {time_part}" if td.days > 0 else time_part

    # rendering
    def render(self, full=True):
        """Main rendering function (must be called on main thread)."""
        if threading.current_thread() != threading.main_thread():
            self.log("render() called from non-main thread!", level="error", cat="UI")
            return

        if full:
            self.canvas.delete("all")
            self._marquee_canvas_id = None
            self._feed_unchanged = False
        else:
            # Only the status bar needs updating every second (countdown timer).
            # Diamond/BSO are left untouched during a fetch — _skip_dynamic handles this below.
            self.canvas.delete("status_bar")

        # Skip dynamic (diamond + BSO) redraw during partial renders while fetching,
        # or when the last poll returned unchanged data (nothing to redraw).
        _skip_dynamic = not full and (self.running_fetch or self._feed_unchanged)

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
            mid_y = (self.height - self.STATUS_BAR_H) // 2
            msg = f"Waiting for game data — {self.followed_team_name}"
            self.canvas.create_text(self.width // 2, mid_y - 60,
                                    text=msg, font=self.font_title, fill=self.fg)

            # Recent results strip
            if self.recent_results:
                self.canvas.create_text(self.width // 2, mid_y - 20,
                                        text="RECENT RESULTS", font=self.font_header,
                                        fill=self.accent)
                strip_w  = max(70, int(self.width * 0.09))
                pill_hw  = max(20, strip_w // 2 - 8)   # half-width of pill
                pill_hh  = max(16, int(self.height * 0.038))  # half-height of pill
                total_w  = len(self.recent_results) * strip_w
                strip_x0 = self.width // 2 - total_w // 2
                for idx, res in enumerate(self.recent_results):
                    sx = strip_x0 + idx * strip_w + strip_w // 2
                    sy = mid_y + 20
                    wl    = res.get("wl", "?")
                    score = res.get("score", "-")
                    opp   = res.get("opp", "")[:10]
                    pill_col = "#00e676" if wl == "W" else "#e74c3c"
                    self.canvas.create_rectangle(sx - pill_hw, sy - pill_hh,
                                                 sx + pill_hw, sy + pill_hh,
                                                 fill=pill_col, outline="white", width=1)
                    self.canvas.create_text(sx, sy - pill_hh // 2, text=wl,
                                            font=self.font_status, fill="#ffffff")
                    self.canvas.create_text(sx, sy + 2, text=score,
                                            font=self.font_sb_label, fill="#ffffff")
                    self.canvas.create_text(sx, sy + pill_hh // 2 + 2, text=f"vs {opp}",
                                            font=self.font_sb_label, fill="#dddddd")

            self.canvas.delete("status_bar")
            self.render_status_bar("UNKNOWN", "UNKNOWN")
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

        left_margin   = self.left_margin
        top_margin    = self.top_margin
        team_x        = left_margin
        score_start_x = self.score_start_x
        col_width     = self.col_width
        row_height    = self.row_height
        hh            = self.header_half   # half-height of all cells

        y_away = top_margin + row_height
        y_home = y_away + row_height

        # Full render components
        if full:
            # Scanline texture: subtle horizontal lines every 4px for retro feel
            for sy in range(0, self.height, 4):
                self.canvas.create_line(0, sy, self.width, sy,
                                        fill="#000000", stipple="gray12",
                                        width=1, tags="scanline")

            title_text = f"{self.followed_team_name} — MLB Scoreboard"
            title_y = max(16, int(self.height * 0.032))
            self.canvas.create_text(self.width // 2, title_y, text=title_text, font=self.font_title, fill=self.accent)

            # Fullscreen toggle button (top-right corner)
            btn_x = self.width - 8
            btn_y = title_y
            fs_label = "⛶ EXIT FULL" if self._is_fullscreen else "⛶ FULL"
            self.canvas.create_text(btn_x, btn_y, text=fs_label,
                                    font=self.font_sb_label, fill=self.accent,
                                    anchor="ne", tags="fs_btn")
            self.canvas.tag_bind("fs_btn", "<Button-1>", self._toggle_fullscreen)

            # header team cell
            self.canvas.create_rectangle(team_x - 8, top_margin - hh, score_start_x - 4, top_margin + hh,
                                         fill=self.bg, outline="black")
            self.canvas.create_text(team_x, top_margin, text="TEAM", font=self.font_header, fill=self.accent, anchor="w")

            # inning header cells
            for i in range(max_innings):
                x_center = score_start_x + i * col_width
                self.canvas.create_rectangle(x_center - col_width // 2, top_margin - hh,
                                             x_center + col_width // 2, top_margin + hh,
                                             fill=self.bg, outline="black", tags="inning_header")
                self.canvas.create_text(x_center, top_margin, text=str(i + 1), font=self.font_header, fill=self.accent, tags="inning_header_text")

            # totals headers: R, H, E, extra (bat icon column)
            is_final_hdr = False
            if self.live_feed:
                hdr_state = (self.live_feed.get("gameData", {}).get("status", {}).get("detailedState", "")) or ""
                is_final_hdr = "Final" in hdr_state or "Game Over" in hdr_state
            totals_labels = ("R", "H", "E", "⚾")
            for j, label in enumerate(totals_labels):
                x_center = score_start_x + (max_innings + j) * col_width
                self.canvas.create_rectangle(x_center - col_width // 2, top_margin - hh,
                                             x_center + col_width // 2, top_margin + hh,
                                             fill=self.bg, outline="black")
                if label == "⚾":
                    display = "🏆" if is_final_hdr else "🦇"
                else:
                    display = label
                self.canvas.create_text(x_center, top_margin, text=display, font=self.font_header, fill=self.accent)

            # --- Clean, properly aligned grid overlay ---
            grid_left = team_x - 8
            grid_top = top_margin - hh
            grid_right = score_start_x + (max_innings + 3) * col_width + col_width // 2
            grid_bottom = grid_top + row_height * 3  # header + away + home full enclosure

            for i in range(max_innings + 4):
                x = score_start_x + (i - 0.5) * col_width
                self.canvas.create_line(x, grid_top, x, grid_bottom, fill="#38444d", width=1)

            for j in range(3):
                y = grid_top + (j + 1) * row_height
                self.canvas.create_line(grid_left, y, grid_right, y, fill="#38444d", width=1)

            self.canvas.create_rectangle(grid_left, grid_top, grid_right, grid_bottom, outline="#55606b", width=2)
            
            # Diamond (static polygon) — geometry already set by _compute_layout()
            ds = self.diamond_ds
            diamond_pts = [self.diamond_cx, self.diamond_cy - ds,
                           self.diamond_cx + ds, self.diamond_cy,
                           self.diamond_cx, self.diamond_cy + ds,
                           self.diamond_cx - ds, self.diamond_cy]
            self.canvas.create_polygon(diamond_pts, outline=self.accent, fill="#6b8f57", width=3)
        
        # Draw team rows (colored) and per-inning values
        def draw_team_row(y, name, side, active_idx):
            bg_col, fg_col = team_color_for(name)
            is_followed = (name == self.followed_team_name)

            # Redraw only the dynamic cells for non-full renders
            if full:
                self.canvas.create_rectangle(team_x - 8, y - hh, score_start_x - 4, y + hh,
                                             fill=bg_col, outline="black", width=1)
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

                # Active inning: brighter header background + bold text
                if i == active_idx:
                    bg_fill_header = blend_colors(self.accent, "#000000", 0.55)
                    text_fill_header = "#ffffff"
                    if full:
                        self.canvas.create_rectangle(x1, top_margin - hh, x2, top_margin + hh,
                                                     fill=bg_fill_header, outline="black", tags="inning_header")
                        self.canvas.create_text(score_start_x + i * col_width, top_margin, text=str(i + 1),
                                                font=self.font_header, fill=text_fill_header, tags="inning_header_text")
                else:
                    bg_fill_header = self.bg
                    text_fill_header = self.accent

                # Score cell — score flash: briefly pulse bright yellow when a run scores
                flash_frames = self._score_flash.get(side, 0)
                if flash_frames > 0:
                    # Only flash the last scored inning (active or most recent)
                    flash_inning = active_idx if active_idx >= 0 else (len(innings) - 1)
                    if i == flash_inning:
                        t_flash = flash_frames / 6.0
                        cell_bg = blend_colors("#FFD700", bg_col, 1.0 - t_flash)
                    else:
                        cell_bg = blend_colors(bg_col, self.accent, 0.25) if i == active_idx else bg_col
                else:
                    cell_bg = blend_colors(bg_col, self.accent, 0.25) if i == active_idx else bg_col

                score_tag = f"score_{side}_{i}"
                self.canvas.delete(score_tag)
                self.canvas.create_rectangle(x1, y - hh, x2, y + hh, fill=cell_bg, outline="black", tags=score_tag)
                self.canvas.create_text(score_start_x + i * col_width, y, text=str(run_val), font=self.font_team,
                                        fill=fg_col, tags=score_tag)

            # Decrement flash counter (main thread only, safe here)
            if self._score_flash.get(side, 0) > 0:
                self._score_flash[side] -= 1

            # Totals
            totals = linescore.get("teams", {}).get(side, {})
            for j, key in enumerate(("runs", "hits", "errors")):
                val = str(totals.get(key, "-"))
                x_center = score_start_x + (max_innings + j) * col_width

                total_tag = f"total_{side}_{j}"
                self.canvas.delete(total_tag)
                self.canvas.create_rectangle(x_center - col_width // 2, y - hh, x_center + col_width // 2, y + hh,
                                             fill=bg_col, outline="black", tags=total_tag)
                self.canvas.create_text(x_center, y, text=val, font=self.font_team, fill=fg_col, tags=total_tag)

            # W/L indicator in icon cell when game is final
            x_icon = score_start_x + (max_innings + 3) * col_width
            icon_tag = f"icon_{side}"
            self.canvas.delete(icon_tag)
            self.canvas.create_rectangle(x_icon - col_width // 2, y - hh, x_icon + col_width // 2, y + hh,
                                         fill=bg_col, outline="black", tags=icon_tag)
            # Show W/L if game is final
            if self.live_feed:
                g_state = (self.live_feed.get("gameData", {}).get("status", {}).get("detailedState", "")) or ""
                if "Final" in g_state or "Game Over" in g_state:
                    ls_t2 = self.live_feed.get("liveData", {}).get("linescore", {}).get("teams", {}) or {}
                    a_r = int(ls_t2.get("away", {}).get("runs", 0))
                    h_r = int(ls_t2.get("home", {}).get("runs", 0))
                    if side == "away":
                        wl_text = "W" if a_r > h_r else "L"
                    else:
                        wl_text = "W" if h_r > a_r else "L"
                    wl_color = "#00e676" if wl_text == "W" else "#e74c3c"
                    self.canvas.create_text(x_icon, y, text=wl_text,
                                            font=self.font_status, fill=wl_color, tags=icon_tag)

        draw_team_row(y_away, away, "away", active_inning_idx)
        draw_team_row(y_home, home, "home", active_inning_idx)

        # Fix #12: Clean inning highlight — detect if the last completed inning had 0 runs
        clean_inning = False
        if innings and active_inning_idx > 0:
            last_completed_idx = active_inning_idx - 1
            if last_completed_idx < len(innings):
                li = innings[last_completed_idx]
                away_runs = li.get("away", {}).get("runs", None)
                home_runs = li.get("home", {}).get("runs", None)
                # Both halves must have completed (non-None) and both must be 0
                if away_runs is not None and home_runs is not None:
                    if int(away_runs) == 0 and int(home_runs) == 0:
                        clean_inning = True

        # Diamond + BSO: skip entirely during partial renders while a fetch is running.
        # The elements stay on canvas from the previous render — no flicker.
        # render_full_gui() redraws everything once the fetch completes.
        if _skip_dynamic:
            self.canvas.delete("status_bar")
            self.render_status_bar(away, home)
            return

        # Diamond bases (dynamic part)
        self.canvas.delete("diamond_bases")
        inset = self.diamond_ds * 0.6
        base_half = self.base_half

        # Fix #12: draw a soft glow ring around the diamond for a clean inning
        if clean_inning:
            ds = self.diamond_ds
            glow_off = max(6, int(ds * 0.09))
            glow_pts = [self.diamond_cx, self.diamond_cy - ds - glow_off,
                        self.diamond_cx + ds + glow_off, self.diamond_cy,
                        self.diamond_cx, self.diamond_cy + ds + glow_off,
                        self.diamond_cx - ds - glow_off, self.diamond_cy]
            self.canvas.create_polygon(glow_pts, outline="#00e676", fill="", width=3, tags="diamond_bases")

        for bname, (bx, by) in self.base_positions.items():
            if bname == "Home":
                # Fix #13: draw a visible home plate pentagon
                hp = base_half
                home_pts = [bx - hp, by - hp // 2,
                            bx + hp, by - hp // 2,
                            bx + hp, by + hp // 2,
                            bx,      by + hp,
                            bx - hp, by + hp // 2]
                self.canvas.create_polygon(home_pts, fill="#e8e8e8", outline="white", width=2, tags="diamond_bases")
                self.canvas.create_text(bx, by, text="H", font=self.font_small, fill="#333333", tags="diamond_bases")
                continue

            b = self.bases.get(bname, {"occupied": False, "team": None, "anim": None})
            fill = self.empty_base_fill
            anim = b.get("anim")

            if anim and not anim.get("finished"):
                fill = anim.get("current", self.empty_base_fill)
            elif b.get("occupied"):
                fill = team_color_for(b["team"])[0] if b["team"] else self.accent

            pts = [bx, by - base_half, bx + base_half, by, bx, by + base_half, bx - base_half, by]
            self.canvas.create_polygon(pts, fill=fill, outline="white", width=2, tags="diamond_bases")

            # Runner initials inside base square when occupied
            if b.get("occupied"):
                raw_name = b.get("runner_name") or bname
                initials = raw_name[:3].upper() if raw_name else bname
                self.canvas.create_text(bx, by, text=initials, font=self.font_sb_label,
                                        fill="#ffffff", tags="diamond_bases")
            else:
                self.canvas.create_text(bx, by, text=bname, font=self.font_small,
                                        fill=self.fg, tags="diamond_bases")

        # Base path lines connecting Home → 1B → 2B → 3B → Home (drawn after bases so they appear under base squares)
        # Order: Home, 1B, 2B, 3B — draw lines connecting each consecutive pair
        hp_pos = self.base_positions.get("Home")
        b1_pos = self.base_positions.get("1B")
        b2_pos = self.base_positions.get("2B")
        b3_pos = self.base_positions.get("3B")
        if all(p is not None for p in (hp_pos, b1_pos, b2_pos, b3_pos)):
            path_color = "#aaaaaa"
            path_w = 2
            for p1, p2 in ((hp_pos, b1_pos), (b1_pos, b2_pos), (b2_pos, b3_pos), (b3_pos, hp_pos)):
                self.canvas.create_line(p1[0], p1[1], p2[0], p2[1],
                                        fill=path_color, width=path_w,
                                        dash=(4, 3), tags="diamond_bases")

        # Bat icon (cleared and redrawn inside draw_team_row, just need the final placement)
        # Bat icon — only shown when game is live (not final, so it doesn't overlap W/L)
        is_final = False
        if self.live_feed:
            g_state = (self.live_feed.get("gameData", {}).get("status", {}).get("detailedState", "")) or ""
            is_final = "Final" in g_state or "Game Over" in g_state

        batting_team = None
        if self.live_feed and not is_final:
            ls = self.live_feed.get("liveData", {}).get("linescore", {}) or {}
            inning_half = ls.get("inningHalf") or None
            if inning_half:
                if str(inning_half).lower() == "top":
                    batting_team = away
                elif str(inning_half).lower() == "bottom":
                    batting_team = home

        if batting_team:
            icon = "⚾"
            x_icon = score_start_x + (max_innings + 3) * col_width
            if batting_team == away:
                y_icon = y_away
                icon_tag = "icon_away"
            else:
                y_icon = y_home
                icon_tag = "icon_home"

            self.canvas.create_text(x_icon, y_icon, text=icon, font=self.font_team, fill=self.accent, tags=icon_tag)

        # B/S/O indicator panel — skip during active fetch to prevent flicker
        self.canvas.delete("bso_group")

        bso_x    = self.bso_zone_left + max(48, int(self.bso_spacing * 3.0))
        spacing  = self.bso_spacing
        sz       = self.bso_sz
        balls    = self.balls
        strikes  = self.strikes
        outs     = self.outs
        top_of_bso = self.diamond_cy - spacing

        ind_gap  = max(4, sz // 2)
        lbl_w    = self.font_small.measure("STRIKES")  # fixed label column width

        # Fixed name column x — start after the widest possible indicator group (3 indicators)
        max_ind_w = 3 * (sz * 2) + 2 * ind_gap
        name_x    = bso_x + lbl_w + spacing + max_ind_w + spacing * 3

        # Right edge for name text — leave a small margin
        name_x_end = self.width - max(8, int(spacing * 0.4))

        # Colours
        col_active_ball   = "#f1c40f"
        col_active_strike = "#e67e22"
        col_active_out    = "#e74c3c"
        col_danger_ball   = "#e74c3c"
        col_danger_strike = "#e74c3c"
        col_inactive      = "#2c3e50"
        col_name_dim      = "#aabbcc"

        def draw_square(cx, cy, half, fill, tag):
            self.canvas.create_rectangle(cx - half, cy - half, cx + half, cy + half,
                                         fill=fill, outline="white", width=1, tags=tag)

        def draw_diamond_shape(cx, cy, half, fill, tag):
            pts = [cx, cy - half, cx + half, cy, cx, cy + half, cx - half, cy]
            self.canvas.create_polygon(pts, fill=fill, outline="white", width=1, tags=tag)

        def draw_out_square(cx, cy, half, fill, tag):
            self.canvas.create_rectangle(cx - half, cy - half, cx + half, cy + half,
                                         fill=fill, outline="#ff6b6b", width=2, tags=tag)

        def bso_row(label, cy, n_ind, draw_fn, count, danger_n, col_act, col_dng, name_text="", name_prefix=""):
            # Label
            self.canvas.create_text(bso_x, cy, text=label,
                                    font=self.font_small, fill=self.fg,
                                    anchor="w", tags="bso_group")
            # Indicators
            ind_start = bso_x + lbl_w + spacing
            for i in range(n_ind):
                cx_i = ind_start + sz + i * (sz * 2 + ind_gap)
                fill_c = (col_dng if count == danger_n else col_act) \
                         if (count is not None and i < count) else col_inactive
                draw_fn(cx_i, cy, sz, fill_c, "bso_group")
            # Name — fixed left-aligned column, same x for all rows
            if name_text:
                full_text = f"{name_prefix}{name_text}" if name_prefix else name_text
                avail_px  = name_x_end - name_x
                display   = full_text
                while display and self.font_small.measure(display) > avail_px:
                    display = display[:-1]
                self.canvas.create_text(name_x, cy, text=display,
                                        font=self.font_small, fill=col_name_dim,
                                        anchor="w", tags="bso_group")

        # Strip "Pitcher: " / "Batter: " prefixes — supply them as the prefix arg
        def strip_prefix(text, prefix):
            return text[len(prefix):].strip() if text.startswith(prefix) else text

        pitcher_name = strip_prefix(self.current_pitcher, "Pitcher: ")
        batter_name  = strip_prefix(self.current_batter,  "Batter: ")

        bso_row("BALLS",   top_of_bso - spacing, 3, draw_diamond_shape,
                balls,   3, col_active_ball,   col_danger_ball,
                pitcher_name, "P: ")
        bso_row("STRIKES", top_of_bso + spacing, 2, draw_square,
                strikes, 2, col_active_strike, col_danger_strike,
                batter_name, "B: ")
        bso_row("OUTS",    top_of_bso + spacing * 3, 2, draw_out_square,
                outs,    3, col_active_out,    col_active_out)

        # Status bar (replaces old footer)
        self.canvas.delete("status_bar")
        self.render_status_bar(away, home)


    def render_status_bar(self, away_name, home_name):
        """
        Status bar anchored to the bottom of the canvas.
        Live:     3 rows — marquee (lineup scroller) | game state | at-bat stats
        Not live: 2 rows — game state | next game / at-bat stats
        """
        tag = "status_bar"

        # Determine live status
        state_str = ""
        is_live = False
        if self.live_feed:
            state_str = (self.live_feed.get("gameData", {})
                         .get("status", {}).get("detailedState", "")) or ""
            is_live = "In Progress" in state_str or "Live" in state_str

        # Always 4 rows: marquee | game state | at-bat stats | event ticker
        sbh   = self.STATUS_BAR_H_LIVE
        row_h = sbh // 4
        bar_top = self.height - sbh
        bar_bot = self.height

        row1_cy = bar_top + row_h // 2              # marquee row
        row2_cy = bar_top + row_h + row_h // 2      # game state row
        row3_cy = bar_top + row_h * 2 + row_h // 2  # at-bat stats row
        row4_cy = bar_top + row_h * 3 + row_h // 2  # event ticker row
        sep1    = bar_top + row_h
        sep2    = bar_top + row_h * 2
        sep3    = bar_top + row_h * 3

        # ── Background gradient ───────────────────────────────────────────────
        away_col = team_color_for(away_name)[0]
        home_col = team_color_for(home_name)[0]
        slices = 40
        for i in range(slices):
            t    = i / float(slices)
            fill = blend_colors(away_col, home_col, t)
            x0   = int(self.width * i / slices)
            x1   = int(self.width * (i + 1) / slices) + 1
            self.canvas.create_rectangle(x0, bar_top, x1, bar_bot,
                                         fill=fill, outline="", tags=tag)

        # Dim overlay
        self.canvas.create_rectangle(0, bar_top, self.width, bar_bot,
                                     fill="#000000", stipple="gray50",
                                     outline="", tags=tag)

        # Top border + row separators
        self.canvas.create_line(0, bar_top, self.width, bar_top,
                                fill=self.accent, width=1, tags=tag)
        self.canvas.create_line(0, sep1, self.width, sep1,
                                fill="#ffffff", width=1, tags=tag, stipple="gray25")
        self.canvas.create_line(0, sep2, self.width, sep2,
                                fill="#ffffff", width=1, tags=tag, stipple="gray25")
        self.canvas.create_line(0, sep3, self.width, sep3,
                                fill="#ffffff", width=1, tags=tag, stipple="gray25")

        # ── Marquee row — always active ───────────────────────────────────────
        # Start or refresh the scroller
        if not self._marquee_active:
            self.root.after(0, self._marquee_start)
        else:
            # Rebuild string each render (live/not-live or roster may change)
            text = self._build_marquee_string()
            if text != self._marquee_text:
                self._marquee_text   = text
                self._marquee_text_w = self.font_marquee.measure(text)

        # Row centres for stat rows
        game_row_cy  = row2_cy
        atbat_row_cy = row3_cy
        event_row_cy = row4_cy

        # Helper: draw a labelled stat segment — offsets scale with row height
        lbl_off = max(4, row_h // 5)
        val_off = max(5, row_h // 4)

        def stat_cell(cx, cy, label, value, value_fill="#ffffff"):
            self.canvas.create_text(cx, cy - lbl_off, text=label.upper(),
                                    font=self.font_sb_label, fill="#cccccc",
                                    anchor="center", tags=tag)
            self.canvas.create_text(cx, cy + val_off, text=str(value),
                                    font=self.font_sb_value, fill=value_fill,
                                    anchor="center", tags=tag)

        # ── Game state row ─────────────────────────────────────────────────────
        # Status pill
        if is_live:
            pill_text = "● LIVE"
            pill_fill = "#e74c3c"
        elif "Final" in state_str or "Game Over" in state_str:
            pill_text = "■ FINAL"
            pill_fill = "#7f8c8d"
        elif state_str:
            pill_text = f"◌ {state_str.upper()}"
            pill_fill = "#f39c12"
        else:
            pill_text = "◌ WAITING"
            pill_fill = "#7f8c8d"

        pill_x = max(44, int(self.width * 0.055))
        self.canvas.create_text(pill_x, game_row_cy, text=pill_text,
                                font=self.font_sb_value, fill=pill_fill,
                                anchor="center", tags=tag)

        # Inning indicator
        inning_str = ""
        if self.live_feed:
            ls = self.live_feed.get("liveData", {}).get("linescore", {}) or {}
            inn = ls.get("currentInning")
            half = ls.get("inningHalf", "")
            if inn:
                arrow = "▲" if str(half).lower() == "top" else "▼"
                inning_str = f"{arrow} {inn}"
        if inning_str:
            stat_cell(max(120, int(self.width * 0.165)), game_row_cy, "inning", inning_str)

        # Weather (centre of game state row)
        if self.sb_weather:
            stat_cell(self.width // 2, game_row_cy, "weather", self.sb_weather)

        # API Status pill
        elapsed_since_status = time.time() - self._poll_status_set_at
        effective_poll_status = self._poll_status
        if self._poll_status != "pending" and elapsed_since_status > 300 and self.next_update_in > 300:
            effective_poll_status = "pending"
        poll_status_text = {
            "updated":   "  updated  ",
            "unchanged": " no change ",
            "error":     "   error   ",
            "pending":   "  pending  ",
        }.get(effective_poll_status, "  pending  ")
        poll_bg_color = {
            "updated":   "#00c853",
            "unchanged": "#f9a825",
            "error":     "#c62828",
            "pending":   "#616161",
        }.get(effective_poll_status, "#616161")
        pill_fg = "#000000"
        api_pill_x = self.width - 8
        pill_w = self.font_sb_value.measure(poll_status_text)
        pill_h = self.font_sb_value.metrics("linespace")
        pill_cx = api_pill_x - pill_w // 2 - 2
        self.canvas.create_text(pill_cx, game_row_cy - lbl_off, text="API STATUS",
                                font=self.font_sb_label, fill="#cccccc",
                                anchor="center", tags=tag)
        pill_cy = game_row_cy + val_off
        self.canvas.create_rectangle(api_pill_x - pill_w - 4, pill_cy - pill_h // 2,
                                     api_pill_x + 4, pill_cy + pill_h // 2,
                                     fill=poll_bg_color, outline="", tags=tag)
        self.canvas.create_text(api_pill_x, pill_cy, text=poll_status_text,
                                font=self.font_sb_value, fill=pill_fg,
                                anchor="e", tags=tag)

        # Next poll countdown
        time_display = self.format_seconds_to_dhms_string(self.next_update_in)
        stat_cell(self.width - max(80, int(self.width * 0.11)), game_row_cy, "next poll", time_display)

        # Error indicator
        if self._fetch_error:
            self.canvas.create_text(self.width - max(160, int(self.width * 0.22)), game_row_cy,
                                    text=f"⚠ {self._fetch_error_msg}",
                                    font=self.font_sb_label, fill="#e67e22",
                                    anchor="center", tags=tag)

        # ── At-bat stats row ───────────────────────────────────────────────────
        # Only show live at-bat stats when a game is in progress
        if not is_live:
            if self.next_game and "gameDate_dt" in self.next_game:
                try:
                    dt = self.next_game["gameDate_dt"].astimezone()
                    away_n = get_team_name(self.next_game["teams"]["away"])
                    home_n = get_team_name(self.next_game["teams"]["home"])
                    next_txt = f"{away_n} @ {home_n}  —  {dt.strftime('%a %b %d  %I:%M %p %Z')}"
                except Exception:
                    next_txt = "Next game info unavailable"
            else:
                next_txt = f"No upcoming games found for {self.followed_team_name}"
            self.canvas.create_text(self.width // 2, atbat_row_cy, text=next_txt,
                                    font=self.font_sb_value, fill="#ffffff",
                                    anchor="center", tags=tag)
            return

        # Last pitch
        if self.sb_last_pitch_speed is not None and self.sb_last_pitch_type:
            pitch_str = f"{self.sb_last_pitch_speed:.0f} mph  {self.sb_last_pitch_type}"
        elif self.sb_last_pitch_speed is not None:
            pitch_str = f"{self.sb_last_pitch_speed:.0f} mph"
        elif self.sb_last_pitch_type:
            pitch_str = self.sb_last_pitch_type
        else:
            pitch_str = "—"
        stat_cell(max(90, int(self.width * 0.12)), atbat_row_cy, "last pitch", pitch_str)

        # Team records — centred, away on left, home on right
        rec_cx  = self.width // 2
        rec_off = max(80, int(self.width * 0.11))
        away_rec = self.sb_away_record or "—"
        home_rec = self.sb_home_record or "—"
        away_rec_col = team_color_for(away_name)[1] or "#ffffff"
        home_rec_col = team_color_for(home_name)[1] or "#ffffff"
        # Last word of team name as nickname label (e.g. "Astros", "Mets")
        def nickname(name):
            return name.split()[-1] if name else "???"

        stat_cell(rec_cx - rec_off, atbat_row_cy, nickname(away_name), away_rec, value_fill=away_rec_col)
        stat_cell(rec_cx + rec_off, atbat_row_cy, nickname(home_name), home_rec, value_fill=home_rec_col)

        # Batter AVG / OBP
        avg_str = self.sb_batter_avg if self.sb_batter_avg else ".---"
        obp_str = self.sb_batter_obp if self.sb_batter_obp else ".---"
        stat_cell(self.width - max(150, int(self.width * 0.20)), atbat_row_cy, "AVG / OBP", f"{avg_str}  /  {obp_str}")

        # Pitcher pitch count
        pc_str = str(self.sb_pitch_count) if self.sb_pitch_count is not None else "—"
        stat_cell(self.width - max(55, int(self.width * 0.073)), atbat_row_cy, "pitches", pc_str)

        # ── Row 4: event ticker ────────────────────────────────────────────────
        # Show transient status (challenge, delay, etc.) for 8s, then last play description
        EVENT_TTL = 8.0
        elapsed_event = time.time() - self.sb_event_msg_set_at
        # Last play expires after 2 poll cycles, capped at 60s
        play_ttl = min(2 * self.poll_interval, 60)
        play_fresh = self.sb_last_play and (time.time() - self.sb_last_play_set_at) < play_ttl
        ind_x = max(12, int(self.width * 0.016))
        msg_x = ind_x + max(10, int(self.width * 0.022))
        if self.sb_event_msg and elapsed_event < EVENT_TTL:
            pulse_on = int(elapsed_event * 2) % 2 == 0
            indicator_fill = "#f39c12" if pulse_on else "#c0820a"
            self.canvas.create_text(ind_x, event_row_cy, text="▶",
                                    font=self.font_sb_value, fill=indicator_fill,
                                    anchor="center", tags=tag)
            # Truncate by measuring until it fits
            avail = self.width - msg_x - 8
            msg = self.sb_event_msg
            while msg and self.font_sb_value.measure(msg) > avail:
                msg = msg[:-1]
            self.canvas.create_text(msg_x, event_row_cy, text=msg,
                                    font=self.font_sb_value, fill="#f39c12",
                                    anchor="w", tags=tag)
        elif play_fresh:
            avail = self.width - 16
            desc = self.sb_last_play
            while desc and self.font_sb_value.measure(desc) > avail:
                desc = desc[:-1]
            if len(desc) < len(self.sb_last_play):
                desc = desc[:-1] + "…"
            self.canvas.create_text(self.width // 2, event_row_cy, text=desc,
                                    font=self.font_sb_value, fill="#ffffff",
                                    anchor="center", tags=tag)
        else:
            self.canvas.create_text(self.width // 2, event_row_cy,
                                    text="Awaiting play data…",
                                    font=self.font_sb_label, fill="#888888",
                                    anchor="center", tags=tag)

    def start_fade(self, base_key, team_color, duration_ms=1200, steps=16):
        """Starts a base fade animation (Must be called on main thread)."""
        if threading.current_thread() != threading.main_thread():
             self.root.after(0, lambda: self.start_fade(base_key, team_color, duration_ms, steps))
             return
        
        start = self.empty_base_fill
        end = team_color or self.accent
        step_ms = max(20, int(duration_ms / steps))
        
        # Reset animation state if starting a new one
        if base_key not in self.bases:
            self.bases[base_key] = {"occupied": False, "team": None, "anim": None}
            
        anim = {"step": 0, "steps": steps, "start": start, "end": end, "current": start, "finished": False}
        self.bases[base_key]["anim"] = anim

        def _step():
            if base_key not in self.bases or not self.bases[base_key]["anim"]:
                # Animation cancelled (e.g., 3rd out reset)
                return

            s = anim["step"]
            t = s / float(anim["steps"])
            anim["current"] = blend_colors(anim["start"], anim["end"], t)
            
            # Partial render to update the base color
            self.render(full=False) 
            anim["step"] += 1
            
            if anim["step"] <= anim["steps"]:
                self.root.after(step_ms, _step)
            else:
                anim["finished"] = True
                anim["current"] = anim["end"]
                self.render(full=False)

        self.root.after(0, _step)

    def update_loop(self):
        """Main loop that controls polling timing and schedules fetch."""
        
        # Using executor.submit to manage the thread
        if self.next_update_in <= 0 and not self.running_fetch:
            self.running_fetch = True # Flag set before submission
            # Submit to ThreadPoolExecutor
            self.executor.submit(self.fetch_and_schedule)
            
        if self.next_update_in > 0:
            self.next_update_in -= 1
        
        # only log B/S/O changes to avoid per-second spam
        current_state = (self.balls, self.strikes, self.outs)
            
        # Partial render for base fade animation and footer update
        self.render(full=False)
        self.root.after(1000, self.update_loop)

    def fetch_and_schedule(self):
        """Fetches game data, updates state, and schedules GUI updates (Runs in background thread)."""
        try:
            games = fetch_schedule(self.team_id)
            self.log(f"Schedule fetched — {len(games)} game(s) in window", verbose=True, cat="POLL")
            # Fix #11: clear error flag on successful fetch
            with _STATE_LOCK:
                self._fetch_error = False
                self._fetch_error_msg = ""
            if not games and self.live_game is None and self.last_game is None:
                # Empty schedule returned — could be a transient network issue
                with _STATE_LOCK:
                    self._fetch_error = True
                    self._fetch_error_msg = "Schedule unavailable"
                    self._set_poll_status("error")
            self.games = games
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            live_game = None
            last_game = None
            next_game = None
            
            for g in games:
                gd = g.get("gameDate_dt")
                state = g.get("status", {}).get("detailedState", "") or ""
                
                # Find the most recent "finished" game
                if gd and state in ("Final", "Game Over") and gd.astimezone(datetime.timezone.utc) <= now_utc:
                    last_game = g
                # Identify the single currently live game
                if state == "In Progress":
                    live_game = g
                    
                # Find the *next* scheduled game (since games are sorted, first match is the next)
                if gd and gd.astimezone(datetime.timezone.utc) >= now_utc and not next_game:
                    # Ignore a game that's just started as 'next' if we have a live game
                    if live_game and live_game["gamePk"] == g["gamePk"]:
                        continue
                    next_game = g

            # Build recent results strip from last 5 completed games
            new_recent = []
            for g in games:
                state = g.get("status", {}).get("detailedState", "") or ""
                gd = g.get("gameDate_dt")
                if state in ("Final", "Game Over") and gd and gd.astimezone(datetime.timezone.utc) <= now_utc:
                    try:
                        g_teams = g.get("teams", {})
                        home_side = g_teams.get("home", {})
                        away_side = g_teams.get("away", {})
                        home_name = (home_side.get("team") or {}).get("name", "")
                        away_name = (away_side.get("team") or {}).get("name", "")
                        home_score = home_side.get("score", 0)
                        away_score = away_side.get("score", 0)
                        # Determine if followed team won
                        followed = self.followed_team_name or ""
                        if followed in (home_name, away_name):
                            if home_name == followed:
                                wl = "W" if home_side.get("isWinner") else "L"
                                opp = away_name
                                score = f"{home_score}-{away_score}"
                            else:
                                wl = "W" if away_side.get("isWinner") else "L"
                                opp = home_name
                                score = f"{away_score}-{home_score}"
                            new_recent.append({"wl": wl, "score": score, "opp": opp})
                    except Exception:
                        pass
            with _STATE_LOCK:
                self.recent_results = new_recent[-5:]  # keep last 5

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

            feed = None
            if chosen:
                feed = fetch_live_feed(chosen.get("gamePk"))
                # Fix #11: flag a feed failure distinctly from a no-game state
                if feed is None:
                    with _STATE_LOCK:
                        self._fetch_error = True
                        self._fetch_error_msg = "Live feed unavailable"
                        self._set_poll_status("error")
                else:
                    with _STATE_LOCK:
                        self._fetch_error = False
                        self._fetch_error_msg = ""
                    # ── Key event: game status change ─────────────────────────
                    new_status = (feed.get("gameData", {}).get("status", {}).get("detailedState", "")) or ""
                    # Transient states — log them but don't update _last_game_status
                    # so the transition back to "In Progress" isn't treated as a new change
                    _TRANSIENT_STATES = {
                        "manager challenge", "player challenge",
                        "review", "instant replay", "delayed",
                    }
                    is_transient = any(t in new_status.lower() for t in _TRANSIENT_STATES)

                    if new_status and new_status != self._last_game_status:
                        gd_t = feed.get("gameData", {}).get("teams", {}) or {}
                        away_t = gd_t.get("away", {}).get("name", "Away")
                        home_t = gd_t.get("home", {}).get("name", "Home")
                        self.log(f"Game status: {self._last_game_status or '—'} → {new_status}  ({away_t} @ {home_t})", level="info", cat="GAME")
                        if is_transient:
                            with _STATE_LOCK:
                                self.sb_event_msg = new_status
                                self.sb_event_msg_set_at = time.time()
                        else:
                            self._last_game_status = new_status
                self.live_feed = feed
                # Fix #4: only invoke each recorder when its path is actually configured
                if RECORD_FULL_PATH:
                    record_live_feed(feed, chosen, full=True)
                if RECORD_PATH:
                    record_live_feed(feed, chosen, full=False)
            else:
                self.live_feed = None

            # Timestamp-based skip: if metaData.timeStamp hasn't changed since last poll,
            # the feed data is identical — skip state extraction and full re-render.
            # The countdown still ticks (update_loop handles that via partial render).
            feed_unchanged = False
            if feed:
                new_ts = feed.get("metaData", {}).get("timeStamp")
                if new_ts and new_ts == self._last_feed_timestamp:
                    feed_unchanged = True
                    self.log(f"Feed timestamp unchanged ({new_ts}), skipping re-render.", verbose=True, cat="POLL")
                    with _STATE_LOCK:
                        self._set_poll_status("unchanged")
                else:
                    self._last_feed_timestamp = new_ts
                    with _STATE_LOCK:
                        self._set_poll_status("updated")

            if self.live_feed and not feed_unchanged:
                # --- State Extraction and 3rd Out Logic ---
                # Fix #7: compute values locally first, then write to shared state under lock
                raw_balls = 0
                raw_strikes = 0
                raw_outs = 0
                try:
                    current_play = self.live_feed.get("liveData", {}).get("plays", {}).get("currentPlay", {}) or {}
                    counts = current_play.get("count", {}) or {}
                    raw_balls = int(counts.get("balls", 0))
                    raw_strikes = int(counts.get("strikes", 0))
                except Exception:
                    pass
                try:
                    raw_outs = int(self.live_feed.get("liveData", {}).get("linescore", {}).get("outs", 0))
                except Exception:
                    pass

                ls_hdr = self.live_feed.get("liveData", {}).get("linescore", {}) or {}
                curr_inning = ls_hdr.get("currentInning")
                curr_half = ls_hdr.get("inningHalf")

                # Compute new batter/pitcher names
                new_batter = "Batter: -"
                new_pitcher = "Pitcher: -"
                try:
                    current_play = self.live_feed.get("liveData", {}).get("plays", {}).get("currentPlay", {}) or {}
                    matchup = current_play.get("matchup", {}) or {}
                    batter = matchup.get("batter", {}).get("fullName")
                    pitcher = matchup.get("pitcher", {}).get("fullName")
                    new_batter = f"Batter: {batter}" if batter else "Batter: -"
                    new_pitcher = f"Pitcher: {pitcher}" if pitcher else "Pitcher: -"
                except Exception:
                    pass

                # --- Status bar stat extraction ---
                new_pitch_speed = None
                new_pitch_type = None
                new_away_record = None
                new_home_record = None
                new_batter_avg = None
                new_batter_obp = None
                new_pitch_count = None
                try:
                    current_play = self.live_feed.get("liveData", {}).get("plays", {}).get("currentPlay", {}) or {}

                    # Last pitch speed + type: walk playEvents in reverse to find last real pitch
                    play_events = current_play.get("playEvents") or []
                    for evt in reversed(play_events):
                        if evt.get("isPitch"):
                            spd = (evt.get("pitchData") or {}).get("startSpeed")
                            typ = ((evt.get("details") or {}).get("type") or {}).get("description")
                            if spd is not None:
                                new_pitch_speed = float(spd)
                            if typ:
                                new_pitch_type = typ
                            break

                    # Last play description
                    new_last_play = (current_play.get("result") or {}).get("description") or None

                    # Team records from gameData
                    gd_rec = self.live_feed.get("gameData", {}).get("teams", {}) or {}
                    for side, attr in (("away", "new_away_record"), ("home", "new_home_record")):
                        rec = gd_rec.get(side, {}).get("record", {}) or {}
                        w = rec.get("wins")
                        l = rec.get("losses")
                        if w is not None and l is not None:
                            val = f"{w}W · {l}L"
                            if side == "away":
                                new_away_record = val
                            else:
                                new_home_record = val

                    # Batter season stats from boxscore players map
                    batter_name = (matchup.get("batter") or {}).get("fullName")
                    boxscore_teams = (self.live_feed.get("liveData", {}).get("boxscore", {}) or {}).get("teams", {})
                    for side in ("home", "away"):
                        players = (boxscore_teams.get(side) or {}).get("players") or {}
                        for pid, pdata in players.items():
                            if (pdata.get("person") or {}).get("fullName") == batter_name:
                                ss = (pdata.get("seasonStats") or {}).get("batting") or {}
                                new_batter_avg = ss.get("avg")
                                new_batter_obp = ss.get("obp")
                                break
                        if new_batter_avg is not None:
                            break

                    # Pitcher pitch count from boxscore
                    pitcher_name = (matchup.get("pitcher") or {}).get("fullName")
                    for side in ("home", "away"):
                        players = (boxscore_teams.get(side) or {}).get("players") or {}
                        for pid, pdata in players.items():
                            if (pdata.get("person") or {}).get("fullName") == pitcher_name:
                                gs = (pdata.get("stats") or {}).get("pitching") or {}
                                pc = gs.get("pitchesThrown")
                                if pc is not None:
                                    new_pitch_count = int(pc)
                                break
                        if new_pitch_count is not None:
                            break
                except Exception as e:
                    if DEBUG:
                        _log("DEBUG", "FEED", f"Status bar stat extraction error: {e}")

                # --- Runner last names from linescore.offense ---
                new_runner_names = {"1B": None, "2B": None, "3B": None}
                try:
                    ls_off2 = self.live_feed.get("liveData", {}).get("linescore", {}).get("offense", {}) or {}
                    for key, bkey in (("first", "1B"), ("second", "2B"), ("third", "3B")):
                        ent = ls_off2.get(key)
                        if ent:
                            ln = ent.get("lastName") or ent.get("fullName", "")
                            new_runner_names[bkey] = ln[:6] if ln else None
                except Exception:
                    pass

                # --- Weather ---
                new_weather = None
                try:
                    wx = self.live_feed.get("gameData", {}).get("weather", {}) or {}
                    cond = wx.get("condition")
                    temp = wx.get("temp")
                    if cond and temp:
                        new_weather = f"{temp}°F  {cond}"
                    elif cond:
                        new_weather = cond
                except Exception:
                    pass

                # --- Score flash detection ---
                new_runs_away = 0
                new_runs_home = 0
                try:
                    ls_teams = self.live_feed.get("liveData", {}).get("linescore", {}).get("teams", {}) or {}
                    new_runs_away = int(ls_teams.get("away", {}).get("runs", 0))
                    new_runs_home = int(ls_teams.get("home", {}).get("runs", 0))
                except Exception:
                    pass

                with _STATE_LOCK:
                    # ── Key event: inning / half-inning change ────────────────
                    if (curr_inning, curr_half) != (self._last_inning, self._last_inning_half):
                        self._inning_reset_done = False
                        if curr_inning and curr_half:
                            arrow = "▲" if str(curr_half).lower() == "top" else "▼"
                            self.log(f"{arrow} Inning {curr_inning} ({curr_half})", level="info", cat="GAME")
                        self._last_inning = curr_inning
                        self._last_inning_half = curr_half

                    if raw_outs >= 3 and not self._inning_reset_done:
                        self.log("Third out — resetting counts and bases.", level="info", cat="GAME")
                        self.root.after(0, self.reset_after_third_out)
                        self.balls = 0
                        self.strikes = 0
                        self.outs = 0
                        self._inning_reset_done = True
                    else:
                        self.balls = max(0, min(3, raw_balls))
                        self.strikes = max(0, min(2, raw_strikes))
                        self.outs = max(0, min(2, raw_outs))

                    # ── Key event: pitching change ────────────────────────────
                    if new_pitcher not in ("Pitcher: -", "Pitcher: ") and new_pitcher != self.current_pitcher:
                        old_p = self.current_pitcher.replace("Pitcher: ", "").strip() or "—"
                        new_p = new_pitcher.replace("Pitcher: ", "").strip()
                        if old_p != "—" and old_p != new_p:
                            self.log(f"Pitching change: {old_p} → {new_p}", level="info", cat="GAME")

                    self.current_batter = new_batter
                    self.current_pitcher = new_pitcher
                    self.sb_last_pitch_speed = new_pitch_speed
                    self.sb_last_pitch_type = new_pitch_type
                    # Smooth win probability
                    self.sb_away_record = new_away_record
                    self.sb_home_record = new_home_record
                    if new_last_play:
                        self.sb_last_play = new_last_play
                        self.sb_last_play_set_at = time.time()
                    self.sb_batter_avg = new_batter_avg
                    self.sb_batter_obp = new_batter_obp
                    self.sb_pitch_count = new_pitch_count
                    self.runner_names = new_runner_names
                    self.sb_weather = new_weather

                    # ── Key event: run(s) scored ──────────────────────────────
                    gd_ev = (self.live_feed.get("gameData", {}).get("teams", {}) or {}) if self.live_feed else {}
                    away_team = gd_ev.get("away", {}).get("name", "Away")
                    home_team = gd_ev.get("home", {}).get("name", "Home")
                    if new_runs_away > self._prev_runs["away"]:
                        diff = new_runs_away - self._prev_runs["away"]
                        self.log(f"Run scored — {away_team} +{diff} (now {new_runs_away}-{new_runs_home})", level="info", cat="GAME")
                        self._score_flash["away"] = 6
                    if new_runs_home > self._prev_runs["home"]:
                        diff = new_runs_home - self._prev_runs["home"]
                        self.log(f"Run scored — {home_team} +{diff} (now {new_runs_away}-{new_runs_home})", level="info", cat="GAME")
                        self._score_flash["home"] = 6
                    self._prev_runs = {"away": new_runs_away, "home": new_runs_home}

                # --- Marquee roster extraction ---
                new_followed_roster = []
                new_opponent_roster = []
                try:
                    current_play_m = self.live_feed.get("liveData", {}).get("plays", {}).get("currentPlay", {}) or {}
                    matchup_m = current_play_m.get("matchup", {}) or {}
                    gd_teams = self.live_feed.get("gameData", {}).get("teams", {}) or {}
                    home_team_name = gd_teams.get("home", {}).get("name", "")
                    followed = self.followed_team_name or ""
                    followed_side = "home" if home_team_name == followed else "away"
                    opponent_side = "away" if followed_side == "home" else "home"
                    bs_teams = (self.live_feed.get("liveData", {}).get("boxscore", {}) or {}).get("teams", {})

                    def build_roster(side):
                        players = (bs_teams.get(side) or {}).get("players") or {}
                        roster = []
                        for pid, pdata in players.items():
                            bo = pdata.get("battingOrder")
                            if not bo:
                                continue
                            person    = pdata.get("person") or {}
                            full_name = person.get("fullName", "")
                            first     = person.get("firstName", "")
                            last      = full_name.split()[-1] if full_name else ""
                            initial   = (first[0] + ".") if first else ""
                            display   = f"{initial} {last}".strip() if initial else last
                            pos       = (pdata.get("position") or {}).get("abbreviation", "")
                            entry_str = f"{display} {pos}".strip()
                            roster.append({
                                "str":       entry_str,
                                "name_str":  display,
                                "full_name": full_name,
                                "order":     int(str(bo)[:1]),
                            })
                        roster.sort(key=lambda p: p["order"])
                        return roster

                    new_followed_roster = build_roster(followed_side)
                    new_opponent_roster = build_roster(opponent_side)
                except Exception as e:
                    if DEBUG:
                        _log("DEBUG", "FEED", f"Marquee roster extraction error: {e}")

                with _STATE_LOCK:
                    self.marquee_followed_roster = new_followed_roster
                    self.marquee_opponent_roster = new_opponent_roster

                # --- Followed team season stats (for not-live marquee) ---
                new_season_stats = []
                try:
                    gd_teams2 = self.live_feed.get("gameData", {}).get("teams", {}) or {}
                    home_name2 = gd_teams2.get("home", {}).get("name", "")
                    followed2  = self.followed_team_name or ""
                    f_side2    = "home" if home_name2 == followed2 else "away"
                    bs_teams2  = (self.live_feed.get("liveData", {}).get("boxscore", {}) or {}).get("teams", {})
                    players2   = (bs_teams2.get(f_side2) or {}).get("players") or {}

                    batters  = []
                    pitchers = []
                    for pid, pdata in players2.items():
                        person    = pdata.get("person") or {}
                        full_name = person.get("fullName", "")
                        first     = person.get("firstName", "")
                        last      = full_name.split()[-1] if full_name else ""
                        initial   = (first[0] + ".") if first else ""
                        display   = f"{initial} {last}".strip() if initial else last
                        pos       = (pdata.get("position") or {}).get("abbreviation", "")
                        ss        = pdata.get("seasonStats") or {}
                        bo        = pdata.get("battingOrder")

                        if bo:
                            # Position player — skip if no meaningful batting stats
                            bat = ss.get("batting") or {}
                            avg = bat.get("avg")
                            hr  = bat.get("homeRuns")
                            rbi = bat.get("rbi")
                            # Consider stats absent if avg is missing or is the placeholder ".000"
                            has_stats = (avg is not None and avg not in (".000", "0.000", "-.---", ".---")
                                         or (hr is not None and int(hr) > 0)
                                         or (rbi is not None and int(rbi) > 0))
                            if not has_stats:
                                continue
                            avg_str = avg if avg else ".---"
                            hr_str  = str(hr) if hr is not None else "-"
                            rbi_str = str(rbi) if rbi is not None else "-"
                            entry_str = f"{display} {pos}  {avg_str}  {hr_str}HR  {rbi_str}RBI"
                            batters.append({
                                "str":        entry_str,
                                "name_str":   display,
                                "full_name":  full_name,
                                "order":      int(str(bo)[:1]),
                                "is_pitcher": False,
                            })
                        else:
                            # Pitcher — only include if they have real pitching stats
                            pit  = ss.get("pitching") or {}
                            wins = pit.get("wins")
                            era  = pit.get("era")
                            has_stats = (era is not None and era not in ("-.--", "0.00", "-")
                                         and not (wins == 0 and era in ("0.00", "-.--")))
                            if not has_stats:
                                continue
                            wins_str = str(wins) if wins is not None else "-"
                            era_str  = str(era)
                            entry_str = f"{display} {pos}  {wins_str}W  {era_str} ERA"
                            pitchers.append({
                                "str":        entry_str,
                                "name_str":   display,
                                "full_name":  full_name,
                                "order":      999,
                                "is_pitcher": True,
                            })

                    batters.sort(key=lambda p: p["order"])
                    new_season_stats = batters + pitchers
                except Exception as e:
                    if DEBUG:
                        _log("DEBUG", "FEED", f"Season stats extraction error: {e}")

                with _STATE_LOCK:
                    self.followed_season_stats = new_season_stats
                
                # 1. Reset base state (in the current thread)
                for k in self.bases:
                    self.bases[k]["occupied"] = False
                    self.bases[k]["team"] = None

                # 2. Update occupancy from linescore (source of truth for base fill)
                try:
                    ls_off = self.live_feed.get("liveData", {}).get("linescore", {}).get("offense", {}) or {}
                    for key, bkey in (("first", "1B"), ("second", "2B"), ("third", "3B")):
                        ent = ls_off.get(key)
                        if ent:
                            self.bases[bkey]["occupied"] = True
                            t = ent.get("team") or {}
                            self.bases[bkey]["team"] = t.get("name") if isinstance(t, dict) else t
                            # Store runner last name for display inside runner dot
                            self.bases[bkey]["runner_name"] = ent.get("lastName") or ent.get("fullName", "")
                        else:
                            self.bases[bkey]["runner_name"] = None
                except Exception:
                    if DEBUG:
                        _log("DEBUG", "FEED", f"Error processing linescore.offense for base occupancy (thread {threading.get_ident()})")
                
                # 3. Check occupancy changes to trigger base fade/runner spawn/path animation
                # Build sets of bases that gained and lost runners this poll
                gained = []  # bases that now have a runner that didn't before
                lost   = []  # bases that lost a runner

                for b in ("1B", "2B", "3B"):
                    was_occ, was_team = prev_base_runners[b]
                    now_occ = self.bases[b]["occupied"]
                    now_team = self.bases[b]["team"]

                    if now_occ and not was_occ:
                        gained.append(b)
                        team_col   = team_color_for(now_team)[0] if now_team else self.accent
                        runner_col = team_color_for(now_team)[1] if now_team else self.accent
                        self.root.after(0, lambda b=b, c=team_col: self.start_fade(b, c))
                        if b not in self.runners_by_base:
                            self.root.after(0, lambda b=b, c=runner_col: self.spawn_runner_at_base(b, color=c))

                    if not now_occ and was_occ:
                        lost.append(b)
                        if b in self.runners_by_base:
                            rkey = self.runners_by_base.pop(b, None)
                            if rkey:
                                info = self.runners.pop(rkey, None)
                                if info:
                                    self.root.after(0, lambda c=info.get("cid"): self.canvas.delete(c))
                        self.bases[b]["anim"] = None

                # Infer movements from occupancy deltas and animate path lines.
                # BASE_ORDER defines valid forward movement: Home→1B→2B→3B→Home
                base_order_idx = {"Home": 0, "1B": 1, "2B": 2, "3B": 3}
                used_gained = set()
                for src in lost:
                    src_idx = base_order_idx.get(src, -1)
                    # Find the most likely destination: a gained base that is ahead in order
                    best_dst = None
                    best_dist = 999
                    for dst in gained:
                        if dst in used_gained:
                            continue
                        dst_idx = base_order_idx.get(dst, -1)
                        dist = (dst_idx - src_idx) % 4
                        if 0 < dist < best_dist:
                            best_dist = dist
                            best_dst = dst
                    if best_dst:
                        used_gained.add(best_dst)
                        now_team = self.bases[best_dst]["team"]
                        anim_col = team_color_for(now_team)[1] if now_team else self.accent
                        self.root.after(0, lambda s=src, e=best_dst, c=anim_col:
                                        self.animate_base_path(s, e, c))
                        self.log(f"Inferred runner movement {src} → {best_dst}", level="info", cat="GAME")

                # Also check if a runner scored (lost a base, no matching gained)
                # Those are handled by move_runner_base scoring logic elsewhere

                # 4. Process currentPlay.runners for *movement/animations*
                try:
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
                            # Schedule runner movement and base path animation on the main thread
                            self.root.after(0, lambda s=sk, e=ek, c=color: self.move_runner_base(s, e, c))
                            self.root.after(0, lambda s=sk, e=ek, c=color: self.animate_base_path(s, e, c))
                        elif ek and ek != "Home":
                            # Runner appeared (e.g., batter on 1B), spawn if not there (handled by occupancy logic, but kept for redundancy)
                            if ek not in self.runners_by_base:
                                self.root.after(0, lambda e=ek, c=color: self.spawn_runner_at_base(e, color=c))

                except Exception:
                    if DEBUG:
                        _log("DEBUG", "UI", f"Error processing currentPlay.runners for animations (thread {threading.get_ident()})")
                
                now = time.time()
                if now - self._last_poll_time > 5:
                    self.log("Successfully polled feed and updated state", verbose=True, cat="POLL")
                    self._last_poll_time = now
            else:
                # No live feed - clear BSO/names/bases
                self.current_batter = "Batter: -"
                self.current_pitcher = "Pitcher: -"
                self.balls = 0
                self.strikes = 0
                self.outs = 0
                for k in self.bases:
                    self.bases[k]["occupied"] = False
                    self.bases[k]["team"] = None
                    self.bases[k]["anim"] = None
                self.root.after(0, self.clear_all_runners)
                self._inning_reset_done = False # Reset flag if game ends/switches

            # --- Smart Polling Calculation ---
            if live_game:
                new_poll_interval = self.polling.get("live", 30)
            elif next_game and next_game.get("gameDate_dt"):
                dt_next = next_game["gameDate_dt"].astimezone()
                dt_now = datetime.datetime.now(dt_next.tzinfo)
                time_to_next = (dt_next - dt_now).total_seconds()

                pre_game_poll = self.polling.get("pre_game", 60)   # <1hr: check every 60s
                min_poll = self.polling.get("scheduled", 300)
                one_hour = 3600
                max_smart_poll = 86400  # never go dormant longer than 1 day

                if time_to_next <= 0:
                    new_poll_interval = self.polling.get("live", 30)
                elif time_to_next > one_hour:
                    # Wait until 1 hour before start, capped at 1 day
                    wait_interval = min(max_smart_poll, max(min_poll, time_to_next - one_hour))
                    new_poll_interval = int(wait_interval)
                else:
                    # Within 1 hour of first pitch: poll every 60s to catch the start promptly
                    new_poll_interval = pre_game_poll

                if self.debug:
                    self.log(f"Next game in: {self.format_seconds_to_dhms_string(time_to_next)} ({time_to_next:.0f}s). Poll interval: {new_poll_interval}s.", verbose=True, cat="POLL")

            else:
                # No next game found
                new_poll_interval = self.polling.get("none", 3600)

            # Fix #7: acquire lock for the final batch write of all shared state
            with _STATE_LOCK:
                self.poll_interval = new_poll_interval
                self.next_update_in = new_poll_interval

            # Only do a full GUI render when the feed actually changed.
            # When feed_unchanged=True the countdown still ticks via update_loop's partial render.
            if not feed_unchanged:
                self._feed_unchanged = False
                self.root.after(0, self.render_full_gui)
            else:
                self._feed_unchanged = True

        finally:
            self.running_fetch = False

    def reset_after_third_out(self):
        """Resets all bases, runners, and clears animation state (Must be called on main thread)."""
        if threading.current_thread() != threading.main_thread():
             self.root.after(0, self.reset_after_third_out)
             return
             
        for b in ("1B", "2B", "3B"):
            self.bases[b]["occupied"] = False
            self.bases[b]["team"] = None
            self.bases[b]["anim"] = None
        
        self.clear_all_runners()
        self._outs_reset_pending = False
        self._inning_reset_done = True # Keep this true until next half-inning change is detected
        self.log("Bases and runners cleared after 3rd out", level="info", cat="GAME")
        # Ensure a render happens to show the cleared bases
        self.render_full_gui()

# Entrypoint
def main():
    root = tk.Tk()
    # --- Ctrl+C Signal Handler ---
    def sigint_handler(signum, frame):
        """Handles SIGINT (Ctrl+C) for clean exit."""
        print(f"\n{_ts()} [INFO/APP] Caught Ctrl+C. Shutting down gracefully...")
        if app.running_fetch:
            print(f"{_ts()} [INFO/APP] Waiting for ongoing fetch thread to finish...")
        
        # Shutdown the executor to prevent new tasks
        app.executor.shutdown(wait=False)
        app._marquee_stop()
        root.quit()

    signal.signal(signal.SIGINT, sigint_handler)

    root.title("MLB Canvas Scoreboard (final v8)")
    app = ScoreboardApp(root)
    # Fix #9: reflect the followed team in the window title
    root.title(f"{app.followed_team_name} — MLB Scoreboard")
    print(f"{_ts()} [INFO/APP] MLB Scoreboard started — following {app.followed_team_name}")
    
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass # Handled by sigint_handler


if __name__ == "__main__":
    main()
