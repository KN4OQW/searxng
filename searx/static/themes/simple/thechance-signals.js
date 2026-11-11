// SPDX-License-Identifier: AGPL-3.0-or-later
// TheChance fork: click and quick-return signals (see searx/clicks.py).
//
// Links stay ordinary links. On a click this sends a beacon so the server can
// count it and check the page in the background. If the reader comes back to
// this results page within BOUNCE_MS, that counts as a bounce for the page.
// Only the result URL and its signature are sent: no query, no identifiers.

const BOUNCE_MS = 15000;
const STORE_KEY = "tc-last-click";

function beacon(path, fields) {
  const body = new URLSearchParams(fields);
  if (!navigator.sendBeacon?.(path, body)) {
    fetch(path, { method: "POST", body, keepalive: true, credentials: "omit" }).catch(() => {});
  }
}

function resultOf(target) {
  const link = target.closest?.("a[href]");
  const article = link?.closest("#urls article.result");
  if (!article?.dataset.tcUrl || !article.dataset.tcSig) return null;
  return { u: article.dataset.tcUrl, s: article.dataset.tcSig };
}

function onOpen(event, sameTab) {
  const result = resultOf(event.target);
  if (!result) return;
  beacon("/thechance/click", result);
  // Only a plain click in this tab can come "back"; new tabs are not bounces.
  if (sameTab) {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({ ...result, t: Date.now() }));
    } catch {
      /* storage unavailable: no bounce tracking, clicks still count */
    }
  }
}

document.addEventListener(
  "click",
  (event) => {
    if (event.button !== 0) return;
    const plain = !(event.ctrlKey || event.metaKey || event.shiftKey || event.altKey);
    onOpen(event, plain);
  },
  true
);
// middle click opens a new tab: a click, never a bounce
document.addEventListener("auxclick", (event) => event.button === 1 && onOpen(event, false), true);

function checkReturn() {
  let last = null;
  try {
    last = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
    sessionStorage.removeItem(STORE_KEY);
  } catch {
    return;
  }
  if (!last) return;
  const dwell = Date.now() - last.t;
  if (dwell > 300 && dwell < BOUNCE_MS) beacon("/thechance/bounce", { u: last.u, s: last.s, d: String(dwell) });
}

// Back button: the page comes out of the back/forward cache (pageshow persisted)
// or reloads (then the stored click is still there).
window.addEventListener("pageshow", checkReturn);
