"use strict";
const { mkdirSync, readFileSync, writeFileSync } = require("node:fs");
const { spawnSync } = require("node:child_process");

mkdirSync("coverage", { recursive: true });
const execution = spawnSync(process.execPath, [
  "--test", "--experimental-test-coverage", "--test-coverage-include=src/classify.js",
  "--test-reporter=lcov", "--test-reporter-destination=coverage/lcov.info",
  "tests/classify.test.js",
], { stdio: "inherit" });
if (execution.error) throw execution.error;
if (execution.status !== 0) process.exit(execution.status ?? 1);

const lcov = readFileSync("coverage/lcov.info", "utf8");
function counter(name) {
  const matches = [...lcov.matchAll(new RegExp(`^${name}:(\\d+)$`, "gm"))];
  if (matches.length === 0) throw new Error(`Missing actual LCOV counter ${name}`);
  return matches.reduce((sum, match) => sum + Number(match[1]), 0);
}
const summary = { total: {
  lines: { total: counter("LF"), covered: counter("LH") },
  branches: { total: counter("BRF"), covered: counter("BRH") },
} };
writeFileSync("coverage/coverage-summary.json", JSON.stringify(summary));
