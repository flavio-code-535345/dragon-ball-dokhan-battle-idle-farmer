# Templates

A template is a small crop of a screenshot (a button, the title of a dialog) that the farmer looks
for on every screenshot. Templates live in the data volume (`/data/templates`: one PNG each plus
`templates.json` with their settings). The project ships none: you capture them from your own game,
in your language and at your device's resolution.

## Capturing

1. Bring the game to the screen that shows the element (use the **Screen** tab with *Touch
   control* if the device is not in your hand).
2. **Templates** tab → press **Capture** next to the name in the checklist (or **+ Capture** for a
   new one). This freezes the current screenshot.
3. Drag a box around the element, check the name, and press **Save template**.
   Optional: **⌖ Set tap point**, then tap the picture where a routine should tap when it taps this
   template, inside or outside the box. Example: the `new_mark` label of a stage, with the tap point
   on the stage's row. The point moves with the template wherever it is found; the Screen tab's
   boxes show it as a red crosshair. *Reset tap point* (Edit) goes back to the middle of the box.
4. The result shows the match score on that screenshot and warns you if the crop also matches
   somewhere else.

**Test on screen** scores every template against the current screen. The Screen tab draws boxes
around everything it recognises.

## What makes a good template

- **Include the text** of a button, not only its coloured background. A plain colour matches
  everywhere, and the farmer refuses almost-flat crops.
- **Avoid parts that change**: numbers (ACT, stones, counters), timers, sparkles, character art.
- **Dialogs: one template for the title, one for the button.** For example `friend_request` is the
  title of the friend-request popup and `cancel_button` is a generic Cancel button. The routine says
  "when `friend_request` is visible, tap `cancel_button`".
- **Search the whole screen** for generic buttons that appear in different places (OK, Cancel).
  Leave it off for buttons that are always in the same place: that is faster and avoids false
  matches. (By default the search area is the captured spot plus a margin.)
- **Compare colours** when two states differ mainly by colour, such as Auto ON vs Auto OFF.
  Normally matching uses greyscale, which is more robust.
- **Threshold**: 0.85 by default. Raise it (0.90+) if a template matches the wrong thing, lower it
  (0.80) if it misses a button that is clearly visible. Check with *Test on screen*.

Things the farmer handles for you:

- **Dimmed copies are ignored.** When a popup darkens the screen behind it, buttons under the popup
  still look similar, so matches much darker than the template are rejected.
- **Different resolutions.** Templates are rescaled when the screenshot width differs from the
  one they were captured at. Recapture after changing the resolution, language or a big game
  update anyway.
- **Taps land inside the button** (or around its tap point), with a small random offset
  (`TAP_JITTER`) instead of always the exact spot.

## Templates of the default routine

| Name | Level | Capture it on |
|---|---|---|
| `attempt_again` | required | the results screen after a clear: the **Attempt Again** button |
| `start_button` | required | the team screen before a quest: the **Start!** button |
| `ok_button` | required | any dialog with an **OK** button (e.g. right after Attempt Again). Tick *whole screen*. |
| `cancel_button` | recommended | any **Cancel** / **No** button. Tick *whole screen*. |
| `act_empty_dialog` | recommended | the **title** of the "not enough ACT" dialog. Without it the farmer can't tell that ACT ran out. |
| `friend_request` | recommended | the **title** of the "send a friend request?" popup |
| `auto_off` | recommended | the **Auto** button while Auto is **off** (tick *compare colours*) |
| `error_retry` | recommended | the **Retry** button of a connection error |
| `defeat_give_up` | recommended | after a defeat, the **Give up / No** button of the revive offer |
| `act_use_item` | optional | the "use ACT item" button of the not-enough-ACT dialog |
| `act_use_stone` | optional | the "restore with Dragon Stone" button of that dialog |
| `title_screen` | optional | something unique on the title screen |
| `resume_yes` | optional | **Yes** on "resume the quest?" after a restart |
| `close_button` | optional | **Close / X** of news and login-bonus popups |
| `nav_1` … `nav_4` | optional | the way back to your stage after a restart: e.g. Events → event banner → stage → difficulty |
| `select_support` | optional | the first entry of the support list, if the game asks for a support |

The optional ones only matter for recovering from crashes. Rules whose templates are missing are
skipped (the Farm tab lists them), so you can add them gradually.

## Backup and sharing

**Export** in the Templates tab downloads every template as one `.json` file (settings plus the PNG
as base64); **Import** adds or replaces the templates of such a file, all or none. An entry without
`region` is searched near its `bbox` like a fresh capture, `"region": null` searches the whole screen.
**📷 Screenshot** saves the current screen as a full-size PNG: handy for cropping templates elsewhere
(or letting someone else do it) and importing the result.

Copying `/data/templates` works too (`docker cp dokkan-farmer:/data/templates .`). Templates work
across installs that use the same device resolution and game language.
