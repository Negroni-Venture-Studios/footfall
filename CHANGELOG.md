# Changelog

All notable changes to Footfall are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Footfall uses [semantic versioning](https://semver.org/).

## [Unreleased]

- `footfall test <folder>` runs the AI testers against a local folder. It asks the runner to prepare the app, start a copy for each persona and start the run's browsers, signs each persona in when the brief asks, then writes each persona's report, step trace and screenshots, a summary of the run and a table of the results. The run is stopped however it ends, and the throwaway password reaches only the seed step.
- `footfall runner` serves the runner, the one process that holds the Docker socket, on a Unix socket that only its own group can use.
- `footfall proxy` serves the egress proxy and its control socket.
- Each run's browser server answers only on a path made fresh for that run, which only the orchestrator is told, so code under test cannot launch a browser of its own there.
- `deploy/dev/compose.yaml` runs the proxy, the runner and an orchestrator for development. Each is read-only and has no capabilities beyond what it needs, only the runner mounts the Docker socket, and only the orchestrator receives the Anthropic key. With `FOOTFALL_RUNTIME=runc`, for a machine without gVisor, every command warns that containers start without it.
- Tiny Tasks, the demo app in `demo-app/`: a to-do list with twelve planted usability and accessibility bugs, listed in its `BUGS.md`, with a manifest and a brief for Footfall to test it.
