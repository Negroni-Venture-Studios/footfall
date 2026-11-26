# The browser image: Playwright's run-server, which the tester connects to.
# The tag, the npm install and uv.lock must name the same Playwright release,
# because run-server refuses a client of another version and the npm package
# must match the browsers baked into the image. tests/test_images.py checks it.

FROM mcr.microsoft.com/playwright:v1.65.1-noble@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579

# No install scripts: the base image has the browsers already, and nothing fetched here runs.
RUN npm install --global --ignore-scripts playwright@1.65.1

# The gate in front of run-server, which the runner starts with each run's path.
COPY images/browsers/gate.js /opt/footfall/gate.js
# Chromium sends a GREASE Encrypted Client Hello, which the egress proxy refuses, and only this
# policy turns it off; the command-line switch no longer does. The policy also turns off what
# would have Chromium call Google, such as autofill's server and component downloads, which the
# proxy refuses too.
# Each path is where one build of Chromium for Linux reads managed policies: Chromium's own,
# Chrome's, and Chrome for Testing's.
COPY images/browsers/chromium-policy.json /etc/chromium/policies/managed/footfall.json
COPY images/browsers/chromium-policy.json /etc/opt/chrome/policies/managed/footfall.json
COPY images/browsers/chromium-policy.json /etc/opt/chrome_for_testing/policies/managed/footfall.json

USER pwuser
EXPOSE 3000
# The runner always gives the whole command: the gate, with the run's own path, in front of
# run-server on loopback. Started without it, the image says so and serves nothing, rather than
# open a server for anyone who reaches it.
CMD ["node", "-e", "console.error('footfall-browsers: the runner gives this container its command, with the path of its run; on its own it serves nothing'); process.exit(64)"]
