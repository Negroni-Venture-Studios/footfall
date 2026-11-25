#!/usr/bin/env node
// Footfall's gate in front of Playwright's run-server, in the browser container.
//
// The container listens on the run's network, which the app containers under test share.
// run-server launches a browser for any client that reaches it, and it gives its secret
// path to anyone who asks `GET /json`. So the container's port is this gate's. It passes on
// only a WebSocket upgrade whose request target is exactly the run's path, compared in
// constant time, to run-server on 127.0.0.1, byte for byte but for its Host header, which
// names loopback, as run-server requires. Every other request gets an empty 404.
// run-server checks the path as well.
//
// The gate starts run-server through a keeper, a second copy of this script, which runs it
// in a process group of its own. The keeper stops that whole group, run-server and anything
// it started, when it is told to, when run-server exits, or when the gate goes, however it
// goes, since its end of a pipe from the gate then closes: so run-server never outlives the
// gate. On SIGTERM or SIGINT the group gets SIGTERM, then SIGKILL after STOP_GRACE_MS, and
// the gate exits within EXIT_BOUND_MS whatever happens. The gate exits when the keeper does,
// with its code. It never prints the path. The child's standard output, where run-server
// names the path, is discarded; its standard error goes to the container's log, which only
// the runner can read and which the runner never passes on.
//
//   node gate.js --host 0.0.0.0 --port 3000 --upstream 3001 --path /<token> -- <command...>
//
// Node's standard library only.

"use strict";

const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const net = require("node:net");

const MAX_HEAD = 16 * 1024; // bytes of request head read before refusing
const HEAD_DEADLINE_MS = 10_000; // from accepting a connection to its whole head, however sent
const PENDING_PER_ADDRESS = 8; // heads one address may keep waiting; the oldest goes first
const UPSTREAM_WAIT_MS = 30_000; // for run-server to start answering
const UPSTREAM_RETRY_MS = 200;
const MAX_CONNECTIONS = 1024;
const STOP_GRACE_MS = 3_000; // for run-server to close its browsers before its group is killed
const EXIT_BOUND_MS = 5_000; // the gate is gone this long after a signal, whatever else happens
const KEEP = "--keep";
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

// Connections whose head has not yet been judged, by remote address, oldest first. A
// connection the gate has forwarded or refused is no longer pending.
const pending = new Map();

function settle(client) {
  clearTimeout(client.footfallDeadline);
  const address = client.footfallAddress;
  const waiting = pending.get(address);
  if (waiting === undefined) return;
  const index = waiting.indexOf(client);
  if (index >= 0) waiting.splice(index, 1);
  if (waiting.length === 0) pending.delete(address);
}

function admit(client) {
  const address = client.remoteAddress || "";
  client.footfallAddress = address;
  client.footfallDeadline = setTimeout(() => client.destroy(), HEAD_DEADLINE_MS);
  client.on("close", () => settle(client));
  const waiting = pending.get(address) || [];
  pending.set(address, waiting);
  waiting.push(client);
  // Too many heads from one address: the oldest goes, so that held connections can never
  // keep a new one, such as the orchestrator's, from being heard.
  while (waiting.length > PENDING_PER_ADDRESS) {
    const oldest = waiting.shift();
    clearTimeout(oldest.footfallDeadline);
    oldest.destroy();
  }
}

// The head the gate sends on, with every Host header replaced by one naming loopback.
// run-server listens on loopback alone and, against DNS rebinding, refuses with 403 any
// request whose Host is not loopback, which the orchestrator's, naming the container's
// address, never is. The path has been checked by then, so nothing else changes.
function towardsUpstream(buffered, upstream) {
  const end = buffered.indexOf("\r\n\r\n");
  const lines = buffered.subarray(0, end).toString("latin1").split("\r\n");
  const kept = lines.filter((line, index) => index === 0 || !/^host[ \t]*:/i.test(line));
  kept.splice(1, 0, `Host: 127.0.0.1:${upstream}`);
  return Buffer.concat([Buffer.from(kept.join("\r\n"), "latin1"), buffered.subarray(end)]);
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
  admit(client);
  const onData = (chunk) => {
    buffered = Buffer.concat([buffered, chunk]);
    const end = buffered.indexOf("\r\n\r\n");
    if (end < 0 && buffered.length <= MAX_HEAD) return;
    client.removeListener("data", onData);
    client.pause();
    settle(client);
    const target = end < 0 || end > MAX_HEAD ? null : upgradeTarget(buffered.subarray(0, end));
    if (target === null || !isRunPath(target, config.path)) {
      refuse(client);
      return;
    }
    forward(client, towardsUpstream(buffered, config.upstream), config);
  };
  client.on("data", onData);
}

function killGroup(child, signal) {
  try {
    process.kill(-child.pid, signal);
  } catch {
    // the group has gone already
  }
}

// The keeper: run the command in a group of its own, and stop the whole group when told to,
// when the gate's pipe closes, or when the command exits. It listens for all three before it
// starts the command, so that no signal can end it in between and leave the command running;
// stopped before there is a command, it simply exits.
function keep(command) {
  let child = null;
  let stopping = false;
  const stop = () => {
    if (stopping) return;
    stopping = true;
    if (child === null) process.exit(1);
    killGroup(child, "SIGTERM");
    setTimeout(() => {
      killGroup(child, "SIGKILL");
      process.exit(1);
    }, STOP_GRACE_MS);
  };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
  process.stdin.on("end", stop);
  process.stdin.on("error", stop);
  process.stdin.resume();
  child = childProcess.spawn(command[0], command.slice(1), {
    stdio: ["ignore", "ignore", "inherit"],
    detached: true,
  });
  child.on("error", () => process.exit(1));
  child.on("exit", (code) => {
    killGroup(child, "SIGKILL"); // anything the command left in its group
    process.exit(code === null ? 1 : code);
  });
}

function main() {
  const argv = process.argv.slice(2);
  if (argv[0] === KEEP && argv[1] === "--" && argv.length > 2) {
    keep(argv.slice(2));
    return;
  }
  const config = parse(argv);
  // The keeper's end of this pipe closes when the gate goes, however it goes.
  const keeper = childProcess.spawn(process.execPath, [__filename, KEEP, "--", ...config.command], {
    stdio: ["pipe", "ignore", "inherit"],
  });
  keeper.on("error", () => process.exit(1));
  keeper.on("exit", (code) => process.exit(code === null ? 1 : code));
  const stop = (signal) => {
    keeper.kill(signal);
    setTimeout(() => process.exit(1), EXIT_BOUND_MS);
  };
  process.on("SIGTERM", () => stop("SIGTERM"));
  process.on("SIGINT", () => stop("SIGINT"));
  const server = net.createServer((client) => serve(client, config));
  server.maxConnections = MAX_CONNECTIONS;
  server.on("error", (error) => {
    console.error(`gate: cannot listen: ${error.code || error.message}`);
    keeper.kill("SIGTERM");
    process.exit(1);
  });
  server.listen(config.port, config.host, () => {
    console.log(`gate listening on ${config.host}:${server.address().port}`);
  });
}

main();
