// ID-coverage preservation gate: every element ID the JS binds to must exist in index.html.
// Run before AND after the redesign — the two reports must match (no orphaned bindings).
const fs = require("fs");
const dir = __dirname;
const jsFiles = ["app.js", "map.js", "charts.js", "simulation.js"];

// IDs the JS depends on: $("id"), getElementById("id"), querySelector("#id")
const bound = new Set();
const reBound = /(?:\$\(|getElementById\()\s*["']([a-zA-Z0-9_-]+)["']/g;
const reHashSel = /querySelector(?:All)?\(\s*["']#([a-zA-Z0-9_-]+)["']/g;
for (const f of jsFiles) {
  const src = fs.readFileSync(dir + "/" + f, "utf8");
  let m;
  while ((m = reBound.exec(src)) !== null) bound.add(m[1]);
  while ((m = reHashSel.exec(src)) !== null) bound.add(m[1]);
}

// IDs present in index.html
const html = fs.readFileSync(dir + "/index.html", "utf8");
const present = new Set();
let h;
const reId = /\bid=["']([a-zA-Z0-9_-]+)["']/g;
while ((h = reId.exec(html)) !== null) present.add(h[1]);

// Dynamically-built-and-bound IDs (not in index.html): map appearance (#ma-*, map.js),
// RAG modal (#rag-*) and upload modal (#up-*) fields (simulation.js innerHTML). Self-contained.
const DYN_PREFIX = ["ma-", "rag-", "up-"];
const dynamic = new Set([...bound].filter((id) => DYN_PREFIX.some((p) => id.startsWith(p))));
const missing = [...bound].filter((id) => !present.has(id) && !dynamic.has(id)).sort();

console.log("JS-bound IDs:", bound.size, "| in index.html:", present.size, "| dynamic(#ma-*):", dynamic.size);
console.log("value= count in html:", (html.match(/\bvalue=/g) || []).length);
if (missing.length) {
  console.log("FAIL — bound IDs missing from index.html:", missing);
  process.exit(1);
} else {
  console.log("PASS — every JS-bound ID exists in index.html.");
}
