/*
 * feedsum - adds a "Brief" action to every Miniflux entry.
 *
 * Paste into Settings -> Custom JavaScript. Miniflux runs this as a nonced ES
 * module, under a CSP with `connect-src 'self'` and `require-trusted-types-for
 * 'script'`. So: same-origin fetch only, and every node is built with
 * createElement/textContent. Assigning innerHTML here would throw.
 * All styling lives in Custom CSS, because `style-src` is nonce-only.
 */

const BASE = "/_summary";
const POLL_MS = 4000;
const POLL_LIMIT = 90; // ~6 minutes, enough for a long video.

// The button doubles as the status light for an entry: whether a brief already
// exists, is being generated right now, or failed.
const STATE = {
  none: { label: "Brief", className: "" },
  pending: { label: "Brief …", className: "is-working" },
  ready: { label: "Brief", className: "is-ready" },
  error: { label: "Brief !", className: "is-failed" },
};

function el(tag, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

// The only inline markup the summarizers emit is **bold**.
function withInline(node, text) {
  const pattern = /\*\*(.+?)\*\*/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) {
      node.appendChild(document.createTextNode(text.slice(last, match.index)));
    }
    const strong = el("strong");
    strong.textContent = match[1];
    node.appendChild(strong);
    last = pattern.lastIndex;
  }
  if (last < text.length) node.appendChild(document.createTextNode(text.slice(last)));
  return node;
}

function renderMarkdown(markdown, into) {
  const lines = String(markdown).replace(/\r\n/g, "\n").split("\n");
  let list = null;
  let paragraph = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    into.appendChild(withInline(el("p"), paragraph.join(" ")));
    paragraph = [];
  };

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      flushParagraph();
      list = null;
      continue;
    }
    if (/^([-*_])(\s*\1){2,}$/.test(line)) {
      flushParagraph();
      list = null;
      into.appendChild(el("hr"));
      continue;
    }
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (heading) {
      flushParagraph();
      list = null;
      into.appendChild(withInline(el("h4"), heading[1]));
      continue;
    }
    const ordered = line.match(/^\d+[.)]\s+(.*)$/);
    const unordered = line.match(/^[-*+]\s+(.*)$/);
    if (ordered || unordered) {
      flushParagraph();
      const tag = ordered ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = el(tag);
        into.appendChild(list);
      }
      list.appendChild(withInline(el("li"), (ordered || unordered)[1]));
      continue;
    }
    list = null;
    paragraph.push(line);
  }
  flushParagraph();
}

function replaceChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function showMessage(panel, text, className) {
  replaceChildren(panel);
  const note = el("p", className);
  note.textContent = text;
  panel.appendChild(note);
}

function showSummary(panel, data) {
  replaceChildren(panel);
  renderMarkdown(data.markdown || "", panel);
  const source = el("p", "feedsum-source");
  source.textContent = `${data.source === "youtube" ? "video" : "article"} · ${data.model || ""}`;
  panel.appendChild(source);
}

async function call(path, options) {
  const response = await fetch(BASE + path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (response.status === 401 || response.redirected) {
    throw new Error("Your session expired — reload the page.");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status}).`);
  return body;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function loadSummary(entryId, panel) {
  let data = await call("/v1/summaries", {
    method: "POST",
    body: JSON.stringify({ entry_id: entryId }),
  });

  for (let attempt = 0; data.status === "pending" && attempt < POLL_LIMIT; attempt += 1) {
    await sleep(POLL_MS);
    data = await call(`/v1/summaries/${entryId}`, { method: "GET" });
  }

  if (data.status === "ready") {
    showSummary(panel, data);
  } else if (data.status === "error") {
    showMessage(panel, data.error || "The summary failed.", "feedsum-error");
  } else {
    showMessage(panel, "Still working — reopen this in a moment.", "feedsum-error");
  }
  return data.status;
}

const buttons = new Map(); // entry id -> its button, for batch status updates.

function setState(button, name) {
  const state = STATE[name] || STATE.none;
  button.textContent = state.label;
  button.className = state.className ? `feedsum-button ${state.className}` : "feedsum-button";
  button.dataset.state = name;
}

function attach(container) {
  if (container.dataset.feedsum === "on") return;
  const entryId = Number(container.dataset.id);
  if (!entryId) return;

  const actions = container.querySelector(".item-meta-icons, .entry-actions ul");
  if (!actions) return;
  container.dataset.feedsum = "on";

  const button = el("button", "feedsum-button");
  button.type = "button";
  setState(button, "none");
  buttons.set(entryId, button);

  const item = el("li", "feedsum-action");
  item.appendChild(button);
  const externalLink = actions.querySelector(".item-meta-icons-external-url");
  actions.insertBefore(item, externalLink ? externalLink.nextSibling : null);

  const panel = el("div", "feedsum-panel");
  panel.hidden = true;
  actions.parentNode.insertBefore(panel, actions.nextSibling);

  let loaded = false;
  let running = false;

  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (running) return;

    if (loaded) {
      panel.hidden = !panel.hidden;
      return;
    }

    running = true;
    panel.hidden = false;
    button.disabled = true;
    setState(button, "pending");
    showMessage(
      panel,
      button.dataset.wasReady === "yes" ? "Loading…" : "Reading and summarizing…",
      "feedsum-status",
    );
    try {
      const status = await loadSummary(entryId, panel);
      loaded = status === "ready" || status === "error";
      setState(button, status === "ready" ? "ready" : status === "error" ? "error" : "pending");
    } catch (error) {
      showMessage(panel, error.message || "Something went wrong.", "feedsum-error");
      setState(button, "error");
    } finally {
      running = false;
      button.disabled = false;
    }
  });
}

// One request tells the whole page which entries already have a brief, so the
// list is accurate as soon as it renders rather than after 100 round trips.
async function refreshStates() {
  const unknown = [...buttons.entries()]
    .filter(([, button]) => !button.dataset.checked)
    .map(([entryId]) => entryId);
  if (!unknown.length) return;

  let statuses;
  try {
    statuses = await call("/v1/summaries/status", {
      method: "POST",
      body: JSON.stringify({ entry_ids: unknown }),
    });
  } catch {
    return; // A status probe is a nicety; leave the buttons in their idle state.
  }

  for (const entryId of unknown) {
    const button = buttons.get(entryId);
    if (!button) continue;
    button.dataset.checked = "yes";
    const status = statuses[String(entryId)];
    if (!status || button.dataset.state === "pending") continue;
    setState(button, status);
    if (status === "ready") button.dataset.wasReady = "yes";
  }
}

let pending = null;
function scan() {
  document
    .querySelectorAll("article.entry-item[data-id], section.entry[data-id]")
    .forEach(attach);
  // The observer fires repeatedly while Miniflux rebuilds the list; coalesce.
  clearTimeout(pending);
  pending = setTimeout(refreshStates, 60);
}

scan();
// Miniflux replaces the list in place when marking pages read, so watch for it.
new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
