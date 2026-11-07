// Сборка демо-страницы одним файлом: JS, CSS и снимок данных встраиваются в HTML.
// node scripts/pack-demo.mjs <dist-demo> <snapshot.json> <out.html>
import fs from "node:fs";
import path from "node:path";

const [dist, snapshotPath, outPath] = process.argv.slice(2);
const index = fs.readFileSync(path.join(dist, "index.html"), "utf8");
const js = [...index.matchAll(/<script type="module" crossorigin src="\/([^"]+)"><\/script>/g)].map((m) =>
  fs.readFileSync(path.join(dist, m[1]), "utf8"));
const css = [...index.matchAll(/<link rel="stylesheet" crossorigin href="\/([^"]+)">/g)].map((m) =>
  fs.readFileSync(path.join(dist, m[1]), "utf8"));
const snapshot = fs.readFileSync(snapshotPath, "utf8").replace(/<\/script/gi, "<\\/script");

const page = `<title>Демо-стенд BIS</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500;600;700&display=swap">
<style>${css.join("\n")}</style>
<div id="root"></div>
<script>window.__BIS_SNAPSHOT__ = ${snapshot};</script>
<script type="module">${js.join("\n").replace(/<\/script/gi, "<\\/script")}</script>
`;
fs.writeFileSync(outPath, page);
console.log(`${outPath}: ${(page.length / 1024 / 1024).toFixed(2)} МБ`);
