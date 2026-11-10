// For the Python tests: loads {html, payload} from stdin into a simulated page, runs fill.js, prints what got filled.
const fs = require("fs"), path = require("path");
const { JSDOM } = require(path.join(__dirname, "..", "dashboard", "node_modules", "jsdom"));
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const dom = new JSDOM(input.html, { url: "https://site.example.org/write-for-us", runScripts: "dangerously", pretendToBeVisual: true });
const s = dom.window.document.createElement("script");
s.textContent = fs.readFileSync(path.join(__dirname, "fill.js"), "utf8");
dom.window.document.head.appendChild(s);
const report = dom.window.blFill(input.payload);
const values = {};
dom.window.document.querySelectorAll("input, textarea, select").forEach((el) => { if (el.name) values[el.name] = el.type === "checkbox" ? el.checked : el.value; });
process.stdout.write(JSON.stringify({ report, values }));
