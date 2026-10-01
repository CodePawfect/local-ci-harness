"use strict";

function classify(value) {
  if (value > 0) return "positive";
  if (value < 0) return "negative";
  return "zero";
}

module.exports = { classify };
