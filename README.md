# MLB Canvas Scoreboard (`mlbscore.py`)

A robust and visually appealing Python application designed to display real-time and scheduled Major League Baseball (MLB) scores, game status, and on-base runners for a user-specified team. Built using the **Tkinter** library for its graphical interface and powered by data retrieved directly from the official MLB Stats API, this script is engineered for stability, visual clarity, and thread-safe operation.

---

## ⚾ Core Functionality and Architecture

This application operates as a single-window GUI that continuously monitors the state of your followed MLB team. It uses a **multi-threaded approach** to ensure that network latency never compromises the smoothness or responsiveness of the graphical display.

### 1. Concurrency and Thread Safety
* **`ThreadPoolExecutor`**: Network operations (`fetch_schedule`, `fetch_live_feed`) are executed in a background thread managed by `concurrent.futures.ThreadPoolExecutor`. This prevents the Tkinter main loop (the GUI thread) from freezing while waiting for API responses.
* **GUI Scheduling**: All subsequent updates to the `tkinter.Canvas` (rendering scores, moving runners, updating BSO counts) are carefully scheduled back onto the main thread using `self.root.after(0, ...)` calls, which is the standard, safest practice for Tkinter development.

### 2. Smart Polling Logic

The script implements a sophisticated polling mechanism to be efficient and save on API calls while ensuring you get timely updates:

| Game State | Poll Interval | Logic |
| :--- | :--- | :--- |
| **LIVE** | **15 seconds** (`polling_intervals["live"]`) | Used when the game is actively **In Progress**. |
| **SCHEDULED** (Far) | **Time to 1-Hour Mark** | Used when the next game is **more than one hour away**. The script calculates the exact number of seconds until the game is precisely 60 minutes from starting. |
| **SCHEDULED** (Near) | **300 seconds** (`polling_intervals["scheduled"]`) | Used for the **final hour countdown** until the game starts, ensuring the footer countdown is always ticking. |
| **NONE** | **3600 seconds** (`polling_intervals["none"]`) | Used when no game is scheduled in the lookahead window, or the last game is Final. |

---

## ✨ Advanced Features

### Dynamic Game State and Pitching Counts

The scoreboard is designed to be highly reactive and visually accurate:

* **Precise Out Logic**: The script implements a crucial, single-trigger logic for the 3rd out. When `raw_outs` from the API reaches 3, the script immediately calls `reset_after_third_out()`, clearing all bases, balls, strikes, and outs **before** the linescore officially updates to the next half-inning. This prevents the brief, erroneous display of a 3rd out count or runners on base after an inning ends.
* **Visual Outs Limit**: The GUI explicitly limits the display to a maximum of **two out circles**, aligning with standard baseball scorekeeping visuals.
* **Batter/Pitcher Display**: The currently active batter and pitcher are fetched and displayed below the BSO counts, providing crucial context for the active play.

### Base Runner Animation and Fades

Runners are not merely static icons; their movement is animated and their occupancy is highlighted:

* **`move_runner_base` Function**: Handles the smooth, step-by-step translation of a runner circle from a starting base (`1B`, `2B`, `3B`) to an ending base (including `Home` for scoring).
* **Base Occupancy Fade**: When a runner successfully occupies a base (e.g., batter reaches 1st), the base icon itself executes a temporary color-blend animation, fading from a neutral color to the batting team's color.
* **Clearing Runners**: Bases and runners are cleared reliably when a score is made, a runner is called out, or the inning concludes.

---

## 🛠️ Installation and Dependencies

The application requires Python 3 and a few standard third-party libraries for networking.

### Prerequisites

1.  **Python 3**: Ensure you have Python 3 installed and accessible on your system.
2.  **Dependencies**: Install the required packages using `pip`:

    ```bash
    pip install requests urllib3
    ```

### Running the Script

Execute the script directly from your terminal:

```bash
python3 mlbscore.py
