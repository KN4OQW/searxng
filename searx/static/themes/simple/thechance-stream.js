// SPDX-License-Identifier: AGPL-3.0-or-later
// TheChance fork: streaming results.
//
// The results page is rendered after a short first-paint deadline. Engines
// still running are listed in #tc-stream; they finish in the background and
// leave their answers in the per-engine cache. This polls the same search with
// partial=1 (cache reads only, never new engine requests) and merges the new
// results in.
//
// Stability rule: the results the reader already sees at the top stay put. New
// results go in at their rank, but never above position PIN.

const PIN = 5;
const INTERVAL_MS = 400;
const MAX_POLLS = 40; // ~16 s, longer than any engine timeout

const box = document.getElementById("tc-stream");
const urls = document.getElementById("urls");

const keyOf = (article) => {
  const link = article.querySelector("a.url_header") || article.querySelector("h3 a");
  return link ? link.href : null;
};

const articles = (root) => Array.from(root.querySelectorAll("article.result"));

function mergeResults(doc) {
  const incoming = articles(doc.getElementById("urls") || doc);
  if (!incoming.length) return 0;

  const seen = new Set(articles(urls).map(keyOf).filter(Boolean));
  let added = 0;

  incoming.forEach((article, rank) => {
    const key = keyOf(article);
    if (!key || seen.has(key)) return;
    seen.add(key);

    const current = articles(urls);
    const at = Math.max(rank, Math.min(PIN, current.length));
    const node = document.importNode(article, true);
    node.classList.add("tc-arrived");

    const anchor = current[at];
    if (anchor) anchor.parentNode.insertBefore(node, anchor);
    else urls.appendChild(node);
    added += 1;
  });

  if (added) urls.querySelector(".dialog-error-block")?.remove();
  return added;
}

// Sidebar pieces that may only exist once a slower engine has answered.
function mergeSidebar(doc) {
  for (const id of ["infoboxes", "answers"]) {
    const fresh = doc.getElementById(id);
    if (!fresh) continue;
    const old = document.getElementById(id);
    if (old) old.replaceWith(document.importNode(fresh, true));
    else if (id === "infoboxes") document.getElementById("sidebar")?.prepend(document.importNode(fresh, true));
    else document.getElementById("results")?.prepend(document.importNode(fresh, true));
  }
}

function setStatus(pending) {
  if (!pending.length) {
    box.classList.add("tc-done");
    box.querySelector(".tc-stream-text").textContent = "All results loaded";
    setTimeout(() => box.remove(), 1500);
    return;
  }
  box.querySelector(".tc-stream-text").textContent = `Still loading: ${pending.join(", ")}`;
}

async function poll() {
  const url = box.dataset.url;
  for (let i = 0; i < MAX_POLLS; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, INTERVAL_MS));
    let doc;
    try {
      const resp = await fetch(url, { credentials: "same-origin", headers: { Accept: "text/html" } });
      if (!resp.ok) break;
      doc = new DOMParser().parseFromString(await resp.text(), "text/html");
    } catch {
      break;
    }
    mergeResults(doc);
    mergeSidebar(doc);
    const next = doc.getElementById("tc-stream");
    const pending = next?.dataset.pending ? next.dataset.pending.split(",") : [];
    setStatus(pending);
    if (!pending.length) return;
  }
  setStatus([]);
}

if (box && urls && box.dataset.url) {
  poll();
}
