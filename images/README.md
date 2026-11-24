# Container images

Footfall builds four images, tagged `dev` when built locally. Each base image is pinned by digest, and Dependabot proposes new ones.

## footfall

`footfall` is Footfall's own image, built from the repository root. The runner, the egress proxy and the orchestrator all start from it, and Docker Compose runs them. It follows the layout of the Snug image. It has a locked install without development dependencies, precompiled bytecode for a read-only root file system, an unprivileged user with uid 10001, and git from its Debian release, which `footfall test` uses to copy only the files git would commit.

## python3.13

`footfall-python3.13` is the base image for Python apps under test. The runner starts an app in it when the manifest asks for the Python runtime, so the app's own install and run commands work as they would on a developer's machine. It runs as uid 10010 in `/workspace`, and `pip` installs into `/workspace/.local`, which lets the root file system stay read-only.

## node22

`footfall-node22` is the equivalent for Node apps. The runner starts it in the same way, as uid 10010 in `/workspace`, with the `npm` cache in `/tmp/npm`.

## browsers

`footfall-browsers` serves [Playwright](https://playwright.dev/)'s `run-server`, a WebSocket endpoint that lets a client drive Chromium or WebKit in another container. The runner starts one for each run, and the tester connects to it on port 3000. run-server listens on loopback alone, behind `gate.js`, which passes on only a WebSocket upgrade for the run's own secret path and answers everything else with an empty 404. The image also installs a Chromium managed policy that turns off Encrypted Client Hello, which the egress proxy refuses. The Playwright version in this image must equal the one in `uv.lock`, because the server refuses a client of a different release, and a test checks that they match.
