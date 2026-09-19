/* Utah status page behaviour.
 *
 * Three jobs: render the package grid from generated data, fetch live build
 * status and open issues from the public GitHub API, and run the entrance
 * animation.
 *
 * The rule the whole file is written around: never render a green that was
 * not observed. Every network path ends in an explicit state, and the
 * "unknown" state is visually distinct from "passing" rather than a blank.
 * An unauthenticated caller gets 60 requests an hour per IP, so a rate-limited
 * visitor is a normal case, not an edge one.
 */
"use strict";

/* The only place workflows are named. Adding or moving one is a line here. */
const WORKFLOWS = [
  { repo: "projectbluefin/utah", file: "build.yml", branch: "main",
    title: "Build Utah", desc: "Four flavors: main, nvidia, gaming, nvidia-gaming." },
  { repo: "projectbluefin/utah", file: "build.yml", branch: "testing",
    title: "Build Utah", desc: "The stream the ISO and promotion gates run against." },
  { repo: "projectbluefin/utah", file: "post-testing-e2e.yml", branch: "main",
    title: "ISO end-to-end", desc: "Boots the live ISO, installs to an encrypted disk, proves the desktop starts." },
  { repo: "projectbluefin/utah-packages", file: "rebuild-rpms.yml", branch: "main",
    title: "Package factory", desc: "Rebuilds the GNOME dependency graph against Hummingbird." },
];

const API = "https://api.github.com";
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

/* ---------- helpers ---------- */

function ago(iso) {
  if (!iso) return "";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  // The label belongs to the unit the value is converted *into*, not the one it
  // came from. Getting this one step out made every duration >= 60s read a unit
  // too small: a build from two days ago rendered "2h ago".
  const steps = [[60, "m"], [60, "h"], [24, "d"], [7, "w"], [4.35, "mo"], [12, "y"]];
  let value = seconds, unit = "s";
  for (const [size, next] of steps) {
    if (Math.abs(value) < size) break;
    value = Math.round(value / size);
    unit = next;
  }
  return `${value}${unit} ago`;
}

/* Map a run to a pill class and label. `status` wins over `conclusion`,
   because a queued rerun of a previously failed run still carries the old
   conclusion and must not read as that old result. */
function runState(run) {
  if (!run) return ["unknown", "no run"];
  if (run.status === "in_progress") return ["running", "running"];
  if (run.status === "queued" || run.status === "waiting") return ["running", "queued"];
  switch (run.conclusion) {
    case "success":   return ["ok", "passing"];
    case "failure":   return ["bad", "failing"];
    case "timed_out": return ["bad", "timed out"];
    case "cancelled": return ["warn", "cancelled"];
    case "skipped":   return ["warn", "skipped"];
    default:          return ["unknown", run.conclusion || "unknown"];
  }
}

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/vnd.github+json" } });
  if (!response.ok) {
    const error = new Error(`${response.status} ${response.statusText}`);
    error.status = response.status;
    // The unauthenticated quota is small and shared per IP; say so plainly
    // rather than reporting a generic failure the reader cannot act on.
    error.rateLimited = response.status === 403 &&
      response.headers.get("X-RateLimit-Remaining") === "0";
    throw error;
  }
  return response.json();
}

/* ---------- build status ---------- */

function statusCard(spec) {
  const card = el("article", "card skeleton");
  card.append(
    el("h3", null, spec.title),
    el("p", "where", `${spec.repo} · ${spec.branch}`),
    el("div", "bar w40"), el("div", "bar w70"),
  );
  return card;
}

function fillStatusCard(card, spec, run, failure) {
  card.className = "card";
  card.replaceChildren();

  const [tone, label] = failure ? ["unknown", "unavailable"] : runState(run);
  const row = el("div", "row");
  const pill = el("span", `pill ${tone}`);
  pill.append(el("span", "dot"), document.createTextNode(label));
  row.append(pill);
  if (run && !failure) row.append(el("span", "when", ago(run.updated_at || run.created_at)));
  card.append(row);

  const heading = el("h3");
  if (run && !failure) {
    const link = el("a", null, spec.title);
    link.href = run.html_url;
    link.rel = "noopener";
    heading.append(link);
  } else {
    heading.textContent = spec.title;
  }
  card.append(heading, el("p", "where", `${spec.repo} · ${spec.branch}`), el("p", "desc", spec.desc));

  if (failure) {
    const why = failure.rateLimited
      ? "GitHub's API rate limit for anonymous visitors was reached, so the current state could not be read."
      : `The status could not be fetched (${failure.message}).`;
    card.append(el("p", "desc", why));
  }
}

async function loadStatus() {
  const grid = $("#status-grid");
  const note = $("#status-note");
  const cards = WORKFLOWS.map((spec) => {
    const card = statusCard(spec);
    grid.append(card);
    return card;
  });

  const results = await Promise.all(WORKFLOWS.map(async (spec, index) => {
    const url = `${API}/repos/${spec.repo}/actions/workflows/${spec.file}` +
                `/runs?branch=${encodeURIComponent(spec.branch)}&per_page=1`;
    try {
      const data = await getJSON(url);
      fillStatusCard(cards[index], spec, (data.workflow_runs || [])[0], null);
      return "ok";
    } catch (error) {
      fillStatusCard(cards[index], spec, null, error);
      return error.rateLimited ? "rate" : "error";
    }
  }));

  grid.setAttribute("aria-busy", "false");
  if (results.every((r) => r === "ok")) {
    note.textContent = `Fetched live from the GitHub Actions API at ${new Date().toLocaleTimeString()}.`;
  } else if (results.includes("rate")) {
    note.textContent = "Some cards could not be read: GitHub limits anonymous API calls to 60 an hour per address. " +
                       "The workflow pages on GitHub always show the authoritative state.";
  } else {
    note.textContent = "Some cards could not be read. The workflow pages on GitHub show the authoritative state.";
  }
}

/* ---------- packages ---------- */

const packageState = { data: null, group: "all", query: "" };

function renderPackages() {
  const { data, group, query } = packageState;
  const host = $("#pkg-groups");
  const needle = query.trim().toLowerCase();
  host.replaceChildren();
  let shown = 0;

  for (const entry of data.groups) {
    if (group !== "all" && group !== entry.id) continue;
    const matches = needle
      ? entry.packages.filter((name) => name.toLowerCase().includes(needle))
      : entry.packages;
    if (!matches.length) continue;
    shown += matches.length;

    const details = el("details", "pkg-group");
    // Collapse only when everything is on show and there is no search to answer.
    details.open = Boolean(needle) || group !== "all" || entry.id === "gnome";
    const summary = el("summary");
    summary.append(el("h3", null, entry.title), el("span", "tally", String(matches.length)));
    summary.append(el("p", "blurb", entry.blurb));
    details.append(summary);

    const list = el("ul", "pkg-list");
    for (const name of matches) {
      const item = el("li");
      if (needle) {
        const at = name.toLowerCase().indexOf(needle);
        item.append(
          document.createTextNode(name.slice(0, at)),
          el("mark", null, name.slice(at, at + needle.length)),
          document.createTextNode(name.slice(at + needle.length)),
        );
      } else {
        item.textContent = name;
      }
      list.append(item);
    }
    details.append(list);
    host.append(details);
  }

  $("#pkg-empty").hidden = shown > 0;
}

function renderGaps(data) {
  const list = $("#gap-list");
  list.replaceChildren();
  for (const gap of data.unavailable) {
    const item = el("li");
    item.append(el("span", "name", gap.name));
    for (const number of gap.issues) {
      const link = el("a", null, `#${number}`);
      link.href = `https://github.com/projectbluefin/utah/issues/${number}`;
      link.rel = "noopener";
      item.append(link);
    }
    list.append(item);
  }
}

function renderChips(data) {
  const chips = $("#pkg-chips");
  const total = data.groups.reduce((sum, entry) => sum + entry.packages.length, 0);
  const specs = [{ id: "all", title: "Everything", n: total }]
    .concat(data.groups.map((entry) => ({ id: entry.id, title: entry.title, n: entry.packages.length })));

  for (const spec of specs) {
    const chip = el("button", "chip");
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(spec.id === packageState.group));
    chip.append(document.createTextNode(spec.title), el("span", "count", String(spec.n)));
    chip.addEventListener("click", () => {
      packageState.group = spec.id;
      for (const other of chips.children) other.setAttribute("aria-pressed", "false");
      chip.setAttribute("aria-pressed", "true");
      renderPackages();
    });
    chips.append(chip);
  }
}

async function loadPackages() {
  let data;
  try {
    data = await getJSON("data/packages.json");
  } catch (error) {
    $("#pkg-empty").hidden = false;
    $("#pkg-empty").textContent = "The package data could not be loaded.";
    return;
  }
  packageState.data = data;

  for (const node of document.querySelectorAll('[data-stat="installed"]')) {
    node.textContent = data.totals.installed;
  }
  for (const node of document.querySelectorAll('[data-stat="unavailable"]')) {
    node.textContent = data.totals.unavailable;
  }
  for (const node of document.querySelectorAll('[data-stat="generated"]')) {
    node.textContent = data.generated_at;
  }

  renderChips(data);
  renderGaps(data);
  renderPackages();

  let timer;
  $("#pkg-search").addEventListener("input", (event) => {
    clearTimeout(timer);
    const value = event.target.value;
    timer = setTimeout(() => { packageState.query = value; renderPackages(); }, 120);
  });
}

/* ---------- roadmap ---------- */

const TRACKER = /^(tracking|roadmap|epic)\b/i;

/* The section copy promises "labelled or titled as a tracker", so a
   `tracking`-labelled issue is promoted even when its title says nothing. */
function isTracker(issue) {
  return TRACKER.test(issue.title)
    || (issue.labels || []).some((label) => TRACKER.test(label.name || label));
}

function issueCard(issue) {
  const card = el("article", "card issue");
  const row = el("div", "row");
  const tracker = isTracker(issue);
  const pill = el("span", `pill ${tracker ? "running" : "unknown"}`);
  pill.append(el("span", "dot"), document.createTextNode(tracker ? "tracker" : "open"));
  row.append(pill, el("span", "when", ago(issue.created_at)));
  card.append(row);

  const heading = el("h3");
  const link = el("a", null, issue.title);
  link.href = issue.html_url;
  link.rel = "noopener";
  heading.append(link);
  const where = el("p", "where");
  where.append(
    el("span", "num", `#${issue.number}`),
    document.createTextNode(` · ${issue.comments} comment${issue.comments === 1 ? "" : "s"}`),
  );
  card.append(heading, where);

  if (issue.labels && issue.labels.length) {
    const labels = el("div", "labels");
    for (const label of issue.labels.slice(0, 4)) {
      labels.append(el("span", "label", typeof label === "string" ? label : label.name));
    }
    card.append(labels);
  }
  return card;
}

async function loadRoadmap() {
  const grid = $("#roadmap-grid");
  const note = $("#roadmap-note");
  for (let i = 0; i < 3; i += 1) {
    const card = el("article", "card skeleton");
    card.append(el("div", "bar w40"), el("div", "bar w70"), el("div", "bar w55"));
    grid.append(card);
  }

  try {
    const issues = await getJSON(
      `${API}/repos/projectbluefin/utah/issues?state=open&sort=created&direction=desc&per_page=60`);
    const real = issues.filter((issue) => !issue.pull_request);
    // Trackers first: they are the roadmap, the rest is the queue behind it.
    real.sort((a, b) => (isTracker(b) ? 1 : 0) - (isTracker(a) ? 1 : 0));
    grid.replaceChildren(...real.slice(0, 9).map(issueCard));
    grid.setAttribute("aria-busy", "false");
    note.textContent = `${real.length} open issue${real.length === 1 ? "" : "s"}, newest first, trackers promoted.`;
  } catch (error) {
    grid.replaceChildren();
    grid.setAttribute("aria-busy", "false");
    note.textContent = error.rateLimited
      ? "Open issues could not be listed: GitHub limits anonymous API calls to 60 an hour per address."
      : `Open issues could not be listed (${error.message}).`;
  }
}

/* ---------- entrance animation ---------- */

function reveal() {
  const targets = document.querySelectorAll(".reveal");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || !("IntersectionObserver" in window)) {
    for (const node of targets) node.classList.add("in");
    return;
  }
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry, index) => {
      if (!entry.isIntersecting) return;
      setTimeout(() => entry.target.classList.add("in"), index * 70);
      observer.unobserve(entry.target);
    });
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.05 });
  for (const node of targets) observer.observe(node);
}

/* ---------- boot ---------- */

reveal();
loadPackages();
loadStatus();
loadRoadmap();
