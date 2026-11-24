# Footfall

Footfall is a self-hosted service that tests web apps the way strangers would use them. A coding agent that has pushed a change asks Footfall to test it. Footfall runs the app in a sandbox and sends a dozen AI testers at it, each playing a different person on a different emulated phone, tablet or laptop. The agent gets structured findings back, and can fix what the testers found before a person looks.

Agents now build and rebuild small apps for one user, often with nobody reading the code. Unit tests check what the author expected. A tester playing a person on a particular device finds what nobody thought to check, such as a button hidden by the keyboard on a small phone, a chart cut off at large text, or an error that only says "Error". Footfall makes that check happen on every change, without anyone having to remember it.

Each app's repository holds a brief that says what the app is for and what each kind of person should try to do. The testers work against that brief, so it outlasts every rebuild of the code.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/diagrams/overview-dark.svg">
  <img alt="Agents and people reach Footfall through Caddy. The web process serves the pages, sign-in, the MCP endpoint and live updates. The orchestrator runs the queue, clones repositories, runs the testers and merges their findings; it alone talks to the Anthropic API and fetches code from GitHub. Both share the database and screenshots. Only the runner holds the Docker socket, and for each run it starts an app container for every persona and one browser container, all under gVisor with no secrets. The orchestrator drives the browsers through Playwright; the browsers reach the app; anything else either container fetches goes through the egress proxy, which allows only the hosts the run's manifest names." src="docs/images/diagrams/overview-light.svg">
</picture>

## Status

Footfall is at phase 1 of the [design](docs/design.md), which is the sandbox and the tester. Nothing here is ready to deploy. Phase 1 builds the runner, the gVisor containers, the egress proxy, the browsers, the tester's tools and loop, and a demo app with planted bugs, all driven from the command line. Later phases add runs, deployment, the MCP endpoint and the live device wall.

The design follows [Snug](https://github.com/Negroni-Venture-Studios/snug), Negroni Venture Studios' self-hosted data room, for security, deployment and documentation. Parts of the tester are adapted from [mobile-agent-testers](https://github.com/pierridotite/mobile-agent-testers), an MIT-licensed project that runs AI testers against native mobile apps.

## Trying phase 1

There is nothing to run yet. This section will be filled in when phase 1 has a command to try.

## Contributing and security

- [CONTRIBUTING.md](CONTRIBUTING.md) explains how to set up, test and propose changes.
- Please report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).
- Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).
- [AGENTS.md](AGENTS.md) holds the working rules for AI coding agents.

## Licence

Footfall is published by [Negroni Venture Studios](https://negroniventurestudios.com/) under the [MIT licence](LICENSE). The Footfall name identifies this project; if you ship a fork as your own product, please see [TRADEMARKS.md](TRADEMARKS.md). The third-party software and assets Footfall uses are listed in [THIRD_PARTY_LICENCES.md](THIRD_PARTY_LICENCES.md).
