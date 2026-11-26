# Where gVisor differs from Linux

The isolation workflow runs Footfall's containers under [gVisor](https://gvisor.dev/), Google's user-space kernel. It uses release 20261026 on GitHub's Ubuntu 24.04 runners with Docker 28.0.4. These are the places where a test found gVisor behaving differently from Linux and what Footfall does about them. Every isolation run writes what it finds on every difference below to the job's log as a `FOOTFALL-FACT` line. These notes can be checked against any run. The Docker facts at the end go to the job's summary instead. The figures here are from run 45656207833 on 25 November 2026. They belong in the plain-English `docs/security.md` for deployers that the design promises.

## Docker's name service does not answer

Inside a gVisor container on a run's network a lookup of another container's name in Docker's name service fails with a temporary failure. That is `EAI_AGAIN` and the log shows it as -3. A lookup of `footfall-orchestrator` still fails this way. Docker answers names at 127.0.0.11 through firewall rules in the container's network namespace. gVisor's own network stack does not use them.

The browsers are given the egress proxy by its name. The runner now writes the proxy's name and its address on the run's network into every container's hosts file. That is `ExtraHosts`, set in `runner/spec.py`. Nothing else in a run looks a container up by name.

## A fork bomb runs out of memory before it reaches the process limit

On Linux a container with a limit of 256 processes is refused the next one with `EAGAIN`. Under gVisor the probe's processes were refused after 109 with `ENOMEM`. That is well below the limit. Every process in the sandbox also costs host memory and the container's 512 MiB limit bounds that memory. The test accepts either refusal. It still requires the bomb to stop below 256 and the probe that set it off to survive. I decided on 25 November 2026 that the test passes on `ENOMEM`. Whether gVisor enforces the process limit itself is not shown because memory runs out first.

## A browser cannot read /proc through file://

A test meant to read gVisor's kernel version from `file:///proc/version` in the browser container. Opening it gave an empty page in Chromium and crashed WebKit's page. This was not checked against the same browsers on Linux. The test now reads the kernel release that full Chromium reports of the system it runs on. Chromium shows it as the operating system on `chrome://gpu`. The test requires a release that names gVisor and is not the host's own. Release 20261026 reports `4.19.0-gvisor`. On Linux `chrome://version` names only the system and `navigator.userAgentData` gives an empty platform version. That still proves the property. Both engines are children of the one run-server in the one container behind the one gate. The kernel Chromium reports is the kernel WebKit runs on too. The app containers' own check reads `/proc/version` and the kernel log from Python. It is unchanged.

## The mount list leaves nodev off /tmp

`runner/spec.py` asks Docker for every container's `/tmp` with `rw,noexec,nosuid,nodev`. Inside an app container under gVisor `/proc/self/mounts` lists it as `rw,nosuid,noexec,size=262144k` without `nodev`. The test checks what matters instead. A script written to `/tmp` is refused with `EACCES` while the same script runs from `/workspace`. The mount says `noexec` and `nosuid`. With every capability dropped, no device file can be made in `/tmp` in any case.

## Facts found on the way

These are Docker's facts and not gVisor's. The same run found them.

- dockerd 28 answers 500 and not 404 when asked to disconnect a container that is not on a network. It does so whether or not the disconnect is forced. It does the same when the network has gone. The runner's engine reads the message and counts that answer as done. Every other 500 is still an error.
- A container on an internal network could reach the host on the network's first address. The host's side of the bridge sits there. Through it the container could reach everything the host serves on every address, such as SSH. The runner now makes every run's network with `com.docker.network.bridge.inhibit_ipv4` and the host's side has no address.
