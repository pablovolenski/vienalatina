// Runs the editor's DOM → markdown serializer outside a browser, so the part
// of it that is pure logic can be tested by the same `pytest` run as the rest
// of the project rather than only by looking at a page.
//
// board.js expects a document to attach to and a DOM to walk. Both are stubbed
// here with the handful of members the serializer actually reads — nodeType,
// nodeValue, tagName, childNodes, children, getAttribute, textContent — built
// from the JSON tree the test sends in. A real DOM implementation would be a
// dependency; this is thirty lines and cannot drift, because the test fails the
// moment the serializer reaches for something it does not provide.

"use strict";

globalThis.document = { addEventListener: function () {} };
globalThis.window = {};

var path = require("path");
var board = require(path.join(__dirname, "..", "static", "board.js"));

function text(node) {
  if (node.nodeType === 3) { return node.nodeValue; }
  return node.childNodes.map(text).join("");
}

function build(spec) {
  if (typeof spec === "string") { return { nodeType: 3, nodeValue: spec }; }
  var node = {
    nodeType: 1,
    tagName: String(spec.tag).toUpperCase(),
    attrs: spec.attrs || {},
    childNodes: (spec.children || []).map(build)
  };
  node.children = node.childNodes.filter(function (child) { return child.nodeType === 1; });
  node.getAttribute = function (name) {
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
  };
  Object.defineProperty(node, "textContent", { get: function () { return text(this); } });
  return node;
}

var input = "";
process.stdin.on("data", function (chunk) { input += chunk; });
process.stdin.on("end", function () {
  var root = build({ tag: "div", children: JSON.parse(input) });
  process.stdout.write(board.serialize(root));
});
