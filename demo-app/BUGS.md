# Planted bugs in Tiny Tasks

Tiny Tasks has twelve planted bugs. Each one is a usability or accessibility problem that a person on a particular device would meet. Footfall's phase 1 is done when its testers find most of them. The default brief does not mention any of them.

Personas are named by their ids in `footfall/data/personas.toml`. The manifest runs `sven`, `grace`, `malik`, `clara`, `layla` and `anna` by default. Any other persona is marked "when run", meaning it can catch the bug if someone adds it to `test.personas` or names it for a run. "The accessibility check" means the accessibility warnings that the tester's `look` tool is meant to give for each screen, such as controls with no name and targets that are too small. "The scripted checks" are phase 2's, which load `test.pages` and read the browser's console.

## Bug 1: every sign-in error says "Something went wrong"

A wrong password, an unknown username and two empty fields all give the same message, with no hint about which field to fix. It lives in `Handler._login` in `serve.py`, with the text in `templates/error.html`. Anyone who mistypes should catch it. The brief tells testers that only the given username and password work, so a tester can reach the error by mistyping on purpose, as `jonas` and `mehmet` (both when run) tend to.

## Bug 2: search is case-sensitive

Searching for `milk` finds "Buy oat milk" and searching for `Milk` finds nothing. It lives in `Handler._index`, which matches with SQLite's `instr()`, an exact comparison. Personas who search should catch it. `tests/test_demo_app.py` pins this bug.

## Bug 3: empty tasks can be added

Pressing "Add task" with nothing typed adds a blank row. It lives in `Handler._add`, which does not check the text. `malik` taps fast and skips fields, and `mehmet` (when run) tries odd input, so both should catch it. Only `malik` is run by default. `tests/test_demo_app.py` pins this bug.

## Bug 4: the header is cut off at 200% text

The header's height is fixed at 40 px and it hides anything that overflows. The title's line is 38 px high at normal size and about 77 px at 200% text, so the lower part of the title is cut off whatever the phone's width. The top of the line is clipped too, but only the empty space above the letters is lost there. The cause is the `.app-header` rule in `static/app.css`. `sven` and `grace` both use 200% text.

## Bug 5: "1 tasks left"

The count never uses the singular, because `Handler._index` builds the text with no case for one. Anyone who gets down to one open task should catch it.

## Bug 6: no empty state

When the list is empty, or a search finds nothing, the page shows a blank space under the count. It lives in `Handler._index`, which renders the rows and nothing else. Anyone who deletes every task, or searches for something that is not there, should catch it.

## Bug 7: task text is unreadable in dark mode

Task text has a fixed dark grey colour, while the card behind it turns dark. The cause is the `.task-text` rule in `static/app.css`. Every persona with a dark colour scheme should catch it, which is `sven`, `malik`, `jonas` (when run), `kenji` (when run), `olga` (when run) and `mehmet` (when run). Of those, the demo runs `sven` and `malik` by default.

## Bug 8: the delete control is tiny and permanent

The delete button is 16 by 16 px, it asks for no confirmation and there is no undo. The size is the `.delete` rule in `static/app.css`, and the permanence is in `Handler._change`. The button does have an accessible name, so only its size and its permanence are the bug. Phone personas such as `sven` and `malik`, and `kenji` when run, should catch it, and so should the accessibility check.

## Bug 9: the settings button has no accessible name

The button in the header is a cog drawn in SVG, inside a link with no text and no label. A sighted person sees a settings button, and a screen reader announces a link with no name. The markup is in `templates/settings-link.html`. The accessibility check should catch it, and `clara`, who judges every detail of a page, may too.

## Bug 10: the toolbar overflows small phones

The search toolbar is a fixed 420 px wide, so a 320 px phone scrolls sideways to reach its right edge. The cause is the `.toolbar` rule in `static/app.css`. `sven` and `anna` both use an iPhone SE.

## Bug 11: due dates always show the month first

A due date of 15 December reads 12/15/2026 whatever the reader's language or region. It lives in `show_due` in `serve.py`, which ignores the browser's locale. `layla` (Egypt) expects the day first and is run by default, and so do `olga` (Russia) and `jonas` (Germany) when run. The seed data has due dates on days 13 to 30 so that a day-first reader cannot take a date as a month.

## Bug 12: the settings page throws a JavaScript error

`static/settings.js` looks for an element, `theme-picker`, that the page does not have, so the browser logs a TypeError as soon as the page loads. The page still shows and works. A tester sees nothing wrong, because only the browser's console shows the error. The scripted checks in phase 2 are meant to catch it.

## What is built properly

The planted bugs are all about use. Nothing in Tiny Tasks is insecure on purpose. SQL is parameterised, user text is escaped on output, passwords are hashed with scrypt and a random salt, session tokens come from `secrets` and live in an HttpOnly cookie, and every change needs a POST.
