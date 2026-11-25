# Where gVisor differs from Linux

The isolation workflow runs Footfall's containers under [gVisor](https://gvisor.dev/), Google's user-space kernel, release 20261026, on GitHub's Ubuntu 24.04 runners with Docker 28.0.4. These are the places where a test found gVisor behaving differently from Linux, and what Footfall does about each. Task 18 folds them into `docs/security.md`.

## Docker's name service does not answer

Inside a gVisor container on a run's network, looking up another container's name fails with a temporary failure (`EAI_AGAIN`), for `footfall-proxy` and `footfall-orchestrator` alike. Docker answers names at 127.0.0.11 through firewall rules in the container's network namespace, and gVisor's own network stack does not use them.

The browsers are given the egress proxy by its name, so the runner now writes the proxy's name and its address on the run's network into every container's hosts file (`ExtraHosts`, set in `runner/spec.py`). Nothing else in a run looks a container up by name.

## A fork bomb runs out of memory before it reaches the process limit

On Linux, a container with a limit of 256 processes is refused the next one with `EAGAIN`. Under gVisor the probe's processes were refused at about 110 with `ENOMEM`, well below the limit, because each process in the sandbox also costs host memory, which the container's 512 MiB limit bounds. The test accepts either refusal, and still requires that the bomb stops below 256 and that the probe which set it off survives.

## Facts found on the way

These are Docker's, not gVisor's, but the same run found them.

- dockerd 28 answers 500, not 404, when asked to disconnect a container that is not on a network, forced or not, and when the network has gone. The runner's engine counts that answer as done, by its message, and every other 500 is still an error.
- A container on an internal network could reach the host on the network's first address, where the host's side of the bridge sits, and with it everything the host serves on every address, such as SSH. The runner now makes each run's network with `com.docker.network.bridge.inhibit_ipv4`, so the host's side has no address.
