# Security policy

Footfall runs other people's code in containers and sends what it sees to a model, so security reports come before all other work.

## Reporting a vulnerability

**Please do not open a public issue.** Report privately, in either of these ways:

1. **GitHub private vulnerability reporting** (preferred). Go to the [Security tab](https://github.com/Negroni-Venture-Studios/footfall/security/advisories/new) and choose "Report a vulnerability".
2. **Email** alasdair@negroniventurestudios.com.

Please include:

- what the vulnerability is and what an attacker could do with it;
- the steps to reproduce it, against a local instance if you can;
- the Footfall version or commit.

You will get a response within 72 hours. We will work with you on a fix and agree the timing of any public disclosure. We are happy to credit you in the advisory.

## Scope

In scope:

- Footfall's own code: the runner and its container settings, the egress proxy, the tester's tools and the guard that keeps the browser on the app, how a run's results reach a calling agent, and anything that handles an API key, a token or a password.
- The deployment files we ship: Footfall's container images, the Compose files, and the scripts that set a server up.

Out of scope:

- The apps Footfall tests. A bug in an app under test is what Footfall exists to find, and an app's own weaknesses are its author's to fix.
- Weaknesses in an operator's own infrastructure or configuration that Footfall's documentation already warns against.

## Supported versions

Security fixes go into the next release, and only the latest release is supported. Footfall has no release yet.

## How changes are checked

Every change to `main`, an administrator's included, goes through a pull request that must pass CI. CI runs:

- ruff's security rules;
- strict type checking;
- the test suite, with no network access;
- gitleaks over the full git history;
- pip-audit against known vulnerabilities;
- a licence check;
- a CycloneDX software bill of materials;
- a build of the four container images;
- CodeQL analysis of the Python code and the workflows;
- the isolation tests, which start the development stack with gVisor and check what an app container and the browsers can reach.

Third-party GitHub Actions are pinned to commit SHAs and kept current by Dependabot. GitHub secret scanning with push protection is enabled on the repository.
