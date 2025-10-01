# MLB Canvas Scoreboard

## Overview

`mlbscore.py` is a classic TV-style MLB scoreboard implemented in Python using Tkinter. It displays live, last, and upcoming game information for a specified MLB team, including:

* Inning-by-inning scoreboard with R/H/E
* Diamond with balls, strikes, and outs visualization
* Current batter and pitcher stats (placeholder display if no live game)
* Countdown to next update
* Dynamic polling intervals depending on game status
* Team row colorization based on official team colors

## Features

* Reads configuration from `config.json`
* Supports specifying a team via CLI (`--team`) or config
* Automatically adjusts polling frequency:

  * Live game: 15s
  * Scheduled game: 300s
  * No game: 3600s
* Canvas-based UI with dynamic sizing
* Visual enhancements:

  * Team-specific row colors
  * Diamond and B/S/O visualization
  * Footer showing next update and upcoming game info

## Installation

1. Ensure Python 3.13+ is installed.
2. Install dependencies:

   ```bash
   pip install requests
   ```
3. Download `mlbscore.py` and `config.json` (optional).

## Usage

```bash
python mlbscore.py --config config.json --team "New York Yankees" --debug
```

### CLI Options

* `--config`: Path to `config.json` (default `config.json`)
* `--team`: Team name to override `team_id` in config
* `--debug`: Enable debug logging

## Configuration (`config.json`)

Example:

```json
{
  "team_id": 147,
  "teams": {
    "New York Yankees": 147
  },
  "polling_intervals": {"live": 15, "scheduled": 300, "none": 3600},
  "lookahead_days": 7
}
```

* `team_id`: MLB team ID
* `teams`: Mapping of team names to IDs
* `polling_intervals`: Polling frequency in seconds
* `lookahead_days`: Number of days ahead to fetch schedule

## Future Improvements / Suggestions

1. **Live Game Enhancements**

   * Show pitch count for pitcher
   * Display current batter stats (avg, OBP, HR, RBI)
   * Show base runner names or positions

2. **Visual Enhancements**

   * Animations for B/S/O changes
   * Highlight scoring plays
   * Improved diamond graphics with colored bases

3. **Additional Stats / Info**

   * Team records, streaks, standings
   * Game duration / start time countdown
   * Weather or stadium info

4. **UX Improvements**

   * Resizeable canvas with responsive layout
   * Keyboard shortcuts to switch teams
   * Save and restore last viewed team

5. **Backend Improvements**

   * Caching API results to reduce network calls
   * Better error handling for network failures
   * Support multiple teams in one view

---
