// Close-up screenshots of the real review page for the carousel (dark theme, phone width, 3x).
// Usage: node capture_review.cjs <proposal-id> <out-dir>
// Expects `ap-agent review --port 8811` running against the carousel demo state.
// Writes review-checks.png (the flag) and review-match.png (invoice line, PO and receiving log).
const path = require("path");
const { chromium } = require(process.env.PLAYWRIGHT_PATH || "playwright");

(async () => {
  const [pid, outDir] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 620, height: 1400 },
    deviceScaleFactor: 3,
    colorScheme: "dark",
  });
  for (let i = 0; i < 20; i++) {
    try {
      await page.goto(`http://127.0.0.1:8811/p/${pid}`, { waitUntil: "load" });
      break;
    } catch {
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  const box = (el) => el.boundingBox();
  const cards = await page.$$(".card");
  const checks = await box(cards[0]);
  await page.screenshot({ path: path.join(outDir, "review-checks.png"), clip: checks });

  // From the invoice line table down to the end of the PO and receiving log table.
  const tables = await cards[1].$$("table");
  const card = await box(cards[1]);
  const first = await box(tables[1]);
  const last = await box(tables[tables.length - 1]);
  await page.screenshot({
    path: path.join(outDir, "review-match.png"),
    clip: { x: card.x, y: first.y - 12, width: card.width, height: last.y + last.height - first.y + 24 },
  });
  await browser.close();
  console.log("captured");
})();
