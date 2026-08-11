// One Piece tracker — iOS home screen widget
//
// iOS has no native "web page widget", so this uses Scriptable (free on the
// App Store) to fetch the same data.json the dashboard reads and render a real
// widget. Small / medium / large are all supported.
//
// Setup:
//   1. App Store -> install "Scriptable"
//   2. Open it, tap + , paste this whole file, name it "OP Tracker"
//   3. Home screen -> long press -> + -> Scriptable -> pick a size -> Add
//   4. Long press the new widget -> Edit Widget -> Script: OP Tracker
//   5. Optional: set "When Interacting" to Run Script
//
// Tapping the widget opens the full dashboard.

const DATA_URL = "https://clanford06.github.io/op-price-tracker/data.json";
const SITE_URL = "https://clanford06.github.io/op-price-tracker/";

// Cache-bust so the widget never shows a stale CDN copy.
const data = await new Request(`${DATA_URL}?t=${Date.now()}`).loadJSON();

const pf = data.portfolio || {};
const size = config.widgetFamily || "medium";

const GOOD = new Color("#3ddc97");
const BAD = new Color("#ff6b5e");
const WARN = new Color("#f0a35e");
const MUTED = new Color("#9aa4b2");

const money = (n) => (n < 0 ? "-$" : "$") + Math.abs(Number(n || 0)).toFixed(2);

const w = new ListWidget();
w.url = SITE_URL;
w.backgroundColor = new Color("#171b21");
w.setPadding(12, 14, 12, 14);

// --- headline: net position -------------------------------------------------
const pos = Number(pf.position_net || 0);

const head = w.addStack();
head.centerAlignContent();
const title = head.addText("One Piece");
title.font = Font.mediumSystemFont(11);
title.textColor = MUTED;
head.addSpacer();
const roi = head.addText(pf.roi_pct == null ? "" : `${pf.roi_pct > 0 ? "+" : ""}${pf.roi_pct}%`);
roi.font = Font.boldSystemFont(11);
roi.textColor = pos >= 0 ? GOOD : BAD;

w.addSpacer(4);
const big = w.addText(money(pos));
big.font = Font.boldRoundedSystemFont(size === "small" ? 24 : 28);
big.textColor = pos >= 0 ? GOOD : BAD;

const sub = w.addText(
  `${money(pf.spent)} spent · ${money(pf.unrealised_net)} held`
);
sub.font = Font.systemFont(10);
sub.textColor = MUTED;

// --- product rows -----------------------------------------------------------
const products = Object.entries(data.products || {})
  .filter(([, p]) => p.current)
  .sort((a, b) => (b[1].current.purchase?.score || 0) - (a[1].current.purchase?.score || 0));

const rowLimit = size === "small" ? 0 : size === "medium" ? 3 : 6;

if (rowLimit && products.length) {
  w.addSpacer(8);
  for (const [, p] of products.slice(0, rowLimit)) {
    const c = p.current;
    const b = c.purchase || {};
    const row = w.addStack();
    row.centerAlignContent();

    const nm = row.addText(shortName(p.name));
    nm.font = Font.systemFont(11);
    nm.textColor = Color.white();
    nm.lineLimit = 1;

    row.addSpacer();

    const price = row.addText(money(c.total));
    price.font = Font.boldRoundedSystemFont(11);
    price.textColor = Color.white();

    row.addSpacer(6);

    const score = row.addText(String(b.score ?? "—"));
    score.font = Font.boldSystemFont(11);
    score.textColor = (b.score ?? 0) >= 80 ? GOOD : (b.score ?? 0) >= 65 ? WARN : MUTED;

    w.addSpacer(3);
  }
}

// --- footer -----------------------------------------------------------------
w.addSpacer();
const foot = w.addText(agoText(data.updated_at));
foot.font = Font.systemFont(9);
foot.textColor = MUTED;

// Refresh roughly with the 2-hourly job. iOS decides the real cadence.
w.refreshAfterDate = new Date(Date.now() + 30 * 60 * 1000);

if (config.runsInWidget) {
  Script.setWidget(w);
} else {
  await w.presentMedium();
}
Script.complete();

function shortName(name) {
  return String(name || "")
    .replace(/^OP-(\d+)\s+.*?(loose packs)?$/i, (m, n, packs) =>
      packs ? `OP-${n} packs` : `OP-${n}`)
    .slice(0, 18);
}

function agoText(iso) {
  if (!iso) return "never updated";
  const mins = Math.round((Date.now() - new Date(iso)) / 60000);
  if (mins < 60) return `updated ${mins}m ago`;
  const h = Math.round(mins / 60);
  if (h < 24) return `updated ${h}h ago`;
  return `updated ${Math.round(h / 24)}d ago`;
}
