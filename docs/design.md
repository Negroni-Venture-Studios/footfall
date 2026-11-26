# Footfall: design

Footfall tests web apps that AI coding agents build and maintain. It runs on a server the deployer owns. One instance serves a GitHub organisation or one developer. An agent that has pushed a change asks Footfall to test it. Footfall clones that commit, runs the app in a sandbox and sends a dozen AI testers at it. Each tester plays a different person on a different emulated phone, tablet or laptop. The agent gets structured findings back and can fix what the testers found before a person looks. People watch each run live, read its report and manage Footfall from its web pages.

Status: phase 1 is built and in review in [pull request #1](https://github.com/Negroni-Venture-Studios/footfall/pull/1).

## Summary

- One Django service on a DigitalOcean droplet behind Caddy, with an MCP endpoint for agents and web pages for people. Both sign in through a GitHub App the deployer creates during setup.
- Each persona gets its own copy of the app in a fresh container under gVisor. The container has no secrets and no network except the hosts its manifest names. The run's browsers get a fresh container of their own. Only a small runner process can start containers. Nothing that faces the internet can reach it directly.
- The testers are Footfall's own loop on the Anthropic API, with six tools that drive a browser and nothing else. They have no shell, no files and no way to type a URL.
- Agents get structured findings. People get a live device wall while a run is going and a summary report when it finishes.
- The sandbox and the tester are built first because the security model depends on them. They are driven from the command line against a demo app with planted bugs.

## Why Footfall exists

Agents have made software cheap enough to build for one user ([A distribution of one](https://negroniventurestudios.com/2026/06/09/a-distribution-of-one/)). Agents build a flood map for one river, a dashboard for one solar array or a data room for one company. They rebuild them too and often nobody else reads the code. The specification lasts. The code gets replaced whenever the problem changes.

These apps still need someone to use them the way a stranger would. Unit tests check what the author expected to happen. A tester playing a person on a particular device finds what nobody thought to check, such as a button hidden by the keyboard on a small phone, a chart cut off at large text, or an error that only says "Error".

Footfall does this for web apps. It is a service the coding agent calls itself when it finishes a change. The check then happens on every change without anyone having to remember it. Each app's repository holds a brief saying what the app is for and what each kind of person should try to do. That brief is the specification the testers work against. It survives every rebuild of the code.

## Who it is for

Deployers have a DigitalOcean account and a GitHub account or organisation. They point their own coding agent at this repository and it walks them through setup. They type every credential themselves. One instance serves a whole GitHub organisation and its developers, or one developer on a personal account. Footfall is MIT-licensed and free. Deployers pay DigitalOcean for the droplet and Anthropic for the model.

People sign in with GitHub to watch runs, read reports, make tokens for their agents and change settings.

Agents call the MCP endpoint with a token a person made for them.

## Goals

1. An MCP endpoint on the public internet, with authentication, that agents can call from a laptop, a home server or a cloud session, to start a run of a repository at a commit, follow its progress and fetch its results.
2. For each run, clone the commit, prepare data and start the app in an isolated container, run scripted checks on every device, run several personas in parallel, each on its own emulated device, and merge their findings.
3. Testers whose only abilities are a few browser actions Footfall defines, confined to the app under test, with nothing in reach worth stealing.
4. Results shaped for an agent to act on, and the same results as web pages for a person.
5. Web pages for a live device wall, a summary report, the list of runs, agent tokens and settings. Nobody should need the DigitalOcean console once Footfall is set up.
6. Hard limits on spend and load.
7. Deployable by other people, with generic configuration, setup an agent can guide, documentation with diagrams, and a demo app with planted bugs.

## Non-goals

- Native iOS or Android apps, for now. iOS is a likely later addition. It needs macOS and would need a Mac worker. Nothing here is built for it yet.
- Real phones or real mobile browsers. Playwright emulates devices. Chromium and WebKit stand in for Chrome and Safari.
- Testing uncommitted code. The agent pushes a branch first.
- Building an app's own Dockerfile. That is a possible later addition.
- Signing in to an app under test through an outside provider such as Google or GitHub because testers cannot leave the app.
- Deterministic regression suites, and load, performance or security testing of the apps.
- Production monitoring, or testing sites the deployer does not own.
- Serving more than one organisation from one instance.
- Public sharing of reports.
- Comparing two versions side by side. That is a possible later addition.

## Anti-goals

Footfall has failed if any of these becomes true, even if it works:

- It can be used as a general-purpose browser or proxy to reach arbitrary hosts.
- A tester, or the code under test, can reach the Anthropic key, the GitHub App's keys, agent tokens, or Footfall's own files and database.
- Text a tester wrote reaches a calling agent in a form that reads as instructions. That text may echo what the app displayed.
- A run, a person or a day can spend without limit.
- Anyone outside the deployer's GitHub organisation can start runs or read results. On a personal instance that means anyone but the deployer. Or a member can see the runs of a repository they cannot read on GitHub.
- Calling it is slow or awkward enough that agents skip it.

## Constraints

- One Linux server. The reference is a DigitalOcean droplet on Ubuntu 24.04. It is sized by how many personas run at once and phase 1 measures that.
- Python 3.12 or later, Django, Playwright for Python, the Anthropic Python SDK, the MCP Python SDK, Docker with gVisor, and SQLite. Any other dependency needs a stated reason.
- One Anthropic API key per instance. Ideally it comes from a workspace with its own spend limit. It is never a personal subscription token.
- Repositories on GitHub, read through a GitHub App the deployer creates.
- The MCP endpoint follows the [2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28/changelog) and only that version. It is an OAuth 2.1 resource server and publishes Protected Resource Metadata (RFC 9728). It accepts tokens issued for it by Footfall's own authorisation server and bearer tokens made on its web page.
- The apps under test vary. My own are Python's `http.server`, SQLite and vanilla JavaScript with no build step. [Floodwatch](https://github.com/aallan/floodwatch) is one. Nothing in Footfall may depend on that.
- MIT licence. Parts of the tester are adapted from [mobile-agent-testers](https://github.com/pierridotite/mobile-agent-testers), an MIT-licensed project that runs AI testers against native mobile apps. Those parts are its screen format, its accessibility check, its rules and the personas. The project's notice is kept.
- Footfall follows [Snug](https://github.com/Negroni-Venture-Studios/snug) for security, deployment, documentation and styling and reuses its code where it fits. Snug is Negroni Venture Studios' self-hosted data room.
- The repository is public and everything committed is published.

## Approaches considered

| Area | Options | Chosen | Why |
|---|---|---|---|
| Tester | Claude Code per persona, limited to Microsoft's Playwright MCP server; Footfall's own loop on the Anthropic API with tools it defines; scripted checks only | Footfall's own loop | No shell exists anywhere, every persona has a hard budget, and results come back in exactly the shape Footfall needs. Claude Code with Playwright MCP would depend on two fast-moving tools staying pinned and locked down. Scripted checks alone lose the personas, so they run before the personas instead. |
| Web framework | Django, as Snug uses; Starlette, which the MCP SDK is built on; the standard library | Django | Sessions, CSRF protection, forms, migrations and security headers are built in and long audited, and Snug's security code (fail-closed configuration, the rate limiter, hashed tokens, the append-only audit log, the tests that walk every URL) can be reused nearly as it is. Starlette would mean assembling those by hand. The standard library cannot hold MCP's long-lived connections without the same SDK. |
| Starting containers | A small runner of Footfall's own, the only holder of the Docker socket; an off-the-shelf Docker socket proxy; a virtual machine per run | Footfall's own runner | A socket proxy filters which API call is made, not what it asks for, so allowing "create container" allows a privileged container that mounts the host's disk. Firecracker needs nested virtualisation, which DigitalOcean [does not officially support](https://www.digitalocean.com/community/questions/does-digitalocean-support-nested-kvm-now). The runner writes every container's settings itself and ignores anything else. |
| Sandbox runtime | Docker's default runtime; gVisor; Kata Containers | gVisor | gVisor puts its own kernel between the code under test and the host's, needs no hardware virtualisation, and Snug already runs it on DigitalOcean for its LibreOffice converter. Kata needs nested virtualisation. |

## How it works

### Overview

A run is a request to test one commit of one app. Footfall keeps the parts that hold secrets away from the code under test. Nothing that touches that code holds anything worth taking.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/overview-dark.svg">
  <img alt="Agents and people reach Footfall through Caddy. The web process serves the pages, sign-in, the MCP endpoint and live updates. The orchestrator runs the queue, clones repositories, runs the testers and merges their findings. It alone talks to the Anthropic API and fetches code from GitHub. The web process and the orchestrator share the database and screenshots. Only the runner holds the Docker socket. For each run it starts an app container for every persona and one browser container. All of them run under gVisor and hold no secrets. The orchestrator drives the browsers through Playwright and the browsers reach the app. Anything else either container fetches goes through the egress proxy. The proxy allows only the hosts the run's manifest names." src="images/diagrams/overview-light.svg">
</picture>

| Part | What it does | What it holds | What it can reach |
|---|---|---|---|
| Caddy | HTTPS and its certificates | Certificates | The internet, and the web process |
| Web | Pages, sign-in, settings, the MCP endpoint, live updates | Django's secret key and the GitHub App's client secret, for sign-in | The database, the orchestrator's local socket, GitHub for sign-in, and OAuth clients' metadata documents through the public-address check |
| Orchestrator | The queue, cloning, scripted checks, the tester loops, the merge | The Anthropic key and the GitHub App's private key | The database and screenshots, the runner's local socket, each run's network (where it listens on nothing), the Anthropic API, GitHub |
| Runner | Starts and stops containers with fixed settings | The Docker socket | Docker |
| App containers, one per persona | Each runs its own copy of the app under test | Nothing | The egress proxy only |
| Browser container, one per run | Runs Chromium and WebKit, with a separate browser context for each persona | Nothing | That run's app containers, and the egress proxy |
| Egress proxy | Applies each run's allow list | Nothing | The hosts each run is allowed |

The web process and the orchestrator are separate containers. A flaw in the part that faces the internet then exposes neither the Anthropic key nor the GitHub App's private key. It cannot start a container either.

### A run, start to finish

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/run-dark.svg">
  <img alt="A run starts when an agent or a person asks for one. Footfall checks and queues it and answers at once. It clones the commit with a short-lived token. It installs and seeds the app once. It then starts a copy for each persona and waits until each one answers. Scripted checks then run on every page and device without a model. The personas run in parallel and their findings are merged. The run finishes by removing the containers and keeping the results. Polling the run reports queued, then preparing, then running, then finished. Quick mode stops after the scripted checks. Every screenshot goes to the live wall as it is taken. Results are kept for 14 days by default." src="images/diagrams/run-light.svg">
</picture>

1. **Start.** An agent calls `start_run`, or a person presses Run on a page. Footfall checks the token's scope, that the GitHub App is installed on the repository, that a manifest exists at that commit, the limits, and the day's spend. It resolves the ref to a commit SHA, queues the run and answers at once with the run's id and the address of its live page.
2. **Clone.** The orchestrator fetches the commit into a workspace with an installation token. GitHub makes that token valid for one hour. Only refs in the installed repository are accepted and code from a fork cannot arrive through a pull request.
3. **Prepare.** The runner starts a fresh container from Footfall's base image for the manifest's runtime and copies the workspace in. The manifest's `install` command runs with access to the package registries. Then `seed` runs. The runner then starts a copy of the prepared app for each persona and one for the scripted checks. Testers never see each other's changes and live data is fetched only once. Each copy runs `start` and Footfall polls its `ready` path until it answers. If the timeout passes first the run fails and reports the app's last log lines.
4. **Scripted checks.** Each page the manifest lists is loaded once on every persona's device against the copy kept for the checks. No model is involved. These findings arrive within a minute or two. In quick mode the run ends here.
5. **Personas.** The testers run in parallel. Each has its own browser context, its own emulated device and its own copy of the app. Each runs until it finishes or uses up its budget. Every screenshot goes to the live wall as it is taken.
6. **Merge.** One model call with no tools groups the findings that describe the same problem and writes the summary.
7. **Finish.** Every container is destroyed. The screenshots, findings and each tester's recorded steps are kept for the retention period. That is 14 days by default.

### The manifest

Each app tells Footfall how to run it in `footfall.toml` at the root of its repository. Footfall reads it with the standard library's `tomllib`. This is an example for a small Python app:

```toml
name = "Tiny Todo"

[app]
runtime = "python3.13"          # one of Footfall's base images
install = "pip install -r requirements.txt"
seed = "python3 scripts/seed.py --awkward"
start = "python3 serve.py 8080"
port = 8080
ready = "/healthz"              # polled until it answers
ready_timeout = 60              # seconds
egress = []                     # hosts the app may reach once started

[browser]
assets = []                     # other hosts the pages may load, such as a map's tile server

[login]                         # optional, for apps with their own sign-in
page = "/login"
username_label = "Email"        # the labels of the fields on the page
password_label = "Password"
submit = "Sign in"              # the text of the button

[test]
pages = ["/", "/settings"]      # for the scripted checks
briefs = "footfall/briefs"      # default.md is used unless the caller names another
personas = ["sven", "grace", "jonas", "layla", "noah"]
```

| Field | Meaning |
|---|---|
| `name` | The app's display name |
| `app.runtime` | Which of Footfall's base images to start from; Python and Node to begin with |
| `app.install` | Optional. Installs dependencies, with the package registries reachable |
| `app.seed` | Optional. Prepares data before the app starts, by copying a committed fixture, generating fake data, or fetching live public data |
| `app.start`, `app.port` | The command that starts the app and the port it listens on |
| `app.ready`, `app.ready_timeout` | A path to poll until the app answers, and how long to wait |
| `app.egress` | Hosts the app may reach while seeding and running. None by default |
| `browser.assets` | Hosts the pages may load besides the app itself. None by default |
| `login` | Optional. How to sign in to the app: the sign-in page, the labels of its two fields, and its button |
| `test.pages` | The pages the scripted checks load. The start page by default |
| `test.briefs` | A folder of briefs. Each says what the app is for, what each persona should try, which pages and states matter, and what is out of scope, in at most 8,000 characters, since it goes into every persona's prompt |
| `test.personas` | The personas used unless the caller names others |

For an app with its own sign-in Footfall makes a throwaway username and password for each run and passes them to `seed` as `FOOTFALL_USERNAME` and `FOOTFALL_PASSWORD`. The seed step creates that account. Every container also has `FOOTFALL=1` set. An app can use it to turn on a way in meant only for testing, such as skipping an emailed code. Footfall signs each persona in before it starts unless the brief asks for signing in to be tested. It also signs in before the scripted checks load their pages.

Where the data comes from is up to each app. The documentation will urge seed steps that include awkward states as well as the happy path. For a flood map that means a severe flood warning, a gauge that has stopped reporting, gaps in a time series, and readings either side of midnight. An agent that writes both a change and the data it is tested against can otherwise test only what it expects.

### Personas

A persona is a person with a device. Footfall ships a personas file adapted for the web from mobile-agent-testers' `personas.json`. Deployers can edit or replace it. Each persona has:

- an id, a short profile (who they are, their patience and skill) and a default goal, which a brief can override for an app;
- a device, named from a separate list of devices, each of which gives a Playwright device descriptor (viewport, pixel density, touch and user agent) and an engine, WebKit (closest to Safari) or Chromium;
- a locale, a timezone, a colour scheme, reduced motion and a geolocation;
- a text scale. Browsers have no equivalent of a phone's text size setting. Footfall approximates it by scaling the page's root font size and the report says so.

Devices sit in their own list so that a later iOS worker can map the same device names to simulators without rewriting any persona. The starting set has twelve personas. That is enough to fill the device wall. Between them they cover a small phone, a large phone, a tablet and a laptop; light and dark; text up to 200%; German, Japanese and Swedish; Arabic, right to left; and a person who asks for reduced motion.

### The tester

Each tester is a loop in which the model sees a screenshot of the page and a numbered list of what is on it, chooses one action, and is shown the result once Footfall has performed it. The loop ends when the tester writes its report or runs out of budget.

| Tool | What it does |
|---|---|
| `look` | Returns a screenshot, a numbered list of interactive and text elements with their centres, in the format of mobile-agent-testers' `screen.py`, and accessibility warnings for the screen |
| `tap` | Taps an element by number, or a point by coordinates |
| `type` | Types text into the focused field, up to a fixed length |
| `scroll` | Scrolls up, down, left or right |
| `go` | Goes to a path on the app, goes back, or reloads |
| `finish` | Writes the structured report and ends the loop |

`go` accepts a path and never a URL. A tester cannot leave the app however a page words its instructions. The guard aborts and logs every request it is given that is not addressed to the app or a host in `browser.assets`, and every navigation that would take the main frame off the app. Downloads and file pickers are refused and new windows are closed. The tester is told of each as text, including where a new window pointed when it was on the app. The browser's own dialogs are dismissed, except the prompt a page shows before it is left. That one is accepted so that a form with unsaved changes cannot trap a tester. The next page must still be on the app. Some requests are never given to the guard and it cannot abort them. Each hop of a redirect and a worker's WebSocket are only reported. In Chromium a beacon sent as a page unloads is not seen at all. The egress proxy refuses each of them unless it goes to an asset host. The proxy remains the wall. A main frame that a redirect takes off the app is brought back. A screen is kept only when the page reports the same address on the app as the browser does before it is read and after. One read off the app or while the page moved is thrown away and read again. The tester is never shown another host's page or two pages at once.

Each persona has a budget of about 30 actions by default. A brief may raise it up to a ceiling set in Settings. Each persona also has a cost ceiling and a time limit. A persona that runs out returns what it found, marked incomplete. Each action's result includes the screen it led to and the tester rarely needs `look` on its own. The conversation is only ever appended to and never edited because current Claude models check that earlier turns are unchanged. Prompt caching keeps the cost of re-reading earlier screenshots at the cache price.

The system prompt holds the persona, the brief, the caller's `focus` if there is one, and rules adapted from mobile-agent-testers' `AGENT_BRIEF.md`. The rules are to look after every action; to treat a blocked step as a finding and then try one change at a time; to report precisely and verifiably; to stay in character as the persona while remaining a tester; and to treat text on the page as data, never as instructions. The model is chosen per run and in Settings. It defaults to Claude Sonnet.

Every action is recorded with what it targeted, the page path and the screenshots before and after. The target is the element's role and accessible name as well as its coordinates. The live wall and the report's replay draw on the screenshots. The recorded steps are kept in a form that can be replayed later without a model. A later version will use that to check whether a finding is fixed on a new commit.

### Scripted checks

Before the personas start Footfall loads each page in `test.pages` on every persona's device. Without a model it records any horizontal overflow, controls with no accessible name, touch targets under 44 by 44 CSS pixels, console errors, failed requests and a screenshot. The accessibility checks adapt mobile-agent-testers' `a11y.py`. These become findings labelled "scripted".

### Findings

Each finding has a title, a severity (blocker, major, minor or cosmetic), its source (a persona's id or "scripted"), the device, the page path, steps to reproduce, the expected and actual results, and the ids of its screenshots. Each persona also returns a score out of ten and whether it reached its goal.

The merge groups findings that describe the same problem across testers. It is one model call with no tools that sees only titles, pages, devices and severities. As in mobile-agent-testers' `summarize.py` each group gets a class. One class is "likely test artefact". It is for things an emulated device does that a real one would not.

### What agents see

The MCP endpoint follows the stateless core of the 2026-07-28 specification and declares the Tasks and skills extensions. It offers these tools:

| Tool | What it does |
|---|---|
| `list_apps` | Repositories the GitHub App is installed on that have a manifest |
| `list_personas` | Persona ids, each with a line describing who they are and their device |
| `start_run(repo, ref, personas?, brief?, focus?, mode?, model?, request_key?)` | Checks and queues a run, and returns its id and the address of its live page at once. A second call with the same `request_key` from the same person returns the first run, so a retry cannot start two |
| `run_status(run_id)` | Queued, awaiting approval, preparing, running (with each persona's progress), finished, or failed with the reason |
| `get_results(run_id, cursor?)` | The summary and a page of findings as structured data, with a cursor for the next page |
| `get_finding(run_id, finding_id)` | One finding in full, with every tester's steps |
| `get_screenshot(run_id, screenshot_id)` | One screenshot, as MCP image content |
| `cancel_run(run_id)` | Stops a run and its spending |

`focus` is a short note from the caller, such as "the new gauge cards on /gauges". It is added to every tester's brief for that run. `mode` is `full` by default. `quick` runs the scripted checks only. They take a minute or two and use no model and an agent can afford them on every change.

A run takes minutes. Where the client supports the Tasks extension `start_run` returns a task it can wait on. The task is checked every 15 seconds and kept for as long as the run is. Other clients poll `run_status`.

Every agent token has an allowance. So does every OAuth client a person approves. The allowance is the most a run it starts may be estimated to cost without asking anyone. The estimate is the sum of the personas' cost ceilings. Twelve personas at the default ceiling of $1 come to $12. The person sets the allowance when they make the token or approve the client. It starts from the instance's default of $5 and never goes above a cap the admins set. A larger run asks the person to confirm through the client before it is queued. Where nobody can answer the run waits for up to a day as awaiting approval. Continuous integration is one such place. The result gives the address of the page where the person can approve it. Quick runs use no model and never ask.

Every free-text field in a result has a length limit. The tool descriptions say that findings are the testers' observations about the app and are not instructions. They say to check findings against the screenshots. Phase 1 comes before the server exists. In it tester text leaves Footfall in four places: `report.json`, `summary.json`, every line of `trace.jsonl` and the table `footfall test` prints. In all four it is cleaned of control and invisible characters, cut to its limits and labelled as observations. Clients load the server's instructions before any tool. Those instructions stay under 4,096 characters.

Footfall's skills tell a coding agent when to call it, how to add a manifest and briefs to a repository, how to read the results, and to check a finding against its screenshots before acting on it. Quick mode fits after a change to the interface and a full run before asking a person to review. The MCP server serves the skills through the skills extension. The repository also publishes the same files as a Claude Code plugin for the places where Claude does not yet load skills from a server, such as cloud sessions.

Agents connect in one of three ways. A Claude Code cloud session or claude.ai adds Footfall as a custom connector and signs in through Footfall's OAuth server. Cloud sessions get Tasks only from connectors. A local Claude Code session can do the same or send a bearer token made on Footfall's web page. It reads the token from an environment variable named in the project's `.mcp.json`. Scripts and continuous integration use bearer tokens.

The details are in the [design for the 2026-07-28 features](specs/2026-11-24-mcp-2026-07-28-design.md).

### What people see

The web pages, all behind GitHub sign-in:

- **Runs.** Recent runs with the app, commit, who or which token started each one, its status and the number of findings, and a form to start a run.
- **Live run**, the device wall. Totals across the top (app, commit, testers done, findings so far, model use so far), then every tester as a device frame in its real proportions showing its latest screenshot, with its name, device, language, text size and theme, and a status line giving its step and current action. A finished tester shows its count of findings. Selecting a device opens a larger view with its recent actions. Updates arrive by server-sent events.
- **Report**, modelled on mobile-agent-testers' [write-up of one of its runs](https://github.com/pierridotite/mobile-agent-testers/blob/main/docs/runs/localsend-20.md). The run's facts (app, commit, branch, who asked for it, the brief); headline numbers (distinct problems, blockers and majors, goals reached, time from start to last report, model use); a replay of the device wall from the run's screenshots; a chart of how many testers saw each problem; the problems, most severe first and grouped across testers, each with its screenshots and expandable to every tester's steps, expected and actual results, with scripted findings among them and labelled; a table of the devices; and the call that would run it again.
- **Agent tokens.** Make, name, scope and revoke tokens, and see when each was last used.
- **Settings**, for admins.
- **First run.** The page a new instance shows until it is set up.

Two mockups fix the layout and content of the device wall and the report. They do not fix the styling, which follows the branding below. Their figures and findings are illustrative.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/mockups/live-run-device-wall-dark.png">
  <img alt="Mockup of the live run page for an app on 12 devices. A bar across the top gives the commit and branch, 8 of 12 testers done, 14 findings so far and the model use so far. Below it, five device frames in their real proportions (a small phone, a large phone, a tablet, another phone and a laptop), each labelled with the tester's name, device, language, text size and theme. Finished testers show a badge with their count of issues; the others show their step and current action, such as step 14 of 30, tapping Gauges." src="images/mockups/live-run-device-wall-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/mockups/summary-report-dark.png">
  <img alt="Mockup of the summary report for the same run. Under the title, the commit, branch, the agent token that asked for it and the brief. Five headline figures: 18 distinct problems, 1 blocker and 5 majors, goals reached by 10 of 12 testers, 7 minutes from start to last report, and $1.84 of model use. Then a replay strip of the device wall with a play button; a bar chart of how many testers saw each problem, where 11 problems were seen by one tester only and one by all 12; the problems, most severe first, each with its severity, page, the testers who saw it and thumbnails of its screenshots, with a scripted finding among them; a table of the devices with engine, language, text size, theme, goal reached, model use and findings; and the call that would run it again." src="images/mockups/summary-report-light.png">
</picture>

Both are also in the repository as HTML: [the live run page](design/mockups/live-run-device-wall.html) and [the summary report](design/mockups/summary-report.html).

### Accounts and access

Each instance has its own GitHub App. The deployer creates it from Footfall's first-run page through GitHub's [manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest). One click on GitHub creates the App with its permissions already set and sends its credentials straight back to the server. The App belongs to the deployer's organisation or account and can be installed only there. It asks for read access to repository contents and metadata and to the organisation's members.

People sign in through the App. It reads repositories through short-lived installation tokens and no long-lived repository credential is stored. It also decides who is allowed in:

- On an organisation's instance any member of the organisation may sign in, or only the members of one team if Settings names one. The organisation's owners are Footfall's admins and can change settings.
- Each person sees and can start runs only for the installed repositories they can read on GitHub. The runs list, reports, screenshots, MCP tools and agent tokens are all limited in the same way.
- On a personal instance only the account that owns it may sign in. It is the admin.
- Membership and the repositories each person can read are checked at each sign-in. They are checked again at least hourly for open sessions and for tokens in use. Someone who leaves the organisation or loses access to a repository loses it in Footfall too without anyone editing Footfall.

The repositories Footfall can test are exactly those the App is installed on. An agent cannot point it at anything else.

Each person makes their own agent tokens. A token has a name, a scope and an expiry. The scope is start and read runs, or read only. It can be limited to some of the repositories its owner can read. A token is shown once, stored only as a SHA-256 hash of its 32 random bytes and listed with its last use. It can be revoked. Every run records the person and the token that started it. Requests are rate-limited per token.

Phase 4 adds an OAuth authorisation server built on a maintained library inside the web process. It delegates sign-in to GitHub and issues tokens bound to the MCP endpoint. Clients that expect the standard flow, such as claude.ai's custom connectors and Claude Code, can then connect given only the address. Any client may ask to connect. The consent page shows its name, the host it returns to, the scope and the allowance. Admins can limit connections to a list of clients. Both kinds of token have the same scopes and are checked the same way. Enterprise-managed authorisation for Claude's Team and Enterprise plans is [issue #6](https://github.com/Negroni-Venture-Studios/footfall/issues/6).

### Settings

The setup script on the server sets two values once. They are the domain and Django's secret key. Until the GitHub App exists there is nobody to sign in. The first-run page asks for a one-time code the setup script prints. That stops anyone who finds the address first from claiming the instance. Everything else is on the Settings page for admins:

- the Anthropic key, and the default model;
- limits and spend ceilings;
- the team that may sign in, if not the whole organisation;
- how long runs are kept;
- the default personas, and the ceiling on a persona's actions;
- the default allowance for a run, and the cap on it;
- the OAuth clients allowed to connect, if not every client.

Keys typed into Settings are write-only. The web process passes them to the orchestrator over its local socket. The orchestrator keeps them in a file on its own volume. The page shows only the last four characters. The GitHub App's credentials arrive the same way during setup. The private key goes to the orchestrator and the client secret stays with the web process. Every change to Settings goes in the audit log.

### Limits and spend

The configured limits are personas per run, personas running at once across all runs, queued runs, a daily spend ceiling for the instance and another for each person, and request rates per token. The server's memory sets how many personas run at once. Each persona also has its own budget of actions, cost and time.

A run estimated above its token's or client's allowance waits for a person to approve it. What agents see describes that. A `footfall test` run has no token. It has a cost ceiling the operator sets with `--cost-ceiling` or `FOOTFALL_RUN_COST_CEILING_USD`. The ceiling is $12 unless set, which is the worst case for the default personas. The tested repository cannot raise it. Each persona checks the run's spend before each turn and starts no other once the run has reached the ceiling. The run can pass it by at most one turn for each persona running. Footfall prices each model call from the usage the API reports and a price table in the code. It checks the ceilings before each persona starts and after every model call. It stops a persona once its run, its person or the instance reaches a ceiling. Settings shows spend for today and this month, by person.

### Isolation and security

The code under test is untrusted, even when the deployer's own agent wrote it. It may have a bug that behaves like an attack, or a dependency that is one. The pages it serves are untrusted too, because they become part of a model's input. Footfall therefore treats the app, its pages and everything the testers write as hostile. It keeps anything worth taking out of their reach.

| Threat | Controls |
|---|---|
| Code under test escapes its container | gVisor; no Linux capabilities; a read-only root filesystem except the workspace and a temporary folder; limits on memory, CPU, processes and disk; nothing secret inside; only the runner can talk to Docker |
| Code under test reaches the internet, the host or other services | An internal network only; everything else goes through the egress proxy, which allows each run only its manifest's hosts (and package registries during `install`); DigitalOcean's metadata address, `169.254.169.254`, is refused outright |
| A page steers the tester | The tools accept paths, never URLs; requests to any other host are aborted; the tester holds nothing worth leaking; the prompt says page text is data |
| A tester's text steers the calling agent | Length limits on every field; tool descriptions that call findings observations; the merge sees titles only |
| A hostile page attacks the browser | Browsers run in their own container per run, under gVisor, with no secrets |
| Someone on the internet | Sign-in only for the organisation's members; hashed tokens; rate limits; Snug's security headers and content security policy; no published ports except Caddy's; a firewall on the droplet and SSH by key only |
| The web process is compromised | It holds neither the Anthropic key nor the GitHub App's private key, and cannot start containers |
| Spending runs away | The limits above, checked after every model call |

Hosts in `app.egress` and `browser.assets` must be host names, never addresses or wildcards. The proxy refuses any host that resolves to a private, loopback or link-local address. It connects to the address it checked and does not look the name up again, as Snug does for email. Anyone who can push to a tested repository can therefore widen what that repository's runs may reach but only to public hosts. Each run's page lists every host its containers reached.

Footfall cannot protect against someone with root on the droplet, against DigitalOcean itself, or against a compromised GitHub account belonging to an organisation owner. A tester can also be wrong and that is why every finding has screenshots. A plain-English `docs/security.md` for deployers will set this out in the manner of Snug's.

### Data and retention

State lives in SQLite in WAL mode: runs, the personas in each run, findings, recorded steps, spend, tokens (hashed), people and their cached roles, sessions, settings other than keys, and an append-only audit log protected by triggers, as in Snug. Screenshots are files on a volume. Runs are deleted after 14 days by default and the audit log after a year.

Version 1 has no backups. Everything except settings and tokens expires within two weeks and both of those can be made again in a few minutes. DigitalOcean's own droplet backups are an option for anyone who wants them.

### Deployment

One `docker compose` file runs Caddy, the web process, the orchestrator, the runner and the egress proxy. The runner starts the per-run containers. Images are built in CI, published to GitHub's container registry and pinned by digest. A `cloud-init` file prepares a fresh Ubuntu 24.04 droplet: Docker from its own apt repository, gVisor installed and registered, a firewall, unattended upgrades, swap, and SSH by key only. It contains no secrets, because DigitalOcean's metadata service serves it back to anything on the droplet that asks. An update script follows Snug's. It fetches release files, checks them against published checksums, pins images by digest and refuses to downgrade. The runner gives each run a disk budget of `FOOTFALL_DISK_BUDGET_MIB` MiB, which is 8192 unless set. The budget counts every copy of the run's app. The runner also gives each run a lifetime of `FOOTFALL_RUN_LIFETIME_MINUTES` minutes, which is 90 unless set. After that it stops the run whether or not its caller is still there. The runner does not start with a disk budget that is not a whole number of MiB from 1 to 16,777,216 (16 TiB). It does not start with a lifetime that is not a whole number of minutes from 1 to 10,080 (a week). An empty value stops it too. Each run's files are in `/var/lib/footfall-runs`. That folder must belong to root and be writable by root alone. So must every folder above it on the host because the runner writes there as root and names its folders to Docker. Only the runner mounts it. The runner refuses to start unless `/var/lib/footfall-runs` as mounted passes that check. Each folder above it must pass too, as the runner's own container shows it. The runner cannot see the host's folders above it. Docker goes through those to find the mount and the deployment keeps them root's. Before it serves the runner removes every container, network and run folder an earlier runner left. One Footfall stack owns each Docker daemon. That sweep removes every container and network on the daemon that carries Footfall's run label, whichever stack made it. The egress proxy listens only at its own address on each run's network when the runner tells it.

A `footfall doctor` command checks that gVisor runs, that a test container cannot reach the internet or the metadata address, that the proxy refuses a host it should, that the browsers start, and that the disk space and the certificate are in order.

The droplet's size depends on how many personas run at once. Phase 1 measures that. The deployment guide will chart memory against personas so deployers can choose.

### Setup guided by an agent

The repository's `AGENTS.md` has a section an agent can follow to set Footfall up with someone. `docs/deploy-digitalocean.md` has the same steps click by click for a person. The agent never reads, prints, stores or asks for a credential. The person types each one into the server or into GitHub. The agent can still check each step by looking up the DNS record, asking the health endpoint whether the server answers, and running `footfall doctor`. The steps are to:

1. create a droplet from the `cloud-init` file;
2. point a DNS name at it;
3. run the setup script in DigitalOcean's console, which writes the domain and the secret key and prints a one-time setup code;
4. open the address, type the setup code, and follow the first-run page, which creates the GitHub App and has the person install it on their repositories;
5. paste the Anthropic key into Settings;
6. make a token and add Footfall to the agent's MCP configuration;
7. run the demo app.

### Front end and branding

Footfall is a Negroni Venture Studios project and follows Snug's adaptation of the Negroni brand, with Footfall's own mark:

- the Negroni palette in CSS `light-dark()` tokens, dark by default, with a light, dark and system toggle;
- Geist and Geist Mono (SIL Open Font Licence) in place of the brand's commercial fonts, which cannot ship in an MIT repository;
- Phosphor icons, server-rendered pages, a strict content security policy with no inline scripts, and pages that work without JavaScript, apart from the live updates on the device wall;
- contrast measured by a test, as in Snug.

Footfall steps back further than Snug. Its pages exist to show other people's apps. The brand's colour stays in the bar across the top and on the primary button. The device wall and report use neutral frames that leave the screenshots to dominate. Campari already marks what to press and the severity colours are distinct from it. The stylesheet will define them with contrast checked in both themes.

### Documentation

Documentation grows with each phase:

| Document | For | Phase |
|---|---|---|
| `README.md` | Everyone | 1, and every phase after |
| `docs/design.md` | Contributors; this document | Now |
| `docs/architecture.md` | Contributors, on how the code works | 1 onwards |
| `docs/security.md` | Deployers, on what Footfall protects against | 1 onwards |
| `docs/manifest.md` | App authors and their agents, on `footfall.toml`, briefs and seed data | 2 |
| `docs/deploy-digitalocean.md` | Deployers, click by click | 3 |
| `docs/configuration.md` | Admins, on every setting | 3 |
| `docs/connecting-agents.md` | People connecting Claude Code locally, in the cloud, and other MCP clients | 4 |
| `docs/personas.md` | Anyone editing the personas | 2 |
| `docs/using-footfall.md` | People using the web pages | 5 |

Diagrams follow Snug's method. Each is drawn by hand as an SVG with classes in place of colours. `docs/diagrams/build.py` writes a light and a dark copy with the palette and fonts inside and the Markdown shows the one that suits the reader's theme. Each diagram's title says what the diagram shows. A diagram whose information appears nowhere else in the text gets a text version beside it in a collapsed block. Agents read these documents to set Footfall up and cannot see images. Every embed uses a path relative to the repository. Charts use measured data only, such as memory against personas and the cost of real runs. A test fails if a built diagram is out of date. The screenshots of the web pages are generated by Playwright tests against the demo app in both themes.

### The repository

The repository follows Snug's:

- `AGENTS.md` holds the rules for agents, and `CLAUDE.md` imports it;
- `CONTRIBUTING.md`, the Contributor Covenant, `SECURITY.md`, `TRADEMARKS.md`, and a `THIRD_PARTY_LICENCES.md` that includes mobile-agent-testers' MIT notice;
- a changelog in the Keep a Changelog format;
- `uv` with `uv_build`, ruff with its security rules, strict mypy, and pytest with a coverage floor of 90%;
- CI with actions pinned by commit hash and least-privilege permissions, running gitleaks, pip-audit, CodeQL, a CycloneDX software bill of materials, an image build, and a smoke test of the whole compose stack;
- pre-commit hooks that match CI, Dependabot each Monday, issue and pull request templates, and a protected `main` branch;
- British English throughout.

## Build order

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/build-order-dark.svg">
  <img alt="Phase 1 builds the sandbox and the tester. That is the runner, the gVisor containers, the egress proxy, the browsers, the tester's tools and loop, and the demo app. It is done when every isolation test passes. Phase 2 builds runs: the queue, limits, scripted checks, findings, merging and spend. It is done when a run produces a merged report. Phase 3 puts Footfall on a droplet with compose, Caddy, cloud-init, the GitHub App, sign-in, settings and tokens. It is done when a fresh droplet can be set up from the docs. Phase 4 builds the MCP endpoint, the OAuth server, the Tasks extension, the spend allowance, and the skills served over MCP and as a plugin. It is done when a cloud session fixes something Footfall found. Phase 5 builds the live device wall and the report. It is done when the mockups' layouts are live. Later work is tracked as GitHub issues. That includes replaying a finding to check a fix, comparing versions, an MCP App and iOS on a Mac." src="images/diagrams/build-order-light.svg">
</picture>

1. **The sandbox and the tester.** The runner, the base images, the app container under gVisor, the egress proxy, the browser container, the six tools, the tester loop, and the demo app with its planted bugs. For development a command runs the testers against a local folder. A fake model replays scripted replies so that CI can test the loop with no API key. Tests show that a container cannot reach the internet, the metadata address or the Docker socket and that a tester cannot leave the app. Phase 1 also confirms that Chromium and WebKit run under gVisor. If Chromium's own sandbox cannot run there, gVisor and the container are the browsers' only wall. The threat table already assumes that. Memory is measured per persona. The phase is done when every isolation test passes in CI and on a droplet and the testers find most of the demo app's planted bugs.
2. **Runs.** The orchestrator, the queue and limits, scripted checks, findings, the merge, spend, the database schema, and a command to start a run and read its results. It is done when a run of the demo app gives a merged report with its costs within its limits.
3. **The droplet.** The Django web process, the compose stack, Caddy, `cloud-init`, the setup script, the first-run page and the GitHub App, sign-in by organisation membership, Settings, agent tokens and the audit log, and cloning from GitHub. It is done when a fresh droplet can be set up by following the documentation and a signed-in person can start a run of a real repository from a page.
4. **Agents.** The MCP endpoint on the 2026-07-28 protocol, with start and poll and the Tasks extension; the OAuth authorisation server; token checking; the allowance and approval for costly runs; the shape of the results; the skills, served over MCP and as a plugin; and the documentation for connecting agents. It is done when a Claude Code cloud session with Footfall as a connector and a local session have both tested a change to a real app and fixed something Footfall found.
5. **The device wall and the report.** The live page, the report page with its replay and charts, and generated screenshots for the documentation. It is done when the mockups' layouts are live in Footfall's own styling.
6. **Later.** Replaying a finding's recorded steps to check a fix; comparing two versions; building an app's own Dockerfile; iOS on a Mac worker. Later work is tracked as [GitHub issues labelled enhancement](https://github.com/Negroni-Venture-Studios/footfall/issues?q=label%3Aenhancement). [#2](https://github.com/Negroni-Venture-Studios/footfall/issues/2) is one of them and shows the live wall in Claude as an MCP App.

## Decided at the start

I decided these on or before 24 November 2026:

- Web apps and not native apps for now. iOS is a later goal and nothing is designed around it yet.
- A shared service behind an MCP endpoint, usable by agents on a laptop, a home server and in the cloud, hosted on a DigitalOcean droplet.
- Footfall clones the repository and runs the app itself, then fans out personas on different emulated devices.
- Where test data comes from is up to each app: fixtures, live public data, or fake data an agent generates.
- The tester is Footfall's own loop on the Anthropic API.
- A public MCP endpoint with authentication because cloud agents cannot join a private network.
- A public MIT repository under Negroni Venture Studios that others can deploy.
- GitHub sign-in through one GitHub App per instance. The App also gives read-only access to repositories.
- One instance per GitHub organisation or per developer on a personal account. Access follows membership of the organisation and can be narrowed to one team.
- Each person makes their own agent tokens.
- Settings live in the web pages and nobody needs the DigitalOcean console after setup.
- Setup that the deployer's own agent can walk them through.
- The name, Footfall.
- A live device wall, and a summary report modelled on mobile-agent-testers' write-up of a run.
- The Negroni palette with Snug's fonts, stepping back further than Snug does.
- Django for the web side, and Footfall's own runner for containers.
- The sandbox and the tester are built first.
- Each person sees runs only for the repositories they can read on GitHub.
- Each persona gets its own copy of the app and its seeded data.
- Apps with their own sign-in describe it in the manifest. Footfall signs each persona in with a throwaway account the seed step creates.
- The OAuth server ships in phase 4 so that cloud sessions can use Footfall as a connector. Bearer tokens stay for scripts.
- A run estimated above its token's or client's allowance needs a person's approval, through the client or on the run's page.
- The MCP endpoint speaks the 2026-07-28 protocol only.
- The live wall and the report stay web pages, built so they can become an MCP App later.
- Later work is tracked as GitHub issues.

## Calls made in this design

I settled these while writing the design and did not decide them at the start. They are listed so they can be revisited:

- This document lives at `docs/design.md`, as Snug's does.
- Organisation owners are Footfall's admins. Other members run tests and make tokens.
- A daily spend ceiling for each person as well as for the instance.
- The web process and the orchestrator are separate containers. Keys typed into Settings are write-only.
- gVisor for the browser container as well as the app container, with a fresh browser container for every run.
- An egress proxy with a per-run allow list. Package registries are reachable only during `install`.
- No app Dockerfiles in version 1, only Footfall's base images.
- Only refs in the installed repository and no pull requests from forks.
- `focus` and `mode` in `start_run`, and quick mode.
- Every tester's steps recorded in a form that can be replayed, from phase 1.
- Support for the MCP Tasks extension alongside start and poll, checked every 15 seconds.
- Skills served over MCP and published as a Claude Code plugin, from one set of files.
- A `request_key` on `start_run`, and findings returned a page at a time.
- A run's estimate is the sum of its personas' cost ceilings, with a default allowance of $5 and a day's wait for approval.
- Any OAuth client may ask to connect, with its metadata fetched through the public-address check, and an optional allow list.
- Screenshots returned as MCP image content.
- Devices kept in their own list, apart from personas.
- Twelve starting personas.
- No backups in version 1.
- The GitHub App created through the manifest flow from the first-run page.
- Membership re-checked at least hourly.
- Hosts in a manifest are trusted as their authors write them, limited to public host names and listed on each run's page.
- A one-time setup code protects the first-run page.

## Left to the builder

- Repository layout and module boundaries.
- The choice of OAuth library for the authorisation server.
- How Footfall asks GitHub which repositories a person can read, including while their agents use tokens and they are signed out. This fixes the GitHub App's permissions and must be settled before phase 3.
- How the prepared app is copied for each persona.
- The exact screenshot and element-list format, kept close to mobile-agent-testers' `screen.py`.
- The tester's system prompt, the starter briefs, the merge prompt, and the demo app's planted bugs.
- The personas themselves, within the coverage above.
- Queueing and scheduling within the configured limits.
- The base images and their runtimes.
- The format of the personas file.
- Page styling, within the layouts and branding above.
- The droplet size to recommend, from measured memory per persona.
