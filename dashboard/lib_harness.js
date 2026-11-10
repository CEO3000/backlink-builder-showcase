// Runs functions from the dashboard's <script id="lib"> block (pure logic, no DOM) for the Python tests.
// Usage: node lib_harness.js < calls.json   where calls.json is [{"fn": "slugify", "args": ["Hello"]}, ...]
// Prints a JSON array with one {ok, value | error} per call.
const fs = require("fs"), path = require("path"), vm = require("vm");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const lib = html.match(/<script id="lib">([\s\S]*?)<\/script>/);
if (!lib) { console.error("no lib script found"); process.exit(2); }
const ctx = vm.createContext({ URL, console });
vm.runInContext(lib[1], ctx);
const calls = JSON.parse(fs.readFileSync(0, "utf8"));
const out = calls.map(c => {
  try {
    const args = (c.args || []).map(a => (a && a.__date__) ? new Date(a.__date__) : a);
    return { ok: true, value: JSON.parse(JSON.stringify(vm.runInContext("(" + c.fn + ")", ctx).apply(null, args) ?? null)) };
  } catch (e) { return { ok: false, error: String(e && e.message || e) }; }
});
process.stdout.write(JSON.stringify(out));
