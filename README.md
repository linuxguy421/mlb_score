# MLB Canvas Scoreboard (mlbscore.py)

A Python application using **Tkinter** to display a real-time or scheduled MLB scoreboard for a followed team.

---

## Features

This script implements the "final v5" features, ensuring a stable and streamlined scoreboard:

* **Fixed Issues:** Addresses syntax and logic errors from previous versions.
* **Thread Safety:** Improved network calls using `ThreadPoolExecutor` to prevent GUI lockup.
* **Real-time Updates:** Ensures all GUI updates are correctly scheduled on the main Tkinter thread.
* **Base/Runner Logic:** Streamlined base occupancy and runner movement logic.
* **Configuration:** Uses `config.json` for persistent settings (team ID, colors, polling intervals).
* **Visuals:**
    * Only displays a maximum of **two out circles**.
    * Immediate reset of bases, balls, strikes, and outs upon detecting a **3rd out**.
    * Includes all base/runner visuals, animations, grid overlay, and the at-bat '⚾' icon.
* **Polling:** Automatically adjusts the network polling interval based on game state (Live, Scheduled, None).

---

## Installation & Setup

1.  **Dependencies:** Ensure you have Python 3 and the required libraries:
    ```bash
    pip install requests urllib3
    ```

2.  **Configuration:** Create a **`config.json`** file in the same directory. This file dictates the team being followed, colors, and update frequency.

    A minimal example:
    ```json
    {
        "team_id": 117,
        "teams": {
            "Cubs": 161,
            "Phillies": 143,
            "Followed Team": 117
        },
        "team_colors": {
            "Followed Team": {"primary": "#002D56", "accent": "#C41E3A"}
        },
        "ui": {"max_innings": 9},
        "debug": false
    }
    ```
    *Note: The `team_id` is the default, but you can override it using `--team` on the command line.*

3.  **Run:** Execute the script from your terminal:
    ```bash
    python3 mlbscore.py
    ```
    You can specify a team or enable debug mode:
    ```bash
    python3 mlbscore.py --team "Cubs" --debug
    ```

---

## Status Indicators

* **🔴 LIVE:** A red circle and "LIVE" text appears in the footer when a game is in progress.
* **Footer:** Displays the next scheduled game and the time until the next data poll.
* **BSO:** Balls, Strikes, and Outs are tracked to the right of the diamond.
