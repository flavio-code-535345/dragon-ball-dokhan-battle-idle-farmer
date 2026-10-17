# Routines

A routine is a YAML file in `/data/routines` (edit it in the **Routine** tab of the web UI). Two
complete, commented routines are bundled:

- [`dokkan_auto_farm.yaml`](../src/dokkan_farmer/routines/dokkan_auto_farm.yaml): repeats a stage
  with Dokkan's Auto Mode (the default),
- [`dokkan_clear_new_stages.yaml`](../src/dokkan_farmer/routines/dokkan_clear_new_stages.yaml):
  plays the stages you have not cleared yet, for their Dragon Stones (see
  [below](#clearing-new-stages-for-dragon-stones)).

Bundled routines are copied into the volume once. Your edits are never overwritten, and *Restore
default* brings back the original. Templates are shared: a button captured for one routine works in
all of them.

## How the engine runs a routine

Every `tick_interval` seconds the engine:

1. takes a screenshot,
2. updates the **counters** (e.g. counts a finished run when the results screen appears),
3. checks the stop conditions (`stop_after_runs`, `stop_after_minutes`),
4. every `foreground_check_seconds` checks that the game is in front, and relaunches it if not,
5. fires the **first rule** (top to bottom) whose `when` conditions all hold and whose `cooldown`
   has passed, runs its `do` actions and waits `action_delay` (or the rule's `delay`),
6. checks whether it is **stuck** (see below).

So **order matters**: put specific, important rules (errors, ACT empty, defeats) first and generic
ones (an OK button, "tap to continue") last.

## Structure

```yaml
name: My routine                 # shown in the UI
description: What it does
version: 1

options:                         # shown as a form on the Farm tab
  max_runs:
    label: Stop after N runs (0 = never)
    type: number                 # number | choice | bool | text
    default: 0
    min: 0
  policy:
    type: choice
    choices: [wait, stop]
    default: wait
    help: Shown under the field

settings:
  tick_interval: 1.0             # seconds between screenshots
  action_delay: 1.5              # pause after a rule fired
  stop_after_runs: $max_runs     # 0 = never; "$name" refers to an option
  stop_after_minutes: 0
  run_counter: runs              # which counter counts as "runs" (stats, stop, notifications)
  ensure_foreground: true        # relaunch the game if another app is in front
  foreground_check_seconds: 30
  stuck:
    static_seconds: 180          # screen unchanged this long -> recovery
    idle_seconds: 1500           # no rule fired this long -> recovery (> your longest battle!)
    recovery:                    # actions of one recovery attempt
      - key: BACK
      - wait: 3
    restart_after: 2             # after this many recoveries without a finished run: restart the game
    max_restarts: 3              # after this many restarts without a finished run: stop with an error

counters:
  runs:
    label: Runs finished
    appears: attempt_again       # +1 when this template appears...
    rearm: start_button          # ...but only once per appearance of this one

templates:                       # documentation for the checklist in the Templates tab
  attempt_again:
    level: required              # required | recommended | optional
    description: The Attempt Again button on the results screen.
  start_button: The Start! button  # short form = description only

rules:
  - name: Attempt again
    when: {visible: attempt_again}
    do: [{tap: attempt_again}]
    cooldown: 8                  # seconds before this rule may fire again
    # delay: 3                   # override action_delay for this rule
    # enabled: false             # keep the rule but skip it
```

Rules that use a template you have not captured are skipped (they are listed on the Farm tab).
If a template marked `required` is missing, the farm does not start.

## Conditions (`when`)

All listed conditions must hold.

| Condition | Meaning |
|---|---|
| `visible: name` or `[a, b]` | all of these templates are on screen |
| `any_visible: [a, b]` | at least one of them is on screen |
| `not_visible: name` or `[a, b]` | none of them is on screen |
| `static_for: 6` | the screen has not changed for 6 seconds |
| `idle_for: 60` | no rule fired for 60 seconds |
| `option: {policy: wait}` | the option currently has this value |
| `counter_below: {stone_refills: $max_stone_refills}` | counter < value |
| `counter_at_least: {runs: 10}` | counter ≥ value |
| `always: true` | always (use with a cooldown) |

## Actions (`do`)

| Action | Meaning |
|---|---|
| `tap: name` | tap the template where it was found: its middle, or the tap point set when capturing it (random offset, see `TAP_JITTER`) |
| `tap: {template: name, offset: [0.0, 0.8]}` | tap relative to the template: offset in template widths/heights from its centre (instead of its tap point) |
| `tap_xy: [0.5, 0.15]` | tap a point: values ≤ 1 are fractions of the screen, larger values are pixels |
| `swipe: {from: [0.5, 0.8], to: [0.5, 0.3], ms: 400}` | swipe (also `[x1, y1, x2, y2, ms]`) |
| `key: BACK` | press a key: `BACK`, `HOME`, `ENTER`, `ESCAPE`, `MENU`, `APP_SWITCH`, `WAKEUP`, `POWER` |
| `wait: 2.5` | wait seconds |
| `wait_minutes: $act_wait_minutes` | wait minutes (shown as a countdown in the UI); with power saving (`POWER_SAVE`) Android's screen is off meanwhile (2 minutes or more), the game paused. Use `wait` for waits in which the game must keep playing |
| `count: stone_refills` | add 1 to a counter (`{counter: x, by: 2}` to add more) |
| `reset: stone_refills` | set a counter to 0 |
| `notify: "Out of ACT after {runs} runs"` | send a notification (with a screenshot) |
| `log: "text"` | write to the log |
| `stop: "reason"` | stop farming |
| `launch_app` / `stop_app` / `restart_app` | start, force-stop or restart the game |
| `screenshot: label` | save a debug screenshot in `/data/screenshots` |

Any number value can be an option reference like `$max_runs`. Texts of `notify`, `log` and `stop`
can use `{placeholders}`: option names, counter names, `{runs}`, `{elapsed}` and `{routine}`.

## Clearing new stages for Dragon Stones

Each stage gives Dragon Stones the first time you clear a difficulty. **Dokkan Clear New Stages**
collects them: it opens the first stage with a "NEW" mark in the stage list, picks **Normal**, plays
it, and continues with the next one. When the list has no NEW mark left (it swipes down and up to
look), it stops and tells you to open the next area. A NEW stage was never cleared, so its Normal
always has a Dragon Stone. The harder difficulties' stones stay for you: such a stage shows
*CLEARED!* with a clear reward of e.g. *1/2*.

Dokkan's Auto Mode only works on stages you already cleared, so this routine plays them itself, the
simple way: on the map it taps where you move, in battle it keeps tapping one spot of the ki sphere
board. That is no battle strategy: it suits easy stages (Normal and Hard Quest stages of the early
areas) with a strong team. After a defeat it taps "Give up" (never a paid revive), tries once more
and then stops (option *Tries again after a defeat*).

To set it up:

1. In Dokkan, open the stage list of one Quest area.
2. In the **Templates** tab capture the six required templates: `new_mark` (the "NEW" mark of a
   stage, tick *search the whole screen*), `stage_list` (something only on stage lists),
   `difficulty_normal` (the NORMAL bar of a stage's difficulty screen), `start_button`,
   `battle_screen` (e.g. the BATTLE DETAILS button of a battle) and `stage_result` (e.g. "Obtained
   Item" on the results screen at the end of a stage). Capture the others when you meet those
   screens: `stage_clear` (the "Battle Results" title after each battle), `map_screen`,
   `ok_button`, `close_button` (e.g. the "Dragon Stone(s) acquired" popup), `cancel_button`,
   `act_empty_dialog`, `defeat_give_up`, ... The checklist on the Templates tab lists them all.
   With the game's *Scenario OFF* there are no story scenes to skip.
3. On the **Farm** tab pick *Dokkan Clear New Stages*. Check the tap points: *Battle: where to tap a
   ki sphere* (a spot in the middle of the sphere board) and *Map: where to tap to move*. With
   *Touch control* on, the Screen tab shows the coordinates of the spot you tapped.
4. Press Start and watch the first stage.

## Playing the Dragon Ball Story

**Dokkan Story** plays the Dragon Ball Story (home screen → START → Dragon Ball Story) saga by saga
for the first-clear Dragon Stones. A series (Dragon Ball, Z, GT, Movies) is a wheel of sagas; a
saga's stage list shows its cleared stages and the next one with a NEW mark. The routine plays that
stage on Normal the way Dokkan Clear New Stages does, and when a saga has no NEW stage left it goes
back to the wheel, turns it one saga further (a short swipe down) and opens that saga. When several
sagas in a row had nothing to play (option *Stop after this many sagas*), the series is done.

Start it on the saga wheel turned to the saga to begin with (the first saga is at the bottom), or
in that saga's stage list. Besides the templates of Dokkan Clear New Stages it needs `saga_wheel`
(the "TAP" marker above the selected saga), `back_button` (the "<<<" of the stage list) and
`stage_result` (e.g. "Obtained Item" on the results screen at the end of a stage).

## Collecting mission rewards and presents

**Dokkan Collect Rewards** claims what is waiting and stops: start it on the home screen. It opens
Missions and presses "Claim all" on the open tab and on each `mission_tab_1` ... `mission_tab_4`
you captured (in that order), goes back, opens the Present Box (where mission rewards land),
presses "Accept all" until nothing is left, goes back and stops. A screen is left only after its
button was missing for three looks in a row. Capture `claim_all` and `accept_all` while they can be
pressed (not greyed out), `ok_button` with *search the whole screen* for the confirm and reward
dialogs, and `back_button` (else Android's Back key is sent).

## The safety net

On top of your rules the engine:

- **Detects stuck screens.** If the screen does not change for `static_seconds`, or no rule fires
  for `idle_seconds`, it runs the `recovery` actions. After `restart_after` recoveries without a
  finished run it restarts the game. After `max_restarts` restarts without a finished run it gives
  up, stops and notifies you (with a screenshot). Screenshots of stuck screens are saved.
- **Pauses rules that have no effect.** A rule that fires 3 times in a row while the screen stays
  identical is skipped for 2 minutes, so lower rules (fallbacks) get a chance. Example: the "use ACT
  item" button when you have no items left.
- **Keeps the game in front.** If another app is in front twice in a row, it relaunches the game.
- **Wakes the screen.** After a few black screenshots it sends `WAKEUP`.
- **Reconnects.** When the device is unreachable it retries with back-off (5 s up to 60 s) and
  notifies you after the third failure.
- **Respects active hours** (`ACTIVE_HOURS`): outside the windows it waits without touching the device.

## Example: a minimal routine

```yaml
name: Just attempt again
rules:
  - name: Attempt again
    when: {visible: attempt_again}
    do: [{tap: attempt_again}]
    cooldown: 8
  - name: OK
    when: {visible: ok_button, not_visible: attempt_again}
    do: [{tap: ok_button}]
  - name: Start
    when: {visible: start_button}
    do: [{tap: start_button}]
  - name: Tap to continue
    when: {static_for: 6}
    do: [{tap_xy: [0.5, 0.15]}]
    cooldown: 4
counters:
  runs: {appears: attempt_again, rearm: start_button}
```

## Example: alternate between two stages

Counters and options let you build simple state machines, e.g. 10 runs of one stage, then
navigate elsewhere:

```yaml
rules:
  - name: Switch stage after 10 runs
    when: {visible: attempt_again, counter_at_least: {runs_here: 10}}
    do:
      - tap: ok_button          # leave the results screen instead of Attempt Again
      - reset: runs_here
  - name: Attempt again
    when: {visible: attempt_again}
    do: [{tap: attempt_again}, {count: runs_here}]
    cooldown: 8
  # ... navigation rules with your own templates ...
```
