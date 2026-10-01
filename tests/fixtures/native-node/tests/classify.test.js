"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { classify } = require("../src/classify.js");

test("positive", () => assert.equal(classify(7), "positive"));
test("negative", () => assert.equal(classify(-7), "negative"));
test("zero", () => assert.equal(classify(0), "zero"));
