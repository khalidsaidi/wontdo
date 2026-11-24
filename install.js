#!/usr/bin/env node
// npx wontdo: add the wontdo marketplace and install the plugin in Claude Code and Codex.
const { spawnSync } = require("child_process");

const REPO = "khalidsaidi/wontdo";
const PLUGIN = "wontdo@wontdo";

const args = process.argv.slice(2);
const wanted = ["claude", "codex"].filter((a) => args.includes(`--${a}`));
const targets = wanted.length ? wanted : ["claude", "codex"];

function run(cmd, cmdArgs) {
  console.log(`$ ${cmd} ${cmdArgs.join(" ")}`);
  return spawnSync(cmd, cmdArgs, { stdio: "inherit" }).status === 0;
}

function onPath(cmd) {
  const probe = spawnSync(cmd, ["--version"], { stdio: "ignore" });
  return !probe.error && probe.status === 0;
}

const steps = {
  claude: [
    ["plugin", "marketplace", "add", REPO],
    ["plugin", "marketplace", "update", "wontdo"],
    ["plugin", "install", PLUGIN],
  ],
  codex: [
    ["plugin", "marketplace", "add", REPO],
    ["plugin", "marketplace", "upgrade", "wontdo"],
    ["plugin", "add", PLUGIN],
  ],
};

if (!onPath("python3")) {
  console.warn("warning: python3 not found on PATH; wontdo's hooks need it to run.");
}

let installed = 0;
let failed = false;
for (const cli of targets) {
  if (!onPath(cli)) {
    if (wanted.includes(cli)) {
      console.error(`${cli} not found on PATH.`);
      failed = true;
    }
    continue;
  }
  const [addMarketplace, refreshMarketplace, install] = steps[cli];
  // An existing marketplace makes "add" fail; refresh it instead.
  if (!run(cli, addMarketplace)) run(cli, refreshMarketplace);
  if (run(cli, install)) {
    installed++;
    console.log(`wontdo installed in ${cli}.\n`);
  } else {
    console.error(`Installing wontdo in ${cli} failed.\n`);
    failed = true;
  }
}

if (!installed && !failed) {
  console.error("Neither claude nor codex found on PATH.");
  failed = true;
}
process.exit(failed ? 1 : 0);
