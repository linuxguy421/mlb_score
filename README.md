# MLB Canvas Scoreboard

## Overview

`mlbscore.py` is a classic TV-style MLB scoreboard implemented in Python using Tkinter. It is designed to display live, final, and upcoming game information for a specified MLB team with a minimalist, dynamic UI.

**This version (v5) features improved thread management, a fully working in-game status logic, and refined visuals.**

---

## Key Features

* **Clean Inning Highlight:** The active inning header and score cells are subtly highlighted using a blend of the team's accent color.
* **Real-time B/S/O Logic:** Balls, Strikes, and Outs accurately update. Bases and counts immediately reset once a third out is detected, avoiding visual lag.
* **Runner Animation:** Bases fill and runners move visually between bases upon significant in-game events.
* **Robust Threading:** Network calls are managed using a `ThreadPoolExecutor` to keep the UI responsive, preventing the "Not Responding" state during network lag.
* **Dynamic Polling:** Automatically adjusts the polling frequency based on game status:
    * **Live Game:** 15s
    * **Scheduled Game:** 300s
    * **No Game:** 3600s
* **Customizable UI:** All colors, fonts, and dimensions are configured via `config.json`.
* **Visual Elements:**
    * Inning-by-inning scoreboard with R/H/E totals.
    * Diamond visualization with B/S/O dots.
    * Current batter and pitcher names.
    * Batter icon (⚾) marking the team currently at bat.

---

## Installation

1.  **Ensure Python is installed.** Python 3.10 or newer is recommended.
2.  **Install dependencies:** This project requires the `requests` library.

    ```bash
    pip install requests
    ```
3.  **Download** `mlbscore.py` and `config.json` to your local machine.

---

## Configuration (`config.json`)

The primary configuration file allows you to customize the team being followed, colors, and polling intervals.
Note: It is *NOT* recommended to poll less than 15 seconds, this may result
in your IP being banned, as the MLB API is undocumented and intended for
private use.

**Example `config.json`:**

```json
{
  "team_id": 119,
  "teams": {
    "Los Angeles Dodgers": 119,
    "New York Yankees": 147 
  },
  "polling_intervals": {
    "live": 15,
    "scheduled": 300,
    "none": 3600
  },
  "lookahead_days": 7,
  "canvas": {
    "width": 1000,
    "height": 500,
    "bg_color": "#0b162a",
    "fg_color": "#eaeaea",
    "accent": "#FFD700",
    "font_family": "Courier"
  },
  "ui": {
    "max_innings": 9
  },
  "debug": false
}
