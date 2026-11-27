# Phase 1's runs on a droplet

On 27 November 2026 twelve AI testers ran against [Tiny Tasks](../demo-app/README.md), the demo app with twelve planted bugs, on a DigitalOcean droplet. It was the first time Footfall's testers used a real model rather than a scripted one. Phase 1 is done when the testers find at least 7 of bugs 1 to 11. Bug 12 is a JavaScript error that only the browser's console shows. Phase 2's scripted checks are meant to catch it.

They found 8 of the 11 on the first run and 9 on the second. The first run also showed two faults in Footfall's own tester. Both were fixed before the second run.

## The setup

The droplet had 4 vCPUs, 8 GB of memory and a 160 GB disk, in London, on Ubuntu 24.04.5. Docker Engine was 29.10.1 and gVisor's `runsc` was release 20261026, the release the isolation workflow pins. The droplet's cloud firewall let in SSH alone.

Before either run the isolation suite ran on the droplet as it does in CI. Of its 48 tests, 46 passed. So did the end-to-end test and the 8 checks the workflow makes on the host. The two tests that only mean something inside a GitHub Actions job failed, as they do anywhere else.

Each run was `footfall test /repo/demo-app` inside the development stack, with all twelve personas and `claude-sonnet-5-5`. The demo app's manifest runs six of them by default. The first run used `main` at 9517816. The second used 70ff324, which became #24.

## The two runs

| | First run | Second run |
|---|---|---|
| Time from start to the last report | 2 min 24 s | 2 min 33 s |
| Longest persona | 64 s | 66 s |
| Findings | 57 | 59 |
| Actions | 76 | 78 |
| Cost | $0.58 | $0.61 |
| Planted bugs found, of 1 to 11 | 8 | 9 |

Every persona finished on its own by calling `finish`. No persona cost more than $0.11, far below the run's ceiling of $12.

| Persona | Device | Engine | Findings | Actions | Cost | Seconds |
|---|---|---|---|---|---|---|
| edge-cases | iPad | WebKit | 5 | 11 | $0.11 | 64 |
| first-visit | iPhone SE | WebKit | 5 | 4 | $0.03 | 30 |
| in-a-hurry | Laptop (Safari) | WebKit | 4 | 5 | $0.05 | 33 |
| largest-text | iPhone SE | WebKit | 4 | 1 | $0.02 | 11 |
| less-motion | iPhone 13 | WebKit | 6 | 5 | $0.04 | 37 |
| long-labels | Laptop (Chrome) | Chromium | 5 | 1 | $0.03 | 18 |
| low-vision | Pixel 7 | Chromium | 4 | 9 | $0.05 | 41 |
| one-handed | Pixel 5 | Chromium | 7 | 17 | $0.10 | 66 |
| right-to-left | Pixel 7 | Chromium | 5 | 4 | $0.03 | 28 |
| skims | Pixel 7 | Chromium | 4 | 5 | $0.04 | 35 |
| tablet-layout | iPad Mini | WebKit | 4 | 6 | $0.06 | 42 |
| unusual-text | Galaxy S9+ | Chromium | 6 | 10 | $0.06 | 55 |

The table above is the second run's.

## The planted bugs

| Bug | First run | Second run, found by |
|---|---|---|
| 1. Every sign-in error says "Something went wrong" | No | one-handed |
| 2. Search is case-sensitive | Narrowly | edge-cases, narrowly |
| 3. Empty tasks can be added | No | No |
| 4. The header is cut off at 200% text | Yes | low-vision, largest-text |
| 5. "1 tasks left" | No | No |
| 6. No empty state | Yes | edge-cases, unusual-text |
| 7. Task text is unreadable in dark mode | Yes | six personas |
| 8. The delete control is tiny and permanent | Yes | nine personas |
| 9. The settings button has no accessible name | Yes | all twelve |
| 10. The toolbar overflows small phones | Yes | eight personas |
| 11. Due dates always show the month first | Yes | six personas, in German, French, Russian, Spanish and British English |

Bug 2 counts narrowly. edge-cases searched for "istanbul" and found nothing, though a task begins "İstanbul". The tester blamed the Turkish dotted İ rather than case in general. Without it the runs found 7 and 8.

Bug 1 came through a fault in Footfall. one-handed typed its username, a tab and its password in one call. The tab did not move to the password field. The form went with an empty password. The app's answer, the same vague message for every mistake, is bug 1 itself.

The testers missed bugs 3 and 5 in both runs. Each needs a deliberate act: pressing Add task with nothing typed, or deleting tasks until one is left. The testers looked far more than they acted, at about six actions each. Phase 2's briefs should ask for more of that kind of trying.

## What else the testers found

These are real problems in Tiny Tasks that nobody planted.

- After every action the page reloads at the top. A new task goes to the bottom of a long list. Nothing on screen says it was added. Four personas reported it.
- The count of tasks left ignores a search. It says 9 while one task shows.
- Beside the wide buttons, task titles squeeze into a column one word wide. A long title runs out of its card.
- The settings page has a Preferences heading with nothing under it. "Back to your tasks" appears twice in the accessibility tree.
- The app is in English alone and runs left to right. right-to-left reported that Arabic gets no right-to-left layout. Three more personas reported labels and dates that ignore their language.
- On an iPad Mini the layout stays one narrow column.

## What the runs found in Footfall

The first run showed two faults in the tester. #24 fixed both before the second run. The second run and CI each showed one more. The memory figures showed a limit set too low.

- **Reading a page in motion.** A screen was read while smooth scrolling was still under way. Its element positions came from mid-scroll. low-vision asked to tap "Mark done: Buy oat milk". The tap landed 306 px away, on another task. After a tap that posted a form, WebKit answered from the page the tap had left while the app had not yet replied. Five WebKit personas reported a black screen. #24 waits for a started navigation and for the page to stop moving. It keeps a read only if the page did not move during it. The second run had neither fault.
- **Tabs.** The `type` tool promises that a typed tab moves to the next field. Playwright types a tab into the field as a character. #28 presses the Tab key.
- **Steps that fail at once.** The runner reads a container's address after starting it. CI's isolation run for #24 had an install that exited at once. Docker had already cleared its network details. The runner reported a missing address in place of the exit code. #25 asks Docker whether the container has stopped.
- **Browser memory.** The browsers container came close to its limit, as the next section shows.

One mistake was the model's. edge-cases deleted the wrong task by reusing an element number from an earlier screen. Footfall tapped what the latest screen numbered.

## Memory

`scripts/measure_memory.py` sampled each container through both runs and read each one's own peak from its cgroup.

| Role | Containers | 2 personas (MiB) | 12 personas, first run (MiB) | 12 personas, second run (MiB) |
|---|---|---|---|---|
| browsers | 1 | 519 | 1,985 | 1,940 |
| app | one each | 47 | 47 | 49 |
| orchestrator | 1 | 288 | 200 | 200 |
| proxy | 1 | 97 | 96 | 96 |
| runner | 1 | 94 | 96 | 96 |
| seed | 1 | 28 | 31 | 29 |

The two-persona figures are from the isolation suite's end-to-end test on the same droplet, with a scripted model. The orchestrator's figure there includes the suite's own test runner.

All twelve personas share the one browsers container. Its limit was 2 GiB. Twelve personas reached 97% of it. Each persona past the first two adds about 150 MiB. #27 raises the limit to 4 GiB. #26 will make it follow the run's personas.

The whole stack peaked at 2,706 MiB with twelve personas. With the browsers at their new limit, one run of twelve could reach about 4,800 MiB. A droplet with 8 GB holds that with room for the system. One with 4 GB would not.

## Time

The isolation suite took 15 min 50 s on the droplet. GitHub's runner also has 4 vCPUs and takes 7 to 8 minutes. A droplet's vCPUs are shared. The suite starts many containers one after another.

A run of twelve personas took two and a half minutes. Preparing the app took 22 seconds of that. The personas ran together for the rest.
