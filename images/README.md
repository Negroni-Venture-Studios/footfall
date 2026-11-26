# Container images

Footfall builds four images. They are tagged `dev` when built locally. Every base image is pinned by digest and Dependabot proposes new ones.

## footfall

`footfall` is Footfall's own image and is built from the repository root. The runner, the egress proxy and the orchestrator all start from it and Docker Compose runs them. It has a locked install without development dependencies, precompiled bytecode for a read-only root file system and an unprivileged user with uid 10001. It also has git from its Debian release. `footfall test` uses git to copy only the files git tracks.

## python3.13

`footfall-python3.13` is the base image for Python apps under test. The runner starts an app in it when the manifest asks for the Python runtime. The app's own install and run commands work as they would on a developer's machine. It runs as uid 10010 in `/workspace`. `pip` installs into `/workspace/.local` and the root file system stays read-only.

The image sets `PIP_USER=1` so that a plain `pip install` lands in `/workspace/.local`. pip inside a virtual environment refuses a user install. An `install` command that makes its own environment must turn the setting off for that pip. `python -m venv .venv && PIP_USER=0 .venv/bin/pip install -r requirements.txt` does that.

## node22

`footfall-node22` is the equivalent for Node apps. The runner starts it in the same way as uid 10010 in `/workspace`. The `npm` cache is in `/tmp/npm`.

In both images `/tmp` is a 256 MiB memory file system that holds no runnable files. Every container the runner starts has the same. The npm cache shares it and an install that downloads more than that runs out of space. A build step cannot run a program it has just written to `/tmp`. `/workspace` has neither limit beyond the run's disk budget.

## browsers

`footfall-browsers` serves [Playwright](https://playwright.dev/)'s `run-server`. That is a WebSocket endpoint that lets a client drive Chromium or WebKit in another container. The runner starts one for each run and the tester connects to it on port 3000. run-server listens on loopback alone behind `gate.js`. The gate passes on only a WebSocket upgrade for the run's own secret path and answers everything else with an empty 404. The image also installs a Chromium managed policy. The policy turns off Encrypted Client Hello because the egress proxy refuses it. It also turns off the features that call Google, such as autofill's server and Safe Browsing. The Playwright version in this image must equal the one in `uv.lock` because the server refuses a client of a different release. A test checks that they match.
