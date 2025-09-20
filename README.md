Canvas-based Astros scoreboard with MLB live API + LIFX effects.

- Canvas retro TV-style scoreboard (all caps team names)
- Bases diamond (simple filled indicators when occupied)
- Outs dots, At-bat, Current batter
- Status line with countdown (next update in Ns)
- Dynamic polling: intervals change with game state
- Countdown driven by Tkinter .after loop; API fetch is done in a background thread,
  and countdown resets only after the fetch thread finishes (keeps UI responsive)
- Debug logging to console
- LIFX effects: opponent score -> red single flash; end-of-game -> rainbow if ASTROS win,
  shades of red if opponent wins. All effects save/restore previous light state.
