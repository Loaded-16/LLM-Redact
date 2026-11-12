// Builds dist/panel.html (single file, script inlined) and dist/server.js (bundled).
import { build } from "esbuild";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(ROOT, "dist");
mkdirSync(dist, { recursive: true });

// Panel: bundle the browser script, then inline it into the HTML.
const panel = await build({
  entryPoints: [path.join(ROOT, "src", "panel.js")],
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "es2020",
  minify: true,
  write: false,
  legalComments: "none",
});
const js = panel.outputFiles[0].text.replace(/<\/script/gi, "<\\/script");
const html = readFileSync(path.join(ROOT, "src", "panel.html"), "utf8").replace(
  '<script type="module" src="./panel.js"></script>',
  () => `<script type="module">${js}</script>`,
);
if (html.includes('src="./panel.js"')) throw new Error("panel script was not inlined");
writeFileSync(path.join(dist, "panel.html"), html);

// Server: one file, no node_modules needed at runtime.
await build({
  entryPoints: [path.join(ROOT, "src", "server.js")],
  bundle: true,
  format: "esm",
  platform: "node",
  target: "node20",
  outfile: path.join(dist, "server.js"),
  banner: {
    js: "import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);",
  },
  legalComments: "none",
});

console.log(`built dist/panel.html (${(html.length / 1024).toFixed(0)} KB) and dist/server.js`);
