# Where gVisor differs from Linux

The isolation workflow runs Footfall's containers under [gVisor](https://gvisor.dev/), Google's user-space kernel, release 20261026, on GitHub's Ubuntu 24.04 runners with Docker 28.0.4. These are the places where a test found gVisor behaving differently from Linux, and what Footfall does about each. Every isolation run writes what it finds on each of them to the job's log as a `FOOTFALL-FACT` line, so these notes can be checked against any run; the figures here are from run 45656207833, on 25 November 2026. They belong in the plain-English `docs/security.md` for deployers that the design promises.

## Docker's name service does not answer

Inside a gVisor container on a run's network, looking up another container's name in Docker's name service fails with a temporary failure (`EAI_AGAIN`, which the log shows as -3), as `footfall-orchestrator` still does. Docker answers names at 127.0.0.11 through firewall rules in the container's network namespace, and gVisor's own network stack does not use them.

The browsers are given the egress proxy by its name, so the runner now writes the proxy's name and its address on the run's network into every container's hosts file (`ExtraHosts`, set in `runner/spec.py`). Nothing else in a run looks a container up by name.

## A fork bomb runs out of memory before it reaches the process limit

On Linux, a container with a limit of 256 processes is refused the next one with `EAGAIN`. Under gVisor the probe's processes were refused after 109 with `ENOMEM`, well below the limit, because each process in the sandbox also costs host memory, which the container's 512 MiB limit bounds. The test accepts either refusal, and still requires that the bomb stops below 256 and that the probe which set it off survives. Alasdair ruled on 25 November 2026 that the test passes on `ENOMEM`. Whether gVisor enforces the process limit itself is not shown, since memory runs out first.

## A browser cannot read /proc through file://

In the browser container, opening `file:///proc/version` gave an empty page in Chromium and crashed WebKit's page, where a test meant to read gVisor's kernel version from it. This was not checked against the same browsers on Linux. The test now reads the kernel release that full Chromium reports of the system it runs on, the operating system on `chrome://gpu`, and requires a release that names gVisor (release 20261026 reports `4.19.0-gvisor`) and is not the host's own. On Linux, `chrome://version` names only the system, and `navigator.userAgentData` gives an empty platform version. That still proves the property: both engines are children of the one run-server in the one container, behind the one gate, so the kernel Chromium reports is the kernel WebKit runs on too. The app containers' own check, which reads `/proc/version` and the kernel log from Python, is unchanged.

## The mount list leaves nodev off /tmp

`runner/spec.py` asks Docker for each container's `/tmp` with `rw,noexec,nosuid,nodev`. Inside an app container under gVisor, `/proc/self/mounts` lists it as `rw,nosuid,noexec,size=262144k`, without `nodev`. The test checks what matters instead. A script written to `/tmp` is refused with `EACCES`, while the same script runs from `/workspace`, and the mount says `noexec` and `nosuid`. With every capability dropped, no device file can be made in `/tmp` in any case.

## Facts found on the way

These are Docker's, not gVisor's, but the same run found them.

- dockerd 28 answers 500, not 404, when asked to disconnect a container that is not on a network, forced or not, and when the network has gone. The runner's engine counts that answer as done, by its message, and every other 500 is still an error.
- A container on an internal network could reach the host on the network's first address, where the host's side of the bridge sits, and with it everything the host serves on every address, such as SSH. The runner now makes each run's network with `com.docker.network.bridge.inhibit_ipv4`, so the host's side has no address.
