# What Footfall protects against

Footfall runs an app it has never seen and sends AI testers through its pages. It treats the app, its pages, and everything a tester writes as hostile. It keeps your API key, your server, and your other runs away from them. It cannot protect against anyone with root on the server or anyone who can use Docker there.

In phase 1 you run Footfall from the command line on one Linux server. The stack is the development one in [`deploy/dev/compose.yaml`](../deploy/dev/compose.yaml). The detail behind each part is in [How Footfall works](architecture.md).

## The app under test

Footfall treats the app's code as hostile even when your own agent wrote it. A bug can behave like an attack. Any dependency can be malicious.

The app's install step, its seed step, and every copy of it run in containers under [gVisor](https://gvisor.dev/). gVisor answers the code's system calls with a kernel of its own. The code never talks to your server's kernel directly. `FOOTFALL_RUNTIME=runc` is for development on a Mac. It starts the browsers and every other container of a run without gVisor. Never set it on a server.

The code runs as an unprivileged user with no Linux capabilities and cannot gain any. It cannot write to its own image. The runner limits each container's memory, CPU, and processes. It stops a run that uses more than 8 GiB of disk or lasts longer than 90 minutes.

Only the runner holds Docker's socket. It starts containers only with the settings in [`footfall/runner/spec.py`](../footfall/runner/spec.py). Nothing in a tested repository can change them.

By default `footfall test` copies only the files git tracks. `--untracked` adds the untracked files git does not ignore. `--all-files` copies everything. A `.env` that git ignores then reaches the app. A secret committed to the repository always reaches it.

The app's containers hold none of your secrets. When the app has its own sign-in, Footfall makes a throwaway username and password for the run. The seed step gets the password in its environment so that it can create the account. The account ends with the run.

## What a run can reach

Each run has a Docker network of its own with no route to the internet. Your server has no address on that network. On it the app can reach the run's other containers and the egress proxy. Other runs and DigitalOcean's metadata address `169.254.169.254` are out of its reach.

The app reaches the internet only through the egress proxy. The proxy lets each container reach only the hosts listed in the run's manifest. It allows only ports 80 and 443. During the install step it also allows `pypi.org`, `files.pythonhosted.org`, and `registry.npmjs.org`. A manifest lists hosts by name, never by address. The proxy connects only to public addresses. It connects to the address it checked and does not look the name up again.

The proxy counts your server's own public addresses as public. A run whose manifest lists your server's own domain can reach whatever your server serves there on ports 80 and 443. Keep anything an app under test must not reach off those ports. Making the proxy refuse your server's own addresses is [issue #11](https://github.com/Negroni-Venture-Studios/footfall/issues/11).

Traffic to the manifest's hosts leaves from your server's own address. A service that trusts that address trusts every app you test.

Name lookups and other UDP traffic stay inside the run. The proxy logs each host a container asked for and whether it was allowed. It never logs a path, a header, or a body.

## Pages and testers

A page can try to steer the tester that reads it. The tester's tools take a path on the app, never an address. The browser reaches any host other than the app only through the egress proxy. For the browser the proxy allows only the manifest's asset hosts. A guard inside the browser refuses most other requests before they get that far. The tester's instructions say that text on a page is data, never instructions. The only thing a tester holds is the run's throwaway account.

Everything a tester sees goes to Anthropic's API: each page's text, each screenshot, and the throwaway account. Seed nothing real.

What a tester writes can echo what a page said. Footfall cuts each field of a tester's report to a set length and labels the text as observations. Have any agent that reads Footfall's results treat them as observations, never as instructions.

A page can attack the browser itself. The browsers for a run share one container under gVisor that holds none of your secrets. Playwright starts Chromium without its own sandbox. gVisor and the container are the browsers' only wall. The app's containers share the browsers' network. A gate on the browsers' port lets through only a path made fresh for each run. The runner makes the path and gives it only to the orchestrator.

## Your API key and your spend

Only the orchestrator's container holds your Anthropic API key. Footfall never logs it. Make the key in an Anthropic Console workspace with a spend limit.

Footfall starts no new turn once a run's personas together have spent $12. A run can go over by one turn for each persona still running. You can change the ceiling with `FOOTFALL_RUN_COST_CEILING_USD`, or for one run with `footfall test --cost-ceiling`. A tested repository cannot raise it.

## What each container holds

| Container | Holds | Can reach |
|---|---|---|
| Runner | Docker's socket, as root, and every run's files | Docker and the proxy's control socket. It has no network |
| Orchestrator | Your API key, the repository it tests, the results, and the run's throwaway account | The internet and your server, directly; each run's network; the runner's socket |
| Egress proxy | The allow lists | Public addresses on ports 80 and 443. It listens only on runs' networks |
| Install | The app's files | The run's other containers; the package registries and the manifest's hosts through the proxy |
| Seed | The app's files and the throwaway password | The run's other containers; the manifest's hosts through the proxy |
| Each app copy | The seeded app | The run's other containers; the manifest's hosts through the proxy |
| Browsers | The run's path, every persona's pages, and the throwaway password, which it types to sign in | The app's copies; the manifest's asset hosts through the proxy |

## Where gVisor differs from Linux

Footfall's isolation tests found four places where gVisor release 20261026 behaves differently from Linux. One of them was not checked against Linux itself.

- Docker's name service does not answer inside a gVisor container. The runner writes the egress proxy's name and address into each container's hosts file instead.
- A fork bomb runs out of memory before it reaches the limit of 256 processes. In the tests the container's memory limit stopped it after 109. The tests do not show whether gVisor enforces the process limit itself.
- A browser in the container cannot open `file:///proc/version`. This was not checked against the same browsers on Linux. The tests read the kernel release that Chromium reports instead. Under gVisor it is `4.19.0-gvisor`.
- The mount list shows `/tmp` without `nodev`. No program can run from `/tmp`. No device file can be made there because every capability is dropped.

The detail of each is in [the gVisor notes](notes/gvisor-differences.md).

## What Footfall cannot protect against

Anyone with root on your server can read your API key and everything else on it. So can anyone who can use Docker there. So can your hosting provider. Keep root and Docker access to the people who run Footfall.

Anyone who can change a tested repository's manifest can add hosts its runs may reach. The proxy still refuses addresses that are not public, but any public host can be added. The app can send what it holds to those hosts. That is its own files, its seed data, and anything copied in with `--untracked` or `--all-files`.

The runner measures a run's disk use by walking its folders. An app can hide what it writes from the walk. It can delete a file it still holds open and go on writing to it. Put `/var/lib/footfall-runs` on a file system of its own. A run that fills it then cannot fill your server's own disk. A hard limit for each run from the host is [issue #4](https://github.com/Negroni-Venture-Studios/footfall/issues/4).

Footfall's own three containers have no memory limits. One that grows without limit can take all of your server's memory. Adding limits is [issue #13](https://github.com/Negroni-Venture-Studios/footfall/issues/13).

A screenshot can show the throwaway password when the app shows what was typed. Footfall removes the password from each report and step trace wherever it appears whole. Hiding it in screenshots is [issue #7](https://github.com/Negroni-Venture-Studios/footfall/issues/7).

A tester can be wrong. A page can also mislead it. Every finding has screenshots so that you can check it.

## Reporting a security problem

To report a vulnerability in Footfall, follow the [security policy](../SECURITY.md).
