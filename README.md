# MLB LIFX Score Notifier

This Python script watches MLB live scores using the **MLB StatsAPI** and blinks your **LIFX smart lights** whenever your favorite team scores a run.  

It supports team filtering, custom blink colors, adaptive polling, and JSON debugging output.  

---

## Features
- 🏟 Track any MLB team’s game in real-time  
- 💡 Blink your LIFX lights when your team scores  
- 🎨 Choose custom colors (orange, red, blue, green, purple, white)  
- ⏱ Adaptive polling:  
  - Every **15s** when a game is in progress  
  - Every **5 min** before a game starts  
  - Every **1h** after the game ends  
- 🔍 Option to dump full MLB API JSON feed for debugging  
- 📋 List all MLB teams and IDs  

---

## Requirements
- Python 3.8+
- [lifxlan](https://pypi.org/project/lifxlan/) (for controlling lights)
- `requests` library

Install dependencies:
```bash
pip install lifxlan requests
```

Make sure your computer is on the same local network as your LIFX bulbs.  

---

## Usage

### Watch a Team
```bash
python mlb_watch.py --team "Astros"
```
Whenever the Astros score, your LIFX bulbs will blink **orange**.

### Change Blink Color
```bash
python mlb_watch.py --team "Yankees" --color blue
```

### List All MLB Teams
```bash
python mlb_watch.py --list-teams
```

Example output:
```
 110 | Baltimore Orioles (BAL)
 111 | Boston Red Sox (BOS)
 117 | Houston Astros (HOU)
 ...
```

### Dump Live Game Feed
```bash
python mlb_watch.py --team "Astros" --dump
```
This prints the raw JSON data from MLB’s live feed for debugging.  

---

## Example Output
```text
=== Pretty Scoreboard ===
Houston Astros (3) vs New York Yankees (2) | Game status: In Progress

🎉 Astros scored! 2 → 3
```

LIFX bulbs will blink after the score update.  

---

## Example Screenshot / GIF

### Terminal Output
![Terminal Example](docs/example_terminal.png)

### Lights Blinking
![Lights Blinking Example](docs/example_lights.gif)

(You can create your own by running with `--team "Astros"` during a live game.)

---

## Notes
- The script clears the terminal and shows a live countdown until the next API poll.  
- The MLB StatsAPI does not enforce strict rate limits, but excessive polling is discouraged.  
- The default blink duration is 3 times at 0.5s intervals.  

---

## Roadmap
- Support **hex color codes** for custom blink colors  
- Multiple team tracking at once  
- Slack/Discord/webhook notifications  

---

## License
MIT License
