// The Markdown pipeline of the frontend that Home Assistant pins, rebuilt with the same libraries,
// for the outputs of the dashboard's contract test (docs/testing.md, "The dashboard in the
// frontend's Markdown pipeline"). Frontend 20260930.2 (Home Assistant 2026.10.0):
//   src/resources/markdown-worker.ts: the allow list, each token filtered on its own, tables
//   wrapped in a <div>
//   src/components/ha-markdown-element.ts: marked with breaks and gfm
//   src/panels/lovelace/cards/hui-markdown-card.ts: <ha-markdown breaks>
// with marked and xss at the versions of the frontend's package.json.
//
// Usage: node tests/manual/markdown_pipeline.mjs <node_modules> <outputs.json>...
// Prints the HTML tokens, images, links and ha-alert elements per file; exits 1 when a card
// gives an image or a link, or an ha-alert that its Markdown did not hold as an element.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [modules, ...inputs] = process.argv.slice(2);
if (!modules || inputs.length === 0) {
  console.error("usage: node markdown_pipeline.mjs <node_modules> <outputs.json>...");
  process.exit(2);
}
const root = resolve(modules);
const { marked } = await import(pathToFileURL(join(root, "marked", "lib", "marked.esm.js")).href);
const { filterXSS, getDefaultWhiteList } = createRequire(join(root, "/"))("xss");

const defaults = getDefaultWhiteList();
const whiteList = {
  ...defaults,
  table: [...(defaults.table ?? []), "role"],
  input: ["type", "disabled", "checked"],
  "ha-icon": ["icon"],
  "ha-svg-icon": ["path"],
  "ha-alert": ["alert-type", "title"],
  "ha-qr-code": ["data", "scale", "width", "margin", "error-correction-level", "center-image"],
};
marked.setOptions({ breaks: true, gfm: true });
marked.use({
  renderer: {
    table(...args) {
      return `<div>${new marked.Renderer().table.apply(this, args)}</div>`; // as the worker wraps it
    },
  },
});

const render = (content) =>
  marked
    .lexer(content)
    .map((token) =>
      filterXSS(marked.parser([token]), {
        whiteList,
        onTagAttr: (tag, name, value) =>
          tag === "input" &&
          !((name === "type" && value === "checkbox") || name === "checked" || name === "disabled")
            ? ""
            : undefined,
      })
    )
    .join("");

const count = (text, pattern) => (text.match(pattern) ?? []).length;
let failed = false;
for (const input of inputs) {
  const cards = JSON.parse(readFileSync(input, "utf-8"));
  const totals = { cards: 0, html: 0, img: 0, a: 0, alertsIn: 0, alertsOut: 0 };
  for (const [name, markdown] of Object.entries(cards)) {
    const html = render(markdown);
    const found = { img: count(html, /<img\b/g), a: count(html, /<a\b/g) };
    const alerts = [count(markdown, /<ha-alert\b/g), count(html, /<ha-alert\b/g)];
    totals.cards += 1;
    totals.html += marked.lexer(markdown).filter((t) => t.type === "html").length;
    totals.img += found.img;
    totals.a += found.a;
    totals.alertsIn += alerts[0];
    totals.alertsOut += alerts[1];
    if (found.img || found.a || alerts[0] !== alerts[1]) {
      failed = true;
      console.log(`${input}: ${name}: ${JSON.stringify(found)}, ha-alert ${alerts.join(" -> ")}`);
    }
  }
  console.log(`${input}: ${JSON.stringify(totals)}`);
}
process.exit(failed ? 1 : 0);
