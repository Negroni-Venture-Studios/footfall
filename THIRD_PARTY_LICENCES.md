# Third-party licences

Footfall is MIT licensed and [LICENSE](LICENSE) has the text. It includes, adapts or installs the third-party software and assets below. Every licence is compatible with distributing Footfall as MIT. `scripts/check_licences.py` checks every installed Python package in CI.

The full licence texts ship with every package in its `*.dist-info/licenses` folder.

## Python packages Footfall runs on

These packages use the Apache-2.0, BSD-3-Clause, MIT and PSF-2.0 licences. All four are permissive and compatible with distributing Footfall under MIT. For each pull request CI runs `scripts/check_licences.py` over every installed package including the development tools. It fails on any licence it does not know to be compatible.

| Package | Licence | Used for |
|---|---|---|
| anthropic | Apache-2.0 | The Anthropic API client, which drives the testers |
| playwright | Apache-2.0 | Driving the browsers |
| greenlet | MIT and PSF-2.0 | Playwright's synchronous calls |
| anyio | MIT | The Anthropic client's async support |
| sniffio | Apache-2.0 or MIT | Telling the Anthropic client which async library runs it |
| typing-extensions | PSF-2.0 | Newer typing features for the Anthropic client and pydantic |
| httpx2 | BSD-3-Clause | The Anthropic client's HTTP requests |
| httpcore2 | BSD-3-Clause | The connections under httpx2 |
| h11 | MIT | HTTP/1.1 for httpcore2 |
| idna | BSD-3-Clause | Internationalised host names for httpx2 |
| truststore | MIT | The system's certificate store, for httpx2's TLS |
| pydantic | MIT | The Anthropic client's data models |
| pydantic-core | MIT | pydantic's validation |
| annotated-types | MIT | Constraints in pydantic's types |
| typing-inspection | MIT | pydantic's reading of type annotations |
| docstring-parser | MIT | The Anthropic client's tool definitions |
| jiter | MIT | The Anthropic client's JSON parsing |
| opentelemetry-api | Apache-2.0 | The Anthropic client's tracing hooks |

## Fonts

| Asset | Licence | Where |
|---|---|---|
| Geist and Geist Mono, by Vercel | SIL Open Font Licence 1.1 | `docs/diagrams/fonts/`, with `OFL.txt` |
