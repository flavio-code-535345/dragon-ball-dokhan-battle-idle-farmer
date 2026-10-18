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
| `color_visible: path_arrow` | a blob of this colour (from `colors`, below) is on screen |
| `color_absent: path_arrow` | no blob of this colour is on screen |
| `always: true` | always (use with a cooldown) |

## Actions (`do`)

| Action | Meaning |
|---|---|
| `tap: name` | tap the template where it was found: its middle, or the tap point set when capturing it (random offset, see `TAP_JITTER`) |
| `tap: {template: name, offset: [0.0, 0.8]}` | tap relative to the template: offset in template widths/heights from its centre (instead of its tap point) |
| `tap_color: path_arrow` | tap the middle of the biggest blob of this colour (from `colors`) |
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

## Colours (`colors`)

Some things have no fixed look but a fixed colour, such as the map's blue path arrows, which point
any of eight ways. A routine can declare colours and use them with `color_visible` and `tap_color`:

```yaml
colors:
  path_arrow:
    description: The bright blue arrows where the path on the map forks.
    hsv_min: [95, 200, 150]     # hue 0-179, saturation and value 0-255 (OpenCV's scales)
    hsv_max: [115, 255, 255]
    min_area: 0.0015            # a blob must cover at least this much of the screen...
    max_area: 0.02              # ...and at most this much (skips small dots and big areas)
    region: [0, 0.05, 1, 0.68]  # optional: where to look, [x, y, w, h] fractions
```

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
   screens: `stage_clear` (the "Battle Results" title after each battle), `map_screen` (the top
   edge of the middle orange move card, in colour mode; not the MENU button, which battles have too),
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

Quest: Dokkan Story (START → Quest) works too: a chapter is a list of areas, and an area with
uncleared stages has a NEW mark. When an area has no NEW stage left, the routine goes back to the
area list and opens the next NEW area.

Both story routines have an option **Difficulty to play**. Clearing a stage's highest difficulty
for the first time also pays the first-clear Dragon Stones of the lower ones (checked in the game:
a Quest stage went from 0/3 to 3/3 with SUPER alone). So by default (*highest*) they play SUPER,
else Z-HARD, else HARD, else NORMAL (templates `difficulty_super`, `difficulty_zhard`,
`difficulty_hard`, `difficulty_normal`): all stones of a stage in one clear, for more STA (about
27 instead of 17) and harder battles. *easiest* plays only the lowest listed difficulty. Quest
stages have no NORMAL: their lowest is HARD. Because a Quest area's stage list
has no MISSION button, Dokkan Story presses Back when no NEW mark is left, and stops after four
Backs in a row without a stage, a battle or a map move in between.

With **Where to start = quest** the routine finds its own way from the home screen: START, then
"Quest Dokkan Story", then the chapter wheel, which it turns *Quest: wheel turns before opening
the chapter* times (short swipes towards later chapters; 0 = the selected chapter, one turn from
Chapter 6 gives Chapter 7) before opening the chapter. That also brings the farm back after the game
was restarted. It needs the templates `home_screen` (the START button, as in Dokkan Collect
Rewards), `menu_quest` ("Quest Dokkan Story" in the menu) and `chapter_wheel` (the "TAP" marker above
the selected chapter of the wheel). With *here* (the default) it starts from the list you opened.

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

## Extreme Z-Battles

**Dokkan Extreme Z-Battle** plays an Extreme Z-Battle event level after level (Awakening Medals, a
Dragon Stone for the first clear of a level): start it on the event's stage page. It presses Fight
(`xb_fight`, tap point in the middle of the round button), Start! on the team page (`xb_start`, the
team of the previous clear is used as it is), taps a ki sphere while `xb_battle` is on screen (e.g.
the ITEM button), clears the results (`xb_congrats`, then `ok_button` for the OK and friend-request
popups) and the game moves on to the next level. These battles cost no stamina and allow no
continues, so a lost battle spends nothing; after `tries` battles in a row that did not reach the
results the routine stops with "Use a stronger team for this level". `max_battles` stops after that
many won battles (0 = no limit).

With **Events to play = all** it needs no stage page to start on: start it anywhere. It goes START
→ Event (`home_screen`, `menu_event`), opens the Z-Battle tab (`xb_tabs`: crop the "Challenge" tab
of the tab row, which is grey unless selected), opens the first event whose banner says
"NEXT >> Lv. 1" (`xb_lv1`: tick *search the whole screen*, leave a little empty space after the 1 so
"Lv. 11" does not match, and give it a tap point on the banner picture), plays it until the team
loses, goes back with `back_button` and takes the next fresh event. Events played before show a
higher level and are skipped; the list is scanned from the top, then down. It stops with "No
Extreme Z-Battle event with a fresh level 1 is left", or when the very first level of an event is
lost `tries` times.

Battles are sped up with the game's fast-forward button (the green double arrow at the right while
animations play): capture it as `xb_skip` (*search the whole screen*) and the routine taps it.

**Team order.** With the option *Team order* = `by_type` the routine swaps the team on the planning
screen (the orange swap arrows between the slots show only then: capture them as `xb_planning`).
Against a PHY enemy (`enemy_phy`, the type badge at the top left of the battle) the STR unit goes to
the front slot and an INT unit is moved out of it. A unit's type is the colour of its ring, read by
the routine's `colors` (hue-wrapping red, a short piece of each slot's ring, `color_absent` for "no
STR ring in slot 1"); a unit is moved by swiping it onto the slot next to it, at most twice a turn.
Only PHY enemies are covered: for another type capture its badge as e.g. `enemy_agl` and copy the
three `Order` rules with that type's winning and losing ring colours (the wheel is AGL > STR > PHY >
INT > TEQ > AGL). Whether the swap really lowers the damage taken is not measured.

## Repeating an event stage

**Dokkan Repeat Stage** clears one event stage again and again from its details page (the page
with the difficulty bar and the BOOST and MISSION buttons), for drops like Skill Orbs and for the
event's mission rewards (Dragon Stones, Skill Orb Tickets, Aged Meat). Open the stage, select the
routine, set `max_clears` and press Start. It starts the stage, plays its map and battles like
*Clear New Stages*, and afterwards opens MISSION and accepts what is ready.

- **BOOST** (option): the routine taps BOOST ON first (templates `boost_off` and `boost_on`;
  capture them in colour mode, since the two look alike in grey tones). A boost charge is used per
  run and refills slowly; when none is left the stage is played without it. A boosted clear costs
  and pays double but counts as one clear for the missions, so unboosted runs are cheaper for those.
- **Stamina**: when the stage cannot start, the refill dialog opens. The routine only ever taps the
  "Restore STA with Item" row (Aged Meat, one piece unless `meat_pieces` says two), at most
  `max_refills` times, and never the Dragon Stone row. With `refill` = stop it stops instead.
- Capture `stage_details` (the "Clear Reward / Featured Reward" headings), `restore_sta`,
  `restore_with_item`, `event_mission`, `mission_dialog` and `mission_accept_all` (colour mode) once; the rest are shared
  with the other routines.

The in-game **Dragon Stone Navigator** (the orange stone button at the bottom of the event
screens) lists the events that still pay stones, with the stones for stage clears and for
missions: a good list to pick the next event from.

## Farming Dragon Stones

Dragon Stones cannot be farmed forever: every source pays once. Stamina is only needed for the
first, and it is refilled by rank-ups, so never press a Dragon Stone refill button (the routines
do not).

| Source | Routine | What it pays |
| --- | --- | --- |
| Quest Dokkan Story, first clear | Dokkan Story, then Dokkan Clear New Stages | about 3 stones per stage on the hardest difficulty (clearing it also pays the easier ones at once); an area you have visited no longer shows a NEW mark, but can still have new stages |
| Extreme Z-Battle, level clear bonuses | Dokkan Extreme Z-Battle | about 1 stone at certain levels; no stamina, no continues, a lost battle costs nothing |
| Missions, presents, events | Dokkan Collect Rewards | what is waiting in the Missions screen and the Present Box |

How far an Extreme Z-Battle goes depends on the team: each event has its own limit (one level per
clear; a team of the usual rarity stops around Lv. 5 to 25 depending on the event), and the
routine stops there by itself. A stronger team of the event's type moves that limit. Run the
routines one after the other (Collect Rewards, Story, Extreme Z-Battle with *all* events):
start each one on the home screen.

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
