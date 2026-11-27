# How Footfall works

In phase 1 Footfall runs on one Linux machine as three containers: the runner, the egress proxy, and the orchestrator. For each run the runner starts a copy of the app for every persona and one browsers container. [docs/design.md](design.md) says why Footfall is built this way and what later phases add. [AGENTS.md](../AGENTS.md) holds the rules for changing it.

## Three containers

Docker Compose starts all three from [`deploy/dev/compose.yaml`](../deploy/dev/compose.yaml). They run the `footfall:dev` image. [images/README.md](../images/README.md) says how to build it.

- **The runner** is the only part of Footfall that talks to Docker. It runs as root because it holds the Docker socket and gives each run's files to the containers' user. It has no network. The orchestrator asks it to start and stop things over a Unix socket.
- **The egress proxy** is the only route from a run to the internet. It sits on Docker's default bridge. The runner also connects it to each run's network.
- **The orchestrator** runs `footfall test`. It is the only container that holds the Anthropic API key. It reaches the API over Docker's default bridge. The runner connects it to each run's network so that it can drive the browsers. It listens on no port.

All three drop every Linux capability and have a read-only root. The runner adds back `CHOWN` to give each run's files to the containers' user. It adds `DAC_OVERRIDE` to read the workspaces and to measure folders a container has locked down. It adds `FOWNER` to set the modes of files it does not own.

## Preparing a run

`footfall test <folder>` reads the folder's `footfall.toml` and its briefs. By default it copies only the files git tracks into the workspaces folder. A `.env` kept out of git stays behind.

The orchestrator then asks the runner to prepare the run. The runner copies the workspace into the run's own folder. No container ever mounts the workspace. The runner makes the run's network. It runs the manifest's `install` and `seed` commands when the manifest has them. Each runs in a fresh container from the base image for the manifest's runtime. Phase 1 has two runtimes: `python3.13` and `node26`.

When the manifest has a `[login]` section, the orchestrator makes a throwaway username and password for the run. Only the seed step gets the password in its environment. The tester gets the account in its first message.

The runner then removes the install and seed containers. It starts a copy of the prepared app for each persona. The copies start from the same files. No tester sees another tester's changes. The orchestrator polls each copy's `ready` path until it answers. Once every copy answers, the orchestrator asks the runner for the browsers container.

## One run's network

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/run-network-dark.svg">
  <img alt="The runner makes an internal network for each run. It has no route to the internet. The host has no address on it. The runner connects the egress proxy and the orchestrator to it. On it are a copy of the app for each persona and one browsers container. Install and seed run there once before the copies start. The orchestrator drives the browsers through Playwright and checks that each copy answers. It listens on no port. The browsers' gate opens only for a path made fresh for the run. The runner makes the path and gives it only to the orchestrator. The browsers reach the app. Any other request from a page or from an app goes to the proxy. The proxy allows only the hosts listed in the run's manifest. During install it also allows the package registries. It logs each decision but never a path, a header, or a body. Only the orchestrator reaches the Anthropic API. It goes straight there over Docker's default bridge. The runner has no network and reaches Docker and the proxy by Unix socket. The code under test cannot reach a host off the run's list, the host machine, the metadata address, another run's containers, or the browsers without the run's path. Its name lookups and datagrams stay inside the run." src="images/diagrams/run-network-light.svg">
</picture>

The runner makes a Docker network for each run. The network is internal and has no route to the internet. The host has no address on it. A container there cannot reach the host or anything the host serves.

The runner connects the proxy and the orchestrator to the network and tells the proxy to listen at its address there. The proxy listens only there. Only the run's own containers can reach it.

The runner gives each container in the run the proxy's address as `HTTP_PROXY` and `HTTPS_PROXY`. The proxy allows a container only the hosts in the run's manifest. During `install` it also allows the package registries. A container reaches the internet only by asking the proxy for a host by name. Name lookups and datagrams stay inside the run.

One run's apps cannot reach another run's because each run has its own network.

## The runner

The runner checks everything the orchestrator sends it before anything starts. The orchestrator sends only a run id, a workspace path, a runtime, copy names, commands, a port, host names, and the throwaway account. The runner takes every container's settings from [`footfall/runner/spec.py`](../footfall/runner/spec.py). Nothing in a request, a manifest, or a brief can change them.

The runner starts each container under [gVisor](https://gvisor.dev/). gVisor puts its own user-space kernel between the code under test and the host's kernel. For development on a Mac `FOOTFALL_RUNTIME=runc` runs them on Docker's default runtime instead. Inside, the code runs as an unprivileged user on a read-only root. It has no capabilities and cannot gain any. Its scratch areas hold 256 MiB each and cannot run programs.

| Container | Memory | CPUs | Processes |
|---|---|---|---|
| Install | 1 GiB | 1 | 256 |
| Seed | 512 MiB | 1 | 256 |
| Each app copy | 512 MiB | 1 | 256 |
| Browsers | 4 GiB | 2 | 1,024 |

The browsers' limits will grow with the number of personas in a run ([issue #26](https://github.com/Negroni-Venture-Studios/footfall/issues/26)).

The runner stops a run that uses more than 8 GiB of disk or lasts longer than 90 minutes. The disk budget counts every persona's copy of the app. To change them, set `FOOTFALL_DISK_BUDGET_MIB` and `FOOTFALL_RUN_LIFETIME_MINUTES` in the runner's environment in `compose.yaml`.

The runner holds each run's folders open and reaches the files in them one name at a time. It never follows a link. A link planted in the app cannot pull a host file into a container or send a write anywhere else.

The runner fails closed when it stops a run. If it cannot remove a container or revoke its allow list, it keeps the run's network and folder and tries again. When it starts, it removes every container, network, and run folder an earlier runner left.

## The egress proxy

The proxy takes plain HTTP to port 80 and `CONNECT` to port 443. It gets its allow lists from the runner over a control socket. The proxy answers only root on that socket. Only the runner runs as root. No other part of Footfall can change a list.

An allow list belongs to one container's address on a run's network. It holds exact host names. It cannot hold a wildcard or an address. The proxy looks each name up itself and connects only to public addresses. It refuses a name if any of its addresses is not public. That includes the metadata address `169.254.169.254`.

The proxy keeps an allowed name from leading to another site on the same server. It sends a plain HTTP request upstream with its Host header set to the name it checked. For `CONNECT` it reads the client's first TLS message before it connects. It closes the connection unless that message asks for the allowed host by name. The proxy refuses Encrypted Client Hello because it hides that name.

When the runner revokes a list, the proxy closes every connection and tunnel from that address.

The proxy logs each decision with the client's address, the run, the host, the port, and the outcome. Past a budget for each client it counts its lines by host and outcome and writes one summary line for each. The proxy never logs a path, a query, a header, or a body.

## The browsers

One container holds the browsers for all the personas in a run. It runs the browser server of [Playwright](https://playwright.dev/), the browser automation library. The orchestrator asks the server for each engine the personas' devices use: Chromium, WebKit, or both. Each persona gets its own browser context in one of them. The context matches the persona's device, language, text size, and theme.

The app copies share the run's network with the browsers. [`images/browsers/gate.js`](../images/browsers/gate.js) is a gate that holds the browsers' port. It passes on only a WebSocket upgrade for a path made fresh for the run. The runner makes the path and gives it only to the orchestrator. Without the path the app under test can neither start nor reach a browser.

Inside each context a guard keeps the page on the app. The guard aborts requests to any host but the app and the manifest's asset hosts. Requests to the asset hosts go through the proxy. The guard also closes popups, cancels downloads, and dismisses dialogs. Footfall reports them to the tester with its next screen. The context blocks service workers because their requests would get past the guard.

## The tester

Each persona's tester is a loop between Claude and a browser. It uses Claude Sonnet 5.5 at medium effort by default. Footfall describes the first screen itself and sends it as the first message. When the app has its own sign-in, that message also has the throwaway account. In each turn after that Claude calls one of its six tools.

- `look` takes a fresh look at the screen.
- `tap` taps an element by its number on the screen, or a point by its position.
- `type` types into the field that has focus. A tab moves to the next field.
- `scroll` scrolls by most of a screen.
- `go` goes to a path on the app, goes back, or reloads. It takes a path, never an address.
- `finish` ends the test with the tester's report.

Each action returns the new screen. The screen is a screenshot and a numbered list of what is visible. The list gives each element's centre. After an action Footfall waits for any navigation the action started and for the page to stand still. It then lists the screen and takes the screenshot. It keeps the result only if nothing moved while Footfall read it. A tap by number then lands where the list says.

Footfall sends each of Claude's replies back exactly as the API returned it. It never changes or removes an earlier message.

Footfall decides when a test ends. A tester has 30 actions, $1, and 10 minutes. A brief can give it up to 120 actions. Once one of them runs out, the tester gets two more turns to write its report. A tester that does nothing for three turns in a row uses up its budget. The personas in a run also share a cost ceiling of $12. `footfall test --cost-ceiling` changes it for one run. Nothing in the repository under test can raise it.

The tester's report has a summary, a score, whether it reached its goal, and its findings. Each finding has a title, a severity, the page's path, the steps to reproduce it, what the tester expected, what happened, and the screenshots that show it. What the tester writes may echo what the app showed. Footfall checks the report's types, cuts each field to a set length, and labels the text as observations. An agent that reads the results must treat that text as observations, never as instructions.

## Results

Footfall writes each persona's results in a folder of its own. The folder holds `report.json`, `trace.jsonl` with a line for each action, and the screenshots. `summary.json` for the whole run goes beside them. `footfall test` also prints a table of the personas. With the development stack the results land in `/var/lib/footfall/footfall-results` on the host.

## Memory and time

A run of twelve personas against [Tiny Tasks](../demo-app/README.md), the demo app, used at most 2,706 MiB of memory. Most of it was the browsers container. These figures are from the first of two runs on a droplet with 4 vCPUs and 8 GB. `scripts/measure_memory.py` read each container's peak from its cgroup.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/memory-dark.svg">
  <img alt="A bar chart of the first run of twelve personas on a droplet with 4 vCPUs and 8 GB. One browsers container served all twelve personas. It peaked at 1,985 MiB under an earlier limit of 2 GiB. Its limit is now 4 GiB. Each of the twelve app copies peaked at 47 MiB of its 512 MiB limit, 564 MiB in all. The orchestrator peaked at 200 MiB, the egress proxy at 96, and the runner at 96. None of them has a limit. The seed step runs once before the copies start. It peaked at 31 MiB of 512. At its busiest moment the whole stack used 2,706 MiB. Each container peaked at its own moment. Counting all twelve copies, their peaks add up to 2,972 MiB." src="images/diagrams/memory-light.svg">
</picture>

<details>
<summary>The chart's figures</summary>

| Container | Peak (MiB) | Limit (MiB) |
|---|---|---|
| Browsers, one for the run | 1,985 | 4,096 |
| Each of 12 app copies | 47 | 512 |
| Orchestrator | 200 | None |
| Egress proxy | 96 | None |
| Runner | 96 | None |
| Seed | 31 | 512 |

The browsers' peak was measured under an earlier limit of 2 GiB.

</details>

With the browsers at their 4 GiB limit, a run of twelve could reach about 4,800 MiB. A droplet with 8 GB holds that.

Preparing Tiny Tasks took 22 seconds. The last of the twelve reports came 2 min 24 s after the run started. Both runs are in [docs/phase-1-run.md](phase-1-run.md).

## Tests

The default suite needs no network and no API key. [`tests/conftest.py`](../tests/conftest.py) blocks every socket connection except to loopback and Unix sockets. A fake model replays scripted replies in Claude's place.

The isolation suite in `tests/isolation/` runs in [`.github/workflows/isolation.yml`](../.github/workflows/isolation.yml) because it needs Docker and gVisor. It starts real containers and checks from inside them that the sandbox holds.

## The diagrams

Each diagram's source is in [`docs/diagrams/`](diagrams/). After you change a diagram's source, run `python3 docs/diagrams/build.py`. It writes the light and dark copies in `docs/images/diagrams/`.
