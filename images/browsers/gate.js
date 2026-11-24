#!/usr/bin/env node
// Footfall's gate in front of Playwright's run-server, in the browser container.
//
// The container listens on the run's network, which the app containers under test share.
// run-server launches a browser for any client that reaches it, and it gives its secret
// path to anyone who asks `GET /json`. So the container's port is this gate's. It passes on
// only a WebSocket upgrade whose request target is exactly the run's path, compared in
// constant time, to run-server on 127.0.0.1, byte for byte. Every other request gets an
// empty 404. run-server checks the path as well.
//
// The gate starts run-server as its child, and exits when the child does. It never prints
// the path, and the child's own output, which names the path, is discarded.
//
//   node gate.js --host 0.0.0.0 --port 3000 --upstream 3001 --path /<token> -- <command...>
//
// Node's standard library only.

"use strict";

const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const net = require("node:net");

const MAX_HEAD = 16 * 1024; // bytes of request head read before refusing
const HEAD_TIMEOUT_MS = 10_000; // to send a whole request head
const UPSTREAM_WAIT_MS = 30_000; // for run-server to start answering
const UPSTREAM_RETRY_MS = 200;
const MAX_CONNECTIONS = 64;
const NOT_FOUND = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
const OPTIONS = ["--host", "--port", "--upstream", "--path"];
const TOKEN_PATH = /^\/[A-Za-z0-9_-]{24,}$/;

function usage() {
  console.error(
    "usage: gate.js --host <address> --port <port> --upstream <port> --path /<token> -- <command...>"
  );
  process.exit(2);
}

function port(text) {
  const value = Number(text);
  if (!Number.isInteger(value) || value < 0 || value > 65535) usage();
  return value;
}

function parse(argv) {
  const split = argv.indexOf("--");
  if (split < 0) usage();
  const own = argv.slice(0, split);
  const command = argv.slice(split + 1);
  const options = {};
  for (let index = 0; index < own.length; index += 2) {
    const name = own[index];
    const value = own[index + 1];
    if (!OPTIONS.includes(name) || value === undefined || name in options) usage();
    options[name] = value;
  }
  if (OPTIONS.some((name) => !(name in options)) || command.length === 0) usage();
  if (!TOKEN_PATH.test(options["--path"])) usage();
  return {
    host: options["--host"],
    port: port(options["--port"]),
    upstream: port(options["--upstream"]),
    path: Buffer.from(options["--path"], "latin1"),
    command,
  };
}

// The request target of a head that asks for a WebSocket upgrade, or null.
function upgradeTarget(head) {
  const lines = head.toString("latin1").split("\r\n");
  const request = /^GET (\S+) HTTP\/1\.1$/.exec(lines[0]);
  if (request === null) return null;
  const upgrade = lines.slice(1).some((line) => /^upgrade:[ \t]*websocket[ \t]*$/i.test(line));
  return upgrade ? request[1] : null;
}

function isRunPath(target, expected) {
  const given = Buffer.from(target, "latin1");
  return given.length === expected.length && crypto.timingSafeEqual(given, expected);
}

function refuse(socket) {
  socket.end(NOT_FOUND, () => socket.destroy());
}

function forward(client, first, config) {
  const deadline = Date.now() + UPSTREAM_WAIT_MS;
  const attempt = () => {
    if (client.destroyed) return;
    const upstream = net.connect(config.upstream, "127.0.0.1");
    upstream.once("error", () => {
      upstream.destroy();
      if (Date.now() < deadline) setTimeout(attempt, UPSTREAM_RETRY_MS);
      else client.destroy();
    });
    upstream.once("connect", () => {
      upstream.removeAllListeners("error");
      upstream.on("error", () => client.destroy());
      client.on("error", () => upstream.destroy());
      upstream.on("close", () => client.destroy());
      client.on("close", () => upstream.destroy());
      upstream.write(first);
      client.pipe(upstream);
      upstream.pipe(client);
      client.resume();
    });
  };
  attempt();
}

function serve(client, config) {
  let buffered = Buffer.alloc(0);
  client.on("error", () => client.destroy());
  client.setTimeout(HEAD_TIMEOUT_MS, () => client.destroy());
  const onData = (chunk) => {
    buffered = Buffer.concat([buffered, chunk]);
    const end = buffered.indexOf("\r\n\r\n");
    if (end < 0 && buffered.length <= MAX_HEAD) return;
    client.removeListener("data", onData);
    client.pause();
    const target = end < 0 || end > MAX_HEAD ? null : upgradeTarget(buffered.subarray(0, end));
    if (target === null || !isRunPath(target, config.path)) {
      refuse(client);
      return;
    }
    client.setTimeout(0);
    forward(client, buffered, config);
  };
  client.on("data", onData);
}

function main() {
  const config = parse(process.argv.slice(2));
  const child = childProcess.spawn(config.command[0], config.command.slice(1), {
    stdio: ["ignore", "ignore", "inherit"],
  });
  child.on("error", () => process.exit(1));
  child.on("exit", (code) => process.exit(code === null ? 1 : code));
  for (const signal of ["SIGTERM", "SIGINT"]) {
    process.on(signal, () => child.kill(signal));
  }
  const server = net.createServer((client) => serve(client, config));
  server.maxConnections = MAX_CONNECTIONS;
  server.on("error", (error) => {
    console.error(`gate: cannot listen: ${error.code || error.message}`);
    child.kill("SIGTERM");
    process.exit(1);
  });
  server.listen(config.port, config.host, () => {
    console.log(`gate listening on ${config.host}:${server.address().port}`);
  });
}

main();
