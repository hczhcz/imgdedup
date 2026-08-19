const state = {
  tabs: [],
  currentTab: null,
  listJson: null,
  groups: [],
  ignored: [],
  old: [],
  listMode: "main",
  currentGid: null,
  autoFollow: true,
  dupgroup: null,
  dupJson: null,
  view: { z: 1, px: 0, py: 0 },
  carouselIdx: 0,
};

function storageKey(suffix) {
  return `imgdedup:${state.currentTab}:${suffix}`;
}

function loadStored(suffix, fallback) {
  try { return JSON.parse(localStorage.getItem(storageKey(suffix))) || fallback; }
  catch (e) { return fallback; }
}

function saveWorking() {
  if (state.dupgroup && state.dupgroup.hasOperations && !state.dupgroup.isCompleted)
    localStorage.setItem(storageKey("working"), JSON.stringify(state.dupgroup));
  else
    localStorage.removeItem(storageKey("working"));
}

function completedGroups() {
  return loadStored("completed", []);
}

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

function showDialog(opts) {
  return new Promise((resolve) => {
    $("modal").classList.remove("hidden");
    $("modal-text").textContent = opts.text;
    const mi = $("modal-img");
    if (opts.imgSrc) {
      mi.classList.remove("hidden");
      mi.src = opts.imgSrc;
    } else {
      mi.classList.add("hidden");
    }
    const input = $("modal-input");
    if (opts.input != null) {
      input.classList.remove("hidden");
      input.value = opts.input;
    } else {
      input.classList.add("hidden");
    }
    $("modal-cancel").classList.toggle("hidden", !opts.cancel);
    const done = (v) => {
      $("modal").classList.add("hidden");
      resolve(v);
    };
    $("modal-ok").onclick = () =>
      done(opts.input != null ? input.value.trim() : true);
    $("modal-cancel").onclick = () => done(null);
    input.onkeydown = (ev) => {
      if (ev.key === "Enter") $("modal-ok").onclick();
      if (ev.key === "Escape") done(null);
    };
    if (opts.input != null) input.focus();
  });
}

const showAlert = (text) => showDialog({ text });
const showConfirm = async (text) =>
  (await showDialog({ text, cancel: true })) === true;

function stashToCompleted(dg, hasOps) {
  const completed = completedGroups().filter((g) => g.id !== dg.id);
  completed.push({ ...structuredClone(dg), hasOperations: hasOps });
  localStorage.setItem(storageKey("completed"), JSON.stringify(completed.slice(-50)));
  localStorage.removeItem(storageKey("working"));
}

async function guardLeaveGroup() {
  const dg = state.dupgroup;
  if (!dg || !dg.hasOperations || dg.isCompleted) return true;
  const present = dg.files.filter(isPresentLike);
  const text = dg.keep_no_gap && !dg.gaps_ok && present.length === 1
    ? "This group leaves a gap in the sequence. Move it to Completed and leave?"
    : "This group has unfinished operations. Move it to Completed and leave?";
  if (!(await showConfirm(text))) return false;
  stashToCompleted(dg, true);
  return true;
}

async function switchTab(name) {
  if (name === state.currentTab) return;
  if (!(await guardLeaveGroup())) return;
  state.currentTab = name;
  state.listJson = null;
  state.currentGid = null;
  state.autoFollow = true;
  state.dupgroup = loadStored("working", null);
  if (state.dupgroup) state.currentGid = state.dupgroup.id;
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
  if (data.error || !Array.isArray(data.groups)) return;
  const json = JSON.stringify([data.groups, data.ignored, data.old]);
  const listChanged = json !== state.listJson;
  if (listChanged) {
    state.listJson = json;
    state.groups = data.groups;
    state.ignored = data.ignored || [];
    state.old = data.old || [];
    renderGroupList();
  }
  if (!force && !listChanged && state.currentGid === null) return;
  const newest = state.groups.length ? state.groups[state.groups.length - 1].id : null;
  if (state.currentGid === null && state.autoFollow && newest !== null) {
    selectGroup(newest, true);
  } else if (state.currentGid !== null) {
    await loadDupgroup();
  } else {
    state.dupgroup = null;
    renderMain();
  }
}

function listSource(mode) {
  return mode === "completed" ? completedGroups() :
    mode === "ignored" ? state.ignored :
    mode === "old" ? state.old : state.groups;
}

function updateListHighlight() {
  const target = state.currentGid != null ? String(state.currentGid) : "";
  for (const el of $("grouplist").children) {
    el.classList.toggle("active", el.dataset.gid === target);
  }
}

function renderGroupList() {
  for (const b of $("list-tabs").querySelectorAll("button")) {
    const label = b.dataset.mode[0].toUpperCase() + b.dataset.mode.slice(1);
    b.textContent = label;
    const n = listSource(b.dataset.mode).length;
    if (n) {
      const badge = document.createElement("span");
      badge.className = "count-badge";
      badge.textContent = n;
      b.appendChild(badge);
    }
  }
  const el = $("grouplist");
  const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.innerHTML = "";
  const source = listSource(state.listMode);
  for (const g of source) {
    const div = document.createElement("div");
    div.className = "group-item" + (g.id === state.currentGid ? " active" : "");
    div.dataset.gid = g.id;
    const head = document.createElement("div");
    const lv = document.createElement("span");
    lv.className = "level level-" + g.level;
    lv.textContent = g.level;
    head.appendChild(lv);
    head.appendChild(document.createTextNode("#" + g.id));
    if (state.listMode === "completed" && g.hasOperations) {
      const tag = document.createElement("span");
      tag.className = "unfinished-tag";
      tag.textContent = "unfinished";
      head.appendChild(tag);
    }
    div.appendChild(head);
    for (const f of g.files) {
      const row = document.createElement("div");
      row.className = "gfile st-" + f.status;
      const nm = document.createElement("span");
      nm.className = "fname";
      nm.textContent = f.rel_path;
      nm.title = f.rel_path;
      const sz = document.createElement("span");
      sz.textContent = fmtSize(f.size);
      row.appendChild(nm);
      row.appendChild(sz);
      div.appendChild(row);
    }
    div.onclick = () => {
      state.autoFollow = false;
      if (state.listMode === "completed") selectStoredGroup(g);
      else selectGroup(g.id, false);
    };
    el.appendChild(div);
  }
  if (stick) el.scrollTop = el.scrollHeight;
  updateUnignoreBtn();
}

function updateUnignoreBtn() {
  $("btn-unignore").disabled =
    !state.ignored.some((g) => g.id === state.currentGid);
}

async function selectGroup(gid, fromAuto) {
  if (state.currentGid !== null && gid !== state.currentGid && !fromAuto) {
    if (!(await guardLeaveGroup())) return;
  }
  state.currentGid = gid;
  const working = loadStored("working", null);
  const stashed = completedGroups().find((g) => g.id === gid && g.hasOperations);
  state.dupgroup = working && working.id === gid ? working :
    stashed ? structuredClone(stashed) : null;
  state.dupJson = null;
  resetView();
  state.carouselIdx = 0;
  updateListHighlight();
  await loadDupgroup();
  const rows = $("rows");
  rows.scrollTop = rows.scrollHeight;
}

async function selectStoredGroup(group) {
  if (!(await guardLeaveGroup())) return;
  state.currentGid = group.id;
  state.dupgroup = structuredClone(group);
  state.dupgroup.isCompleted = true;
  state.dupJson = null;
  state.autoFollow = false;
  resetView();
  updateListHighlight();
  refreshWorkingFiles().then(() => {
    state.dupJson = JSON.stringify(state.dupgroup);
    renderMain();
  });
}

async function loadDupgroup() {
  if (state.currentGid === null) return;
  if (!state.dupgroup) {
    let data = null;
    try {
      data = await api("/api/dupgroup", { group: state.currentTab, id: state.currentGid });
    } catch (e) {
      return;
    }
    if (data.error) {
      state.currentGid = null;
      state.autoFollow = true;
      state.dupJson = null;
      renderMain();
      return;
    }
    state.dupgroup = data;
  }
  await refreshWorkingFiles();
  const json = JSON.stringify(state.dupgroup);
  if (json !== state.dupJson) {
    state.dupJson = json;
    renderMain();
  }
}

async function refreshWorkingFiles() {
  const dg = state.dupgroup;
  if (!dg) return;
  await Promise.all(dg.files.map(async (f) => {
    let info;
    try {
      info = await api("/api/file-state", {
        group: state.currentTab, path: f.rel_path, md5: f.md5,
        repo_name: f.repo_path || "",
      });
    } catch (e) {
      return;
    }
    if (info.error) return;
    if (info.repo_kind) f.repo_kind = info.repo_kind;
    f.fs = {
      lib_md5: info.lib_md5,
      occupied: info.path_occupied,
      in_repo: info.in_repo,
      lib_size: info.lib_size, lib_mtime: info.lib_mtime,
      repo_size: info.repo_size, repo_mtime: info.repo_mtime,
    };
  }));
  for (const f of dg.files) {
    const s = f.fs;
    if (!s) continue;
    f.moved_from = null;
    if (s.lib_md5 && s.lib_md5 === f.md5) {
      f.status = "present";
    } else if (s.lib_md5 && dg.files.some((x) => x !== f && x.md5 === s.lib_md5)) {
      f.status = "moved";
      f.moved_from = dg.files.find((x) => x !== f && x.md5 === s.lib_md5).rel_path;
    } else if (s.in_repo) {
      f.status = s.occupied ? "replaced" : "in_repo";
    } else {
      f.status = s.occupied ? "replaced" : "missing";
    }
    if (f.status === "in_repo") {
      f.size = s.repo_size;
      f.mtime = s.repo_mtime;
    } else {
      f.size = s.lib_size;
      f.mtime = s.lib_mtime;
    }
  }
  updateWorkingRules(dg);
  saveWorking();
}

function isPresentLike(f) {
  return f.status === "present" || f.status === "moved";
}

function updateWorkingRules(dg) {
  const present = dg.files.filter(isPresentLike);
  const slotFilled = (f) => ["present", "replaced", "moved"].includes(f.status);
  const gapsOk = !dg.keep_no_gap || dg.files.every((f) =>
    slotFilled(f) || f.is_last);
  dg.gaps_ok = gapsOk;
  dg.can_complete = present.length === 1 && gapsOk;
  dg.can_ignore = present.length >= 2;
}

function fileKey(f) {
  return `${f.rel_path}\0${f.md5 || ""}`;
}

function effectiveMd5(f) {
  if (f.status === "moved" || f.status === "replaced")
    return f.fs ? f.fs.lib_md5 : null;
  if (f.status === "missing") return null;
  return f.md5;
}

function identity(f) {
  return { path: f.rel_path, md5: effectiveMd5(f) };
}

function fileAbsPath(f) {
  const tab = state.tabs.find((t) => t.name === state.currentTab);
  if (!tab) return null;
  return tab.library_root.replace(/\/+$/, "") + "/" + f.rel_path;
}

function fileImageLoc(f) {
  if (["present", "moved", "replaced"].includes(f.status))
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
  updateUnignoreBtn();
  const rows = $("rows");
  const stick = rows.scrollTop + rows.clientHeight >= rows.scrollHeight - 10;
  rows.innerHTML = "";
  if (!dg) { renderCarousel(); return; }
  const md5Counts = {};
  for (const f of dg.files) {
    const m = effectiveMd5(f);
    if (m) md5Counts[m] = (md5Counts[m] || 0) + 1;
  }
  const dupMd5s = new Set(Object.keys(md5Counts).filter((m) => md5Counts[m] >= 2));
  for (const f of dg.files) rows.appendChild(renderRow(dg, f, dupMd5s));
  if (stick) rows.scrollTop = rows.scrollHeight;
  renderCarousel();
}

function renderNeighborGrid(f) {
  const grid = document.createElement("div");
  grid.className = "neighbor-grid";
  const cells = new Array(9).fill(null);
  const prev = f.neighbors_prev || [];
  const next = f.neighbors_next || [];
  for (let i = 0; i < prev.length; i++) {
    const p = prev[prev.length - 1 - i];
    if (p) cells[3 - i] = { ...p };
  }
  cells[4] = { rel_path: f.rel_path, dup_gid: null, cur: true };
  for (let i = 0; i < next.length; i++)
    cells[5 + i] = { ...next[i] };
  for (const c of cells) {
    const cell = document.createElement("div");
    cell.className = "ngrid-cell" + (c && c.cur ? " cur" : "") +
      (c && c.dup_gid != null ? " has-dup" : "");
    if (c) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = thumbUrl(c.rel_path);
      cell.appendChild(img);
      const tip = document.createElement("div");
      tip.className = "ntip";
      tip.textContent = c.rel_path.split("/").pop() +
        (c.dup_gid != null ? ` (dup #${c.dup_gid})` : "");
      cell.appendChild(tip);
      if (c.dup_gid != null) {
        cell.onclick = () => {
          state.autoFollow = false;
          selectGroup(c.dup_gid, false);
        };
      }
    }
    grid.appendChild(cell);
  }
  return grid;
}

function renderRow(dg, f, dupMd5s) {
  const row = document.createElement("div");
  row.className = "dup-row" + (effectiveMd5(f) && dupMd5s.has(effectiveMd5(f)) ? " md5-dup" : "");

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
  row.appendChild(renderNeighborGrid(f));

  const side = document.createElement("div");
  side.className = "dup-side";
  side.title = "Click to copy absolute path";
  side.onclick = async (ev) => {
    if (ev.target.closest("button")) return;
    const abs = fileAbsPath(f);
    if (!abs) return;
    try {
      await navigator.clipboard.writeText(abs);
    } catch (e) {
      return;
    }
    side.classList.add("copied");
    setTimeout(() => side.classList.remove("copied"), 400);
  };
  const p = document.createElement("div");
  p.className = "path";
  p.textContent = f.rel_path;
  side.appendChild(p);
  const st = document.createElement("div");
  st.className = "status status-" + f.status;
  st.textContent = f.status === "moved" ? `moved here (from: ${f.moved_from})` :
    f.status + (f.repo_path ? ` (dup repo: ${f.repo_path})` : "");
  side.appendChild(st);
  if (effectiveMd5(f) && dupMd5s.has(effectiveMd5(f))) {
    const em = document.createElement("div");
    em.className = "md5-flag";
    em.textContent = "exact duplicate (same md5)";
    side.appendChild(em);
  }
  const maxSize = Math.max(...dg.files.map((x) => x.size || 0));
  const mDate = document.createElement("div");
  mDate.className = "meta";
  mDate.textContent = fmtDate(f.mtime);
  side.appendChild(mDate);
  const mSize = document.createElement("div");
  mSize.className = "meta" + (f.size && f.size === maxSize ? " max-val" : "");
  mSize.textContent = fmtSize(f.size);
  side.appendChild(mSize);
  const mDim = document.createElement("div");
  mDim.className = "meta dim";
  mDim.textContent = "? × ?";
  side.appendChild(mDim);
  if (src) {
    const infoParams = { group: state.currentTab, loc: src.loc, path: src.path };
    if (src.kind) infoParams.kind = src.kind;
    api("/api/imageinfo", infoParams).then((info) => {
      if (info.width) {
        f.width = info.width;
        f.height = info.height;
        mDim.textContent = `${info.width} × ${info.height}`;
        const areas = dg.files.map((x) => (x.width || 0) * (x.height || 0));
        const maxArea = Math.max(...areas);
        for (const el of document.querySelectorAll(".dup-row .dim"))
          el.classList.remove("max-val");
        dg.files.forEach((x, i) => {
          const el = document.querySelectorAll(".dup-row .dim")[i];
          if (el && maxArea > 0 && x.width * x.height === maxArea)
            el.classList.add("max-val");
        });
      }
    });
  }

  const actions = document.createElement("div");
  actions.className = "actions";
  const present = isPresentLike(f);
  const inRepo = f.status === "in_repo";
  const idx = dg.files.indexOf(f);
  const prevSlot = dg.files.slice(0, idx).reverse().find((x) => x.status === "in_repo");
  const nextSlot = dg.files.slice(idx + 1).find((x) => x.status === "in_repo");

  const bMain = document.createElement("button");
  if (inRepo) {
    bMain.textContent = "Restore from dup repo";
    bMain.onclick = () => doAction("/api/restore", {
      ...identity(f), repo_name: f.repo_path,
    });
  } else {
    bMain.className = "danger";
    bMain.textContent = "Move to dup repo";
    bMain.disabled = f.status !== "present";
    bMain.onclick = () => moveToRepo(f, null);
  }
  actions.appendChild(bMain);

  const slotRow = document.createElement("div");
  slotRow.className = "slot-row";
  const bPrev = document.createElement("button");
  bPrev.textContent = "\u2191 prev slot";
  bPrev.disabled = !present || !prevSlot;
  if (prevSlot) {
    bPrev.title = "Move to " + prevSlot.rel_path;
    bPrev.onclick = () => doAction("/api/move", {
      ...identity(f), target: prevSlot.rel_path,
    });
  }
  slotRow.appendChild(bPrev);
  const bNext = document.createElement("button");
  bNext.textContent = "\u2193 next slot";
  bNext.disabled = !present || !nextSlot;
  if (nextSlot) {
    bNext.title = "Move to " + nextSlot.rel_path;
    bNext.onclick = () => doAction("/api/move", {
      ...identity(f), target: nextSlot.rel_path,
    });
  }
  slotRow.appendChild(bNext);
  actions.appendChild(slotRow);

  side.appendChild(actions);
  row.appendChild(side);
  return row;
}

async function doAction(path, extra) {
  const body = { group: state.currentTab, ...extra };
  const res = await apiPost(path, body);
  if (res.error) {
    await showAlert(`${res.error}: ${res.message}`);
    return;
  }
  state.dupgroup.hasOperations = true;
  if (path === "/api/restore") {
    const file = state.dupgroup.files.find((f) => fileKey(f) === `${extra.path}\0${extra.md5}`);
    if (file) file.repo_path = null;
  }
  await refreshWorkingFiles();
  state.listJson = null;
  await pollState(true);
}

async function loadAllDims(dg) {
  await Promise.all(dg.files.map(async (f) => {
    if (f.width != null && f.height != null) return;
    const src = fileImageLoc(f);
    if (!src) return;
    const params = { group: state.currentTab, loc: src.loc, path: src.path };
    if (src.kind) params.kind = src.kind;
    try {
      const info = await api("/api/imageinfo", params);
      if (info.width && info.height) {
        f.width = info.width;
        f.height = info.height;
      }
    } catch (e) {}
  }));
}

function uniqueMaxWarn(dg, file) {
  const sizes = dg.files.map((x) => x.size).filter((s) => s != null && s > 0);
  const maxSize = sizes.length ? Math.max(...sizes) : null;
  const sizeMaxUnique = maxSize != null &&
    sizes.filter((s) => s === maxSize).length === 1 && file.size === maxSize;
  const areas = dg.files.map((x) => (x.width && x.height ? x.width * x.height : 0))
    .filter((a) => a > 0);
  const maxArea = areas.length ? Math.max(...areas) : null;
  const fileArea = file.width && file.height ? file.width * file.height : 0;
  const areaMaxUnique = maxArea != null &&
    areas.filter((a) => a === maxArea).length === 1 && fileArea === maxArea;
  return sizeMaxUnique || areaMaxUnique;
}

async function moveToRepo(file, name, skipWarn) {
  if (!skipWarn) {
    await loadAllDims(state.dupgroup);
    if (uniqueMaxWarn(state.dupgroup, file)) {
      const proceed = await showConfirm(
        "This image has the largest and unique file size or dimensions within the group. Move it to the dup repo anyway?");
      if (!proceed) return;
    }
  }
  const body = {
    group: state.currentTab, ...identity(file),
    repo_name: name || file.rel_path.split("/").pop(),
  };
  const res = await apiPost("/api/move_to_repo", body);
  if (res.error === "dst_exists") {
    const conflict = name || file.rel_path.split("/").pop();
    const kind = res.repo_kind || "fuzzy";
    const newName = await showDialog({
      text: `A file named "${conflict}" already exists in the ${kind} dup repo (shown below). Enter a different name:`,
      imgSrc: "/api/image?" + new URLSearchParams(
        { group: state.currentTab, loc: "repo", kind, path: conflict, t: Date.now() }),
      input: conflict,
      cancel: true,
    });
    if (newName) moveToRepo(file, newName, true);
    return;
  }
  if (res.error) {
    await showAlert(`${res.error}: ${res.message}`);
    return;
  }
  file.repo_path = body.repo_name;
  file.repo_kind = res.repo_kind;
  state.dupgroup.hasOperations = true;
  await refreshWorkingFiles();
  state.listJson = null;
  await pollState(true);
}

function carouselImgs() {
  return [...document.querySelectorAll(".carousel-img")];
}

function carouselShow(el) {
  for (const img of carouselImgs())
    img.classList.toggle("front", img === el);
}

function renderCarousel() {
  const dg = state.dupgroup;
  const label = $("carousel-label");
  const imgs = carouselImgs();
  if (!dg || !dg.files.length) {
    carouselShow(null);
    label.textContent = "";
    return;
  }
  const candidates = dg.files.filter((f) =>
    ["present", "moved", "replaced"].includes(f.status));
  if (!candidates.length) {
    carouselShow(null);
    label.textContent = "no images";
    return;
  }
  const f = candidates[state.carouselIdx % candidates.length];
  const src = fileImageLoc(f);
  const url = imgUrl(src.loc, src.path, f.mtime || "", src.kind);
  label.textContent = f.rel_path;
  const ready = imgs.find((i) => i.dataset.src === url);
  const target = ready ||
    imgs.find((i) => !i.classList.contains("front")) || imgs[0];
  if (!ready) {
    target.dataset.src = url;
    target.src = url;
  }
  target.decode().then(() => {
    if (target.dataset.src !== url) return;
    applyCarouselView(target);
    carouselShow(target);
  }).catch(() => {});
}

function applyCarouselView(img) {
  const targets = img ? [img] : carouselImgs();
  const vp = $("carousel-view");
  for (const t of targets) {
    if (!t.naturalWidth) continue;
    const vw = vp.clientWidth, vh = vp.clientHeight;
    const s = Math.min(vw / t.naturalWidth, vh / t.naturalHeight);
    const cx = (vw - t.naturalWidth * s) / 2;
    const cy = (vh - t.naturalHeight * s) / 2;
    t.style.transform = `translate(${cx}px, ${cy}px) scale(${s})`;
  }
}

$("btn-ignore").onclick = async () => {
  const dg = state.dupgroup;
  if (!dg || !dg.can_ignore) return;
  const md5s = [...new Set(dg.files.filter((f) => isPresentLike(f) && effectiveMd5(f)).map(effectiveMd5))];
  if (!md5s.length) return;
  const res = await apiPost("/api/ignore", { group: state.currentTab, md5s });
  if (res.error) {
    await showAlert(`${res.error}: ${res.message}`);
    return;
  }
  localStorage.removeItem(storageKey("working"));
  state.currentGid = null;
  state.dupgroup = null;
  state.autoFollow = true;
  state.listJson = null;
  await pollState(true);
};

$("btn-unignore").onclick = async () => {
  const g = state.ignored.find((x) => x.id === state.currentGid);
  if (!g) return;
  const res = await apiPost("/api/unignore", {
    group: state.currentTab,
    md5s: g.ignored_md5s,
  });
  if (res.error) {
    await showAlert(`${res.error}: ${res.message}`);
    return;
  }
  state.listJson = null;
  await pollState(true);
};

$("btn-complete").onclick = async () => {
  const dg = state.dupgroup;
  if (!dg || !dg.can_complete) return;
  stashToCompleted(dg, false);
  const idx = state.groups.findIndex((g) => g.id === dg.id);
  const prev = state.groups.slice(0, idx < 0 ? state.groups.length : idx)
    .reverse().find((g) => g.id !== dg.id);
  state.autoFollow = false;
  if (prev) {
    await selectGroup(prev.id, true);
  } else {
    state.currentGid = null;
    state.dupgroup = null;
    renderMain();
  }
  state.listJson = null;
  await pollState(true);
};

setInterval(() => {
  if (state.dupgroup) {
    state.carouselIdx++;
    renderCarousel();
  }
}, 500);

window.addEventListener("resize", () => {
  applyViewAll();
  applyCarouselView();
});

setInterval(() => pollState(false), 1000);

loadTabs();

for (const button of $("list-tabs").querySelectorAll("button")) {
  button.onclick = () => {
    state.listMode = button.dataset.mode;
    for (const b of $("list-tabs").querySelectorAll("button"))
      b.classList.toggle("active", b === button);
    $("completed-tools").classList.toggle("hidden", state.listMode !== "completed");
    renderGroupList();
  };
}

$("btn-clear-completed").onclick = async () => {
  if (completedGroups().length === 0) return;
  if (!(await showConfirm("Clear all completed groups?"))) return;
  localStorage.removeItem(storageKey("completed"));
  renderGroupList();
};
