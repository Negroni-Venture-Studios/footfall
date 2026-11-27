# Changelog

All notable changes to Footfall are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Footfall uses [semantic versioning](https://semver.org/).

## [Unreleased]

### Added

- `footfall test <folder>` sends the AI testers at an app in a folder on your machine. Each tester writes a report with its screenshots and the steps it took. Footfall writes a summary of the run and prints a table of the results.
- `footfall/data/personas.toml` holds twelve starting personas. Each has a device, a language, a text size, and other settings. Its id says what it tests, such as `largest-text` or `right-to-left`.
- Tiny Tasks is a small to-do app in `demo-app/`. It has twelve usability and accessibility bugs planted for the testers to find.
- An app's `footfall.toml` says how to install, seed, and start it, which pages to test, and which hosts it may reach. Its briefs say what the app is for and what each kind of person should try. Footfall reports all the problems it finds in them at once.
- `footfall runner` starts and stops the containers for each run. It is the only part of Footfall that can talk to Docker.
- `footfall proxy` is the egress proxy. A run's containers reach the internet only through it. They reach only the hosts listed in the app's manifest.
- Whoever runs Footfall has three settings: `FOOTFALL_DISK_BUDGET_MIB` (8 GiB by default), `FOOTFALL_RUN_LIFETIME_MINUTES` (90), and `FOOTFALL_RUN_COST_CEILING_USD` ($12). `footfall test --cost-ceiling` sets the last one for a single run. The tested repository cannot raise any of them. Footfall stops on a value it cannot use rather than fall back to the default.
- `deploy/dev/compose.yaml` runs the development stack.
- The isolation workflow runs the whole stack under gVisor on a GitHub-hosted runner for each pull request. It checks what the app and browser containers can and cannot reach.
- `docs/architecture.md` says how the runner, the egress proxy, the browsers, and the tester work. It has a diagram of one run's network and a chart of the memory each container used.

### Security

- Each app and its browsers run in their own containers under gVisor. A container has no capabilities, a read-only root, limits on memory, processes, and disk, and a network of its own.
- Nothing secret goes into a container except a password Footfall makes up for each run. The step that sets up the app's data creates an account with it. The testers use it to sign in.
- `footfall test` copies only the files git tracks unless you ask for more. `--untracked` adds the untracked files git does not ignore and names each one first. `--all-files` copies everything and is needed outside git.
- `footfall test` refuses a checkout in which a branch could pass off one of your own files as part of the app. That covers sparse checkouts and changed tracked files that match your own git excludes. The refusal gives the file's name and says what to do.
- `footfall test` never follows a link and never copies a git folder. It writes results only into folders that belong to you. Only you can read them.
- The egress proxy checks each connection. It allows public addresses only, port 80 for plain HTTP and port 443 for TLS, one request per connection, and a TLS server name that matches the host. It refuses Encrypted Client Hello. The browsers have Encrypted Client Hello and their own calls to Google turned off.
- Text a tester writes is cleaned, capped, stripped of the run's password, and labelled as observations before it leaves Footfall. An agent reading a report must never take it as instructions.
- In GitHub Actions Footfall masks each secret it makes. The job's log shows `***` in its place.
