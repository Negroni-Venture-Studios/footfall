# Changelog

All notable changes to Footfall are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Footfall uses [semantic versioning](https://semver.org/).

## [Unreleased]

- `footfall test <folder>` runs the AI testers against a local folder. It copies only the files git would commit, so an ignored `.env` never reaches a container, and a folder outside git needs `--all-files`, which warns that it copies everything. It never reads or writes through a link, and it refuses a submodule or a nested repository, whose files git does not list. It asks the runner to prepare the app, start a copy for each persona and start the run's browsers, signs each persona in when the brief asks, then writes each persona's report, step trace and screenshots, a summary of the run and a table of the results. The run is stopped however it ends, and the throwaway password reaches only the seed step.
- `footfall runner` serves the runner, the one process that holds the Docker socket, on a Unix socket that only its own group can use.
- `footfall proxy` serves the egress proxy and its control socket.
- Each run's browser container puts a gate in front of Playwright's server, which listens on loopback alone. The gate passes on only a WebSocket upgrade for a path made fresh for that run, which only the orchestrator is told, and answers every other request with an empty 404, so code under test can neither learn the path nor launch a browser of its own there. Each connection has 10 seconds to send its request, and connections held open from one address cannot crowd out the orchestrator's.
- The browser image installs a Chromium managed policy that turns off Encrypted Client Hello, which the egress proxy refuses, and the run's Chromium starts as the full build, since the headless shell reads no policies.
- Every container the runner starts has an init process, and the browser container has 512 MiB of shared memory for Chromium.
- `deploy/dev/compose.yaml` runs the proxy, the runner and an orchestrator for development. Each is read-only, runs under an init process and has no capabilities beyond what it needs, only the runner mounts the Docker socket, and only the orchestrator receives the Anthropic key. The orchestrator mounts the repository under test read-only with its `.git`, and the footfall image carries git, so the copy is git's there too. With `FOOTFALL_RUNTIME=runc`, for a machine without gVisor, every command warns that containers start without it.
- Tiny Tasks, the demo app in `demo-app/`: a to-do list with twelve planted usability and accessibility bugs, listed in its `BUGS.md`, with a manifest and a brief for Footfall to test it.
