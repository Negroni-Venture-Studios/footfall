# Third-party licences

Footfall is MIT licensed (see [LICENSE](LICENSE)). It includes, adapts or installs the third-party software and assets below. Every licence is compatible with distributing Footfall as MIT, and `scripts/check_licences.py` checks every installed Python package in CI.

The full licence texts ship with each package, in its `*.dist-info/licenses` folder.

## Adapted from another project

[mobile-agent-testers](https://github.com/pierridotite/mobile-agent-testers) runs AI testers against native mobile apps. It is MIT licensed, and Footfall adapts its screen format, its accessibility check, its rules and its personas for web apps. Its notice is kept here, as the licence requires:

> MIT License
>
> Copyright (c) 2026 Pierre Raffalli
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Python packages Footfall runs on

| Package | Licence | Used for |
|---|---|---|
| anthropic | Apache-2.0 | The Anthropic API client, which drives the testers |
| playwright | Apache-2.0 | Driving the browsers |
| greenlet (Playwright's) | MIT and PSF-2.0 | Playwright's synchronous calls |
| anyio, idna, sniffio, typing-extensions | MIT, BSD-3-Clause, Apache-2.0 or MIT, PSF-2.0 | The Anthropic client's async and typing support |
| httpx2, httpcore2 (the Anthropic client's HTTP stack, with truststore) | BSD-3-Clause (truststore MIT) | HTTP requests to the API |
| pydantic (with pydantic-core, annotated-types and typing-inspection) | MIT | The Anthropic client's data models |
| docstring-parser, jiter | MIT | The Anthropic client's tool definitions and JSON parsing |
| opentelemetry-api | Apache-2.0 | The Anthropic client's tracing hooks |

## Fonts

| Asset | Licence | Where |
|---|---|---|
| Geist and Geist Mono, by Vercel | SIL Open Font Licence 1.1 | `docs/diagrams/fonts/`, with `OFL.txt` |
