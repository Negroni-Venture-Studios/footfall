# The browser image: Playwright's run-server, which the tester connects to.
# The tag, the npm install and uv.lock must name the same Playwright release,
# because run-server refuses a client of another version and the npm package
# must match the browsers baked into the image. tests/test_images.py checks it.

FROM mcr.microsoft.com/playwright:v1.65.1-noble@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579

RUN npm install --global playwright@1.65.1

USER pwuser
EXPOSE 3000
CMD ["playwright", "run-server", "--port", "3000", "--host", "0.0.0.0"]
