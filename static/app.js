const state = {
  tabs: [],
  currentTab: null,
  listVersion: -1,
  groups: [],
  currentGid: null,
  autoFollow: true,
  dupgroup: null,
  view: { z: 1, px: 0, py: 0 },
  carouselIdx: 0,
  knownGids: new Set(),
};

const $ = (id) => document.getElementById(id);

async function api(path, params) {
  const qs = new URLSearchParams(params).toString();
  const resp = await fetch(path + (qs ? "?" + qs : ""));
  return resp.json();
}

async function apiPost(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

function fmtSize(n) {
  if (n == null) return "?";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(2) + " MB";
}

function fmtDate(ts) {
  if (ts == null) return "?";
  return new Date(ts * 1000).toLocaleString();
}

function imgUrl(loc, path, bust, kind) {
  const params = new URLSearchParams({ group: state.currentTab, loc, path });
  if (kind) params.set("kind", kind);
  if (bust) params.set("t", bust);
  return "/api/image?" + params.toString();
}

function thumbUrl(path) {
  const params = new URLSearchParams({ group: state.currentTab, loc: "lib", path });
  return "/api/thumb?" + params.toString();
}

async function loadTabs() {
  const data = await api("/api/groups");
  state.tabs = data.groups;
  const el = $("tabs");
  el.innerHTML = "";
  for (const t of state.tabs) {
    const div = document.createElement("div");
    div.className = "tab" + (t.name === state.currentTab ? " active" : "");
    div.textContent = t.name;
    div.onclick = () => switchTab(t.name);
    el.appendChild(div);
  }
  if (!state.currentTab && state.tabs.length) switchTab(state.tabs[0].name);
}

function guardLeaveGroup() {
  const dg = state.dupgroup;
  if (!dg || !dg.keep_no_gap) return true;
  const present = dg.files.filter((f) =>
    ["present", "replaced", "restored_external"].includes(f.status));
  if (present.length === 1 && !dg.gaps_ok) {
    return window.confirm(
      "This group has unfilled gaps (keep_no_gap is on). Leave anyway?");
  }
  return true;
}

function switchTab(name) {
  if (name === state.currentTab) return;
  if (!guardLeaveGroup()) return;
  state.currentTab = name;
  state.listVersion = -1;
  state.currentGid = null;
  state.autoFollow = true;
  state.dupgroup = null;
  state.knownGids = new Set();
  resetView();
  for (const el of $("tabs").children)
    el.classList.toggle("active", el.textContent === name);
  pollState(true);
}

async function pollState(force) {
  if (!state.currentTab) return;
  let data;
  try {
    data = await api("/api/state", { group: state.currentTab });
  } catch (e) {
    return;
  }
  if (!force && data.version === state.listVersion) return;
  state.listVersion = data.version;
  state.groups = data.groups;
  renderGroupList();
  const gids = state.groups.map((g) => g.id);
  const unresolved = state.groups.filter((g) => !g.resolved);
  const newest = unresolved.length ? unresolved[unresolved.length - 1].id : null;
  const hasNew = gids.some((id) => !state.knownGids.has(id));
  state.knownGids = new Set(gids);
  if (state.currentGid !== null && !gids.includes(state.currentGid)) {
    state.currentGid = null;
    state.autoFollow = true;
  }
  if (state.autoFollow && newest !== null && (state.currentGid === null || hasNew)) {
    selectGroup(newest, true);
  } else if (state.currentGid !== null) {
    await loadDupgroup();
  } else {
    state.dupgroup = null;
    renderMain();
  }
}

function renderGroupList() {
  const el = $("grouplist");
  const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.innerHTML = "";
  for (const g of state.groups) {
    const div = document.createElement("div");
    div.className = "group-item" + (g.id === state.currentGid ? " active" : "") +
      (g.resolved ? " resolved" : "");
    const head = document.createElement("div");
    const lv = document.createElement("span");
    lv.className = "level level-" + g.level;
    lv.textContent = g.level;
    head.appendChild(lv);
    head.appendChild(document.createTextNode("#" + g.id));
    div.appendChild(head);
    for (const f of g.files) {
      const row = document.createElement("div");
      row.className = "gfile st-" + f.status;
      const nm = document.createElement("span");
      nm.className = "fname";
      nm.textContent = f.name;
      nm.title = f.rel_path;
      const sz = document.createElement("span");
      sz.textContent = fmtSize(f.size);
      row.appendChild(nm);
      row.appendChild(sz);
      div.appendChild(row);
    }
    div.onclick = () => {
      state.autoFollow = false;
      selectGroup(g.id, false);
    };
    el.appendChild(div);
  }
  if (stick) el.scrollTop = el.scrollHeight;
}

async function selectGroup(gid, fromAuto) {
  if (state.currentGid !== null && gid !== state.currentGid && !fromAuto) {
    if (!guardLeaveGroup()) return;
  }
  state.currentGid = gid;
  resetView();
  state.carouselIdx = 0;
  for (const el of $("grouplist").children) {
    el.classList.toggle("active",
      el.firstChild && el.firstChild.textContent.endsWith("#" + gid));
  }
  await loadDupgroup();
  const rows = $("rows");
  rows.scrollTop = rows.scrollHeight;
}

async function loadDupgroup() {
  if (state.currentGid === null) return;
  let data;
  try {
    data = await api("/api/dupgroup", { group: state.currentTab, id: state.currentGid });
  } catch (e) {
    return;
  }
  if (data.error) {
    state.dupgroup = null;
  } else {
    state.dupgroup = data;
  }
  renderMain();
}

function fileImageLoc(f) {
  if (["present", "replaced", "restored_external"].includes(f.status))
    return { loc: "lib", path: f.rel_path, kind: null };
  if (f.status === "in_repo")
    return { loc: "repo", path: f.repo_path, kind: f.repo_kind };
  return null;
}

function resetView() {
  state.view = { z: 1, px: 0, py: 0 };
}

function applyViewAll() {
  document.querySelectorAll(".sync-img").forEach(applyView);
}

function applyView(img) {
  const vp = img.parentElement;
  if (!img.naturalWidth) return;
  const vw = vp.clientWidth, vh = vp.clientHeight;
  const s0 = Math.min(vw / img.naturalWidth, vh / img.naturalHeight);
  const s = s0 * state.view.z;
  const cx = (vw - img.naturalWidth * s) / 2;
  const cy = (vh - img.naturalHeight * s) / 2;
  img.style.transform =
    `translate(${cx + state.view.px}px, ${cy + state.view.py}px) scale(${s})`;
}

let dragState = null;

window.addEventListener("mousemove", (ev) => {
  if (!dragState) return;
  state.view.px += ev.clientX - dragState.x;
  state.view.py += ev.clientY - dragState.y;
  dragState.x = ev.clientX;
  dragState.y = ev.clientY;
  applyViewAll();
});

window.addEventListener("mouseup", () => {
  if (dragState) {
    dragState.vp.classList.remove("dragging");
    dragState = null;
  }
});

function setupViewport(vp) {
  vp.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.2 : 1 / 1.2;
    const rect = vp.getBoundingClientRect();
    const mx = ev.clientX - rect.left - rect.width / 2;
    const my = ev.clientY - rect.top - rect.height / 2;
    const v = state.view;
    v.px = mx + (v.px - mx) * factor;
    v.py = my + (v.py - my) * factor;
    v.z *= factor;
    applyViewAll();
  }, { passive: false });
  vp.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0) return;
    dragState = { x: ev.clientX, y: ev.clientY, vp };
    vp.classList.add("dragging");
    ev.preventDefault();
  });
}

function renderMain() {
  const dg = state.dupgroup;
  $("group-title").textContent = dg ? `Group #${dg.id} [${dg.level}]` : "no group selected";
  const btn = $("btn-complete");
  btn.disabled = !dg || !dg.can_complete;
  $("btn-ignore").disabled = !dg || !dg.can_ignore;
  const rows = $("rows");
  const stick = rows.scrollTop + rows.clientHeight >= rows.scrollHeight - 10;
  rows.innerHTML = "";
  if (!dg) { renderCarousel(); return; }
  for (const f of dg.files) rows.appendChild(renderRow(dg, f));
  if (stick) rows.scrollTop = rows.scrollHeight;
  renderCarousel();
}

function renderNeighborCol(neighbors) {
  const col = document.createElement("div");
  col.className = "neighbors";
  for (const n of neighbors) {
    const d = document.createElement("div");
    d.className = "neighbor" + (n.dup_gid != null ? " has-dup" : "");
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = thumbUrl(n.rel_path);
    d.appendChild(img);
    const tip = document.createElement("div");
    tip.className = "ntip";
    tip.textContent = n.rel_path.split("/").pop() +
      (n.dup_gid != null ? ` (dup #${n.dup_gid})` : "");
    d.appendChild(tip);
    if (n.dup_gid != null) {
      d.onclick = () => {
        state.autoFollow = false;
        selectGroup(n.dup_gid, false);
      };
    }
    col.appendChild(d);
  }
  return col;
}

function renderRow(dg, f) {
  const row = document.createElement("div");
  row.className = "dup-row";
  row.appendChild(renderNeighborCol(f.neighbors_prev));

  const vp = document.createElement("div");
  vp.className = "dup-viewport";
  const src = fileImageLoc(f);
  if (src) {
    const img = document.createElement("img");
    img.className = "sync-img";
    img.src = imgUrl(src.loc, src.path, f.mtime || "", src.kind);
    img.onload = () => applyView(img);
    vp.appendChild(img);
    setupViewport(vp);
  } else {
    const ph = document.createElement("div");
    ph.className = "placeholder";
    ph.textContent = "missing: " + f.rel_path;
    vp.appendChild(ph);
  }
  row.appendChild(vp);
  row.appendChild(renderNeighborCol(f.neighbors_next));

  const side = document.createElement("div");
  side.className = "dup-side";
  const p = document.createElement("div");
  p.className = "path";
  p.textContent = f.rel_path;
  side.appendChild(p);
  const st = document.createElement("div");
  st.className = "status status-" + f.status;
  st.textContent = f.status + (f.repo_path ? ` (repo: ${f.repo_path})` : "");
  side.appendChild(st);
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${fmtSize(f.size)} · ${fmtDate(f.mtime)}`;
  side.appendChild(meta);
  const dim = document.createElement("div");
  dim.className = "meta dim";
  side.appendChild(dim);
  if (src) {
    const infoParams = { group: state.currentTab, loc: src.loc, path: src.path };
    if (src.kind) infoParams.kind = src.kind;
    api("/api/imageinfo", infoParams).then((info) => {
      if (info.width) dim.textContent = `${info.width} × ${info.height}`;
    });
  }

  const actions = document.createElement("div");
  actions.className = "actions";
  const canMove = ["present", "replaced", "restored_external"].includes(f.status);
  if (canMove && !f.repo_path) {
    const b = document.createElement("button");
    b.className = "danger";
    b.textContent = "Move to repo";
    b.onclick = () => moveToRepo(dg.id, f.rel_path, null);
    actions.appendChild(b);
  }
  if (f.status === "in_repo") {
    const b = document.createElement("button");
    b.textContent = "Restore";
    b.onclick = () => doAction("/api/restore", { path: f.rel_path });
    actions.appendChild(b);
  }
  if (canMove && !f.repo_path) {
    const idx = dg.files.indexOf(f);
    const prevSlot = dg.files.slice(0, idx).reverse().find((x) => x.status === "in_repo");
    const nextSlot = dg.files.slice(idx + 1).find((x) => x.status === "in_repo");
    if (prevSlot) {
      const b = document.createElement("button");
      b.textContent = "Move to prev slot";
      b.title = prevSlot.rel_path;
      b.onclick = () => doAction("/api/relocate", { path: f.rel_path, target: prevSlot.rel_path });
      actions.appendChild(b);
    }
    if (nextSlot) {
      const b = document.createElement("button");
      b.textContent = "Move to next slot";
      b.title = nextSlot.rel_path;
      b.onclick = () => doAction("/api/relocate", { path: f.rel_path, target: nextSlot.rel_path });
      actions.appendChild(b);
    }
  }
  side.appendChild(actions);
  row.appendChild(side);
  return row;
}

async function doAction(path, extra) {
  const body = { group: state.currentTab, id: state.currentGid, ...extra };
  const res = await apiPost(path, body);
  if (res.error) alert(`${res.error}: ${res.message}`);
  state.listVersion = -1;
  await pollState(true);
}

async function moveToRepo(gid, relPath, name) {
  const body = { group: state.currentTab, id: gid, path: relPath };
  if (name) body.repo_name = name;
  const res = await apiPost("/api/move_to_repo", body);
  if (res.error === "dst_exists") {
    const conflict = name || relPath.split("/").pop();
    showRenameModal(conflict, res.repo_kind || "fuzzy",
      (newName) => moveToRepo(gid, relPath, newName));
    return;
  }
  if (res.error) alert(`${res.error}: ${res.message}`);
  state.listVersion = -1;
  await pollState(true);
}

function showRenameModal(conflictName, repoKind, onOk) {
  $("modal").classList.remove("hidden");
  $("modal-text").textContent =
    `A file named "${conflictName}" already exists in the ${repoKind} repo (shown below). Enter a different name:`;
  const mi = $("modal-img");
  mi.classList.remove("hidden");
  mi.src = "/api/image?" + new URLSearchParams(
    { group: state.currentTab, loc: "repo", kind: repoKind, path: conflictName, t: Date.now() });
  const input = $("modal-input");
  input.classList.remove("hidden");
  input.value = conflictName;
  input.focus();
  $("modal-ok").onclick = () => {
    const v = input.value.trim();
    hideModal();
    if (v) onOk(v);
  };
  $("modal-cancel").onclick = hideModal;
  input.onkeydown = (ev) => {
    if (ev.key === "Enter") $("modal-ok").onclick();
    if (ev.key === "Escape") hideModal();
  };
}

function hideModal() {
  $("modal").classList.add("hidden");
}

function renderCarousel() {
  const dg = state.dupgroup;
  const img = $("carousel-img");
  const label = $("carousel-label");
  if (!dg || !dg.files.length) {
    img.style.display = "none";
    label.textContent = "";
    return;
  }
  const candidates = dg.files.filter((f) => fileImageLoc(f));
  if (!candidates.length) {
    img.style.display = "none";
    label.textContent = "no images";
    return;
  }
  const f = candidates[state.carouselIdx % candidates.length];
  const src = fileImageLoc(f);
  const url = imgUrl(src.loc, src.path, f.mtime || "", src.kind);
  img.style.display = "";
  if (img.dataset.src !== url) {
    img.dataset.src = url;
    img.src = url;
    img.onload = () => applyCarouselView();
  } else {
    applyCarouselView();
  }
  label.textContent = f.rel_path;
}

function applyCarouselView() {
  const img = $("carousel-img");
  const vp = $("carousel");
  if (!img.naturalWidth) return;
  const vw = vp.clientWidth, vh = vp.clientHeight;
  const s0 = Math.min(vw / img.naturalWidth, vh / img.naturalHeight);
  const mainVp = document.querySelector(".dup-viewport");
  let ratio = 1;
  if (mainVp) ratio = Math.min(vw / mainVp.clientWidth, vh / mainVp.clientHeight);
  const s = s0 * state.view.z;
  const cx = (vw - img.naturalWidth * s) / 2;
  const cy = (vh - img.naturalHeight * s) / 2;
  img.style.transform =
    `translate(${cx + state.view.px * ratio}px, ${cy + state.view.py * ratio}px) scale(${s})`;
}

$("btn-ignore").onclick = async () => {
  const dg = state.dupgroup;
  if (!dg) return;
  if (!window.confirm(
    "Ignore this group? It will be hidden from the list. (All files have identical md5.)"))
    return;
  const res = await apiPost("/api/ignore", { group: state.currentTab, id: dg.id });
  if (res.error) alert(`${res.error}: ${res.message}`);
  state.currentGid = null;
  state.dupgroup = null;
  state.autoFollow = true;
  state.listVersion = -1;
  await pollState(true);
};

$("btn-complete").onclick = async () => {
  const dg = state.dupgroup;
  if (!dg) return;
  await apiPost("/api/complete", { group: state.currentTab, id: dg.id });
  const idx = state.groups.findIndex((g) => g.id === dg.id);
  const prev = state.groups.slice(0, idx).reverse().find((g) => !g.resolved);
  state.autoFollow = false;
  if (prev) {
    await selectGroup(prev.id, true);
  } else {
    state.currentGid = null;
    state.dupgroup = null;
    renderMain();
  }
  state.listVersion = -1;
  await pollState(true);
};

setInterval(() => {
  if (state.dupgroup) {
    state.carouselIdx++;
    renderCarousel();
  }
}, 500);

setInterval(() => pollState(false), 1000);

const origApplyViewAll = applyViewAll;
applyViewAll = function () {
  origApplyViewAll();
  applyCarouselView();
};

loadTabs();
