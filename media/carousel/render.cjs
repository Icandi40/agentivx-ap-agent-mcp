// Renders slides.html to one PNG per slide (2x) and a LinkedIn-ready PDF.
// The terminal slide is filled from build/cli.txt, real output captured from the CLI.
// Usage: PLAYWRIGHT_PATH=<path to playwright> node render.cjs
const fs = require("fs");
const path = require("path");
const { chromium } = require(process.env.PLAYWRIGHT_PATH || "playwright");

const here = __dirname;
const out = path.join(here, "out");
fs.mkdirSync(out, { recursive: true });

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const raw = fs.readFileSync(path.join(here, "build", "cli.txt"), "utf8").replace(/\r/g, "");
// Keep the gate sequence: process, refused post, refused self-approval, approval, post.
const blocks = raw.split("\n\n").slice(0, 5);
const cli = blocks
  .join("\n\n")
  .split("\n")
  .map((line) => {
    const t = esc(line.replace(/^\s+"/, '"'));
    if (line.startsWith("$")) return `<span class="c-cmd">${t}</span>`;
    if (line.startsWith("Refused")) return `<span class="c-bad">${t}</span>`;
    if (/approve$|now approved|"executed"/.test(line)) return `<span class="c-ok">${t}</span>`;
    return t;
  })
  .join("\n");

const html = fs.readFileSync(path.join(here, "slides.html"), "utf8").replace("{{CLI}}", cli);
const built = path.join(here, "build", "slides.rendered.html");
fs.writeFileSync(built, html.replace(/(src=")(assets|build)\//g, `$1../$2/`));

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1080, height: 1350 }, deviceScaleFactor: 2 });
  await page.goto("file:///" + built.replace(/\\/g, "/"), { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  const slides = await page.$$(".slide");
  for (let i = 0; i < slides.length; i++) {
    const overflow = await slides[i].evaluate((el) => {
      const foot = el.querySelector(".foot");
      const limit = foot ? foot.getBoundingClientRect().top : el.getBoundingClientRect().bottom;
      return [...el.children]
        .filter((c) => c !== foot)
        .some((c) => c.getBoundingClientRect().bottom > limit + 1);
    });
    if (overflow) console.warn(`slide ${i + 1}: content runs into the footer`);
    await slides[i].screenshot({ path: path.join(out, `slide-${String(i + 1).padStart(2, "0")}.png`) });
  }
  await page.pdf({
    path: path.join(out, "ap-agent-carousel.pdf"),
    width: "1080px",
    height: "1350px",
    printBackground: true,
    pageRanges: `1-${slides.length}`,
  });
  await browser.close();
  console.log(`rendered ${slides.length} slides and the PDF to ${out}`);
})();
