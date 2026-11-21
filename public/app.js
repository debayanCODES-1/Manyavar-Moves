import { upload } from "@vercel/blob/client";

const state = {
  summary: null, moves: [], stores: [], insights: [], currentView: "overview",
  selectedProduct: null, selectedStore: null, products: [],
};
const authState = { required: false, authenticated: true, demoMode: false, user: null };
const apiSessionTokenKey = "manyavar-api-session-token-v1";
const photoUrls = [
  "https://images.unsplash.com/photo-1610030469983-98e550d6193c?auto=format&fit=crop&w=320&q=80",
  "https://images.unsplash.com/photo-1597983073493-88cd35cf93b0?auto=format&fit=crop&w=320&q=80",
  "https://images.unsplash.com/photo-1603400521630-9f2de124b33b?auto=format&fit=crop&w=320&q=80",
];
let toastTimer;

function illustrativePhoto(key) {
  const value = String(key || "");
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) >>> 0;
  }
  return photoUrls[hash % photoUrls.length];
}

function gradeTag(grade, title = "") {
  const gradeClass = String(grade || "UNKNOWN").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  return `<span class="grade-tag grade-${gradeClass}"${title ? ` title="${escapeHTML(title)}"` : ""}>${escapeHTML(grade || "UNKNOWN")}</span>`;
}

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const number = (value) => new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(value || 0);
const rupees = (value) => new Intl.NumberFormat("en-IN", {
  style: "currency", currency: "INR", maximumFractionDigits: 0,
}).format(value || 0);
const shortRupees = (value) => {
  if (!value) return "₹0";
  if (value >= 10_000_000) return `₹${(value / 10_000_000).toFixed(1)}Cr`;
  if (value >= 100_000) return `₹${(value / 100_000).toFixed(1)}L`;
  return rupees(value);
};

async function api(path, options = {}) {
  const apiBase = window.__MANYAVAR_CONFIG__?.apiBaseUrl || "";
  const requestPath = apiBase && path.startsWith("/")
    ? `${apiBase}${path}`
    : path;
  const isCrossOrigin = /^https?:\/\//i.test(requestPath)
    && new URL(requestPath, window.location.href).origin !== window.location.origin;
  const headers = new Headers(options.headers || {});
  const sessionToken = sessionStorage.getItem(apiSessionTokenKey);
  if (sessionToken && !headers.has("authorization")) {
    headers.set("authorization", `Bearer ${sessionToken}`);
  }
  const response = await fetch(requestPath, {
    ...options,
    headers,
    credentials: options.credentials || (isCrossOrigin ? "omit" : "same-origin"),
  });
  const contentType = response.headers.get("content-type") || "";
  const result = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof result === "object" ? result.detail || result.message : result;
    throw new Error(message || `Request failed (${response.status})`);
  }
  return result;
}

function updateDemoRoleControls() {
  const demoRole = $("#demo-role");
  const isAreaManager = demoRole.value === "area_manager";
  $("#area-manager-label").hidden = !authState.demoMode || !isAreaManager;
  $("#area-manager-account").hidden = !authState.demoMode || !isAreaManager;
  const tokenRequired = authState.demoMode
    ? Boolean(authState.tokenRequired && demoRole.value !== "viewer")
    : Boolean(authState.required);
  $("#token-label").hidden = !tokenRequired;
  $("#auth-token").hidden = !tokenRequired;
  $("#auth-token").required = tokenRequired;
}

async function updateAuthState() {
  try {
    Object.assign(authState, await api("/api/auth/status"));
    if (authState.required && !authState.authenticated) {
      sessionStorage.removeItem(apiSessionTokenKey);
    }
    const button = $("#auth-button");
    button.classList.toggle("locked", authState.required && !authState.authenticated);
    button.title = authState.authenticated && authState.user
      ? `${authState.user.name} · sign out`
      : authState.required ? "Choose a workspace role" : "Local workspace";
    button.textContent = authState.user?.name?.slice(0, 1) || (authState.required ? "↗" : "M");
    $("#current-user-label").textContent = authState.user?.name || (authState.required ? "Sign in" : "Local workspace");
    const demoRole = $("#demo-role");
    demoRole.hidden = !authState.demoMode;
    updateDemoRoleControls();
    applyPermissions();
  } catch {
    authState.required = false;
  }
}

function demoFaceVerificationKey() {
  const user = authState.user;
  return `manyavar-demo-face-verified:${user?.role || ""}:${user?.area_manager_id || ""}`;
}

function openDemoFaceCheck() {
  $("#role-auth-controls").hidden = true;
  $("#face-demo-panel").hidden = false;
  $("#auth-close").hidden = true;
  $("#auth-title").textContent = "Complete the camera login demo.";
  $("#auth-description").textContent = "Your demo role is selected. Opening the camera completes this non-biometric demo sign-in.";
  sessionStorage.removeItem("manyavar-demo-face-template-v1");
  if (!$("#auth-dialog").open) $("#auth-dialog").showModal();
}

function stopFaceCamera() {
  const video = $("#face-video");
  if (video.srcObject) {
    video.srcObject.getTracks().forEach((track) => track.stop());
    video.srcObject = null;
  }
  video.hidden = true;
}

function finishDemoFaceCheck() {
  stopFaceCamera();
  $("#auth-dialog").close();
  $("#role-auth-controls").hidden = false;
  $("#face-demo-panel").hidden = true;
  loadDashboard();
}

$("#face-start").addEventListener("click", async () => {
  const button = $("#face-start");
  button.disabled = true;
  $("#face-status").textContent = "Opening the camera…";
  try {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("Camera access is unavailable. Use HTTPS or localhost, or continue without the camera.");
    }
    const video = $("#face-video");
    video.srcObject = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user" },
      audio: false,
    });
    video.hidden = false;
    await video.play();
    sessionStorage.setItem(demoFaceVerificationKey(), "demo-approved");
    $("#face-status").textContent = "Face approved — demo only. Entering workspace…";
    window.setTimeout(finishDemoFaceCheck, 900);
  } catch (error) {
    stopFaceCamera();
    $("#face-status").textContent = error.message;
    button.disabled = false;
  }
});

$("#face-skip").addEventListener("click", () => {
  sessionStorage.setItem(demoFaceVerificationKey(), "camera-skipped");
  $("#face-status").textContent = "Camera skipped. Opening the demo workspace without camera access.";
  finishDemoFaceCheck();
});

$("#face-sign-out").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
    sessionStorage.removeItem(apiSessionTokenKey);
    Object.keys(sessionStorage).filter((key) => key.startsWith("manyavar-demo-face-verified:"))
      .forEach((key) => sessionStorage.removeItem(key));
    window.location.reload();
  } catch (error) {
    $("#face-status").textContent = error.message;
  }
});

function can(permission) {
  return !authState.required || Boolean(authState.user?.permissions?.includes(permission));
}

function applyPermissions() {
  $("#top-upload-button").hidden = !can("upload");
  $("#run-button").hidden = !can("run");
  $("#welcome-upload").hidden = !can("upload");
  $("#inline-upload").hidden = !can("upload");
  $("#choose-file").hidden = !can("upload");
  $("#chat-form").hidden = !can("ask");
  $("#dropzone").style.pointerEvents = can("upload") ? "" : "none";
  $("#dropzone").style.opacity = can("upload") ? "" : ".55";
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.style.background = error ? "rgba(133,48,42,.97)" : "";
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 3800);
}

function showView(name) {
  state.currentView = name;
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `${name}-view`));
  $$(".nav-link").forEach((link) => link.classList.toggle("active", link.dataset.view === name));
  const labels = {
    overview: "Overview", logistics: "Logistics", moves: "Move approvals", stores: "Store network",
    products: "Product intelligence", bills: "Bill analyzer", model: "Demand & AI", data: "Data & rules",
  };
  $("#breadcrumb-current").textContent = labels[name] || labels.overview;
  if (name === "logistics") refreshLogistics();
  if (name === "moves") refreshMoves();
  if (name === "products") refreshProducts();
  if (name === "bills") refreshBillAnalysis();
  if (name === "stores" && state.selectedStore) loadStoreDetail(state.selectedStore);
}

function updateConnection(connected, hasData) {
  $("#system-status-text").textContent = connected
    ? (hasData ? "Recommendation engine ready" : "Ready for dataset")
    : "API connection unavailable";
  $(".status-dot").style.background = connected ? (hasData ? "#93ad82" : "#c69b58") : "#c46d56";
  $("#last-run").textContent = state.summary?.generated_at
    ? `Updated ${new Date(state.summary.generated_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}`
    : (connected ? "Waiting for dataset" : "Service unavailable");
}

function updateSummary() {
  const data = state.summary;
  if (!data?.has_data) {
    $("#data-period").textContent = "Connecting to the backend-computed demo dataset";
    $("#data-source-name").textContent = "Loading calculated demo data…";
    $(".source-dot").classList.remove("ready");
    $("#kpi-p1").textContent = "Upload data for recommendations";
    $("#nav-pending").textContent = "—";
    $("#category-chart-total").textContent = "—";
    return;
  }
  $("#welcome-banner").classList.add("data-loaded");
  $("#data-period").textContent = data.period_start && data.period_end
    ? `${data.is_demo_data ? "Synthetic demo · " : ""}Stock snapshot · ${new Date(`${data.period_end}T00:00:00`).toLocaleDateString("en-IN", { dateStyle: "medium" })}`
    : "Dataset loaded";
  $("#data-source-name").textContent = `${data.source_name || "Uploaded dataset"}${data.is_demo_data ? " · SYNTHETIC DEMO" : ""} · ${number(data.stores)} stores · ${number(data.skus)} SKUs`;
  $(".source-dot").classList.add("ready");
  $("#kpi-stock").textContent = number(data.stock_units);
  $("#kpi-warehouse").textContent = number(data.warehouse_units);
  $("#kpi-revenue").textContent = shortRupees(data.revenue);
  $("#kpi-pending").textContent = number(data.pending);
  $("#kpi-p1").textContent = `${number(state.moves.filter((move) => move.priority === "P1" && move.decision === "pending").length)} urgent · ${number(data.approved)} approved`;
  $("#nav-pending").textContent = number(data.pending);
  $("#category-chart-total").textContent = `${number(data.stock_units)} UNITS`;
  renderCategoryChart(data.category_stock || []);
}

function renderCategoryChart(categories) {
  const target = $("#category-chart");
  const sorted = [...categories].sort((a, b) => b.units - a.units).slice(0, 10);
  if (!sorted.length) {
    target.classList.add("empty-chart");
    target.innerHTML = '<div class="chart-placeholder">No merchandise stock categories found.</div>';
    return;
  }
  target.classList.remove("empty-chart");
  const max = Math.max(...sorted.map((item) => item.units), 1);
  target.innerHTML = sorted.map((item) => {
    const height = Math.max(5, Math.round(item.units / max * 82));
    return `<div class="category-column"><small>${number(item.units)}</small><div class="category-bar" style="height:${height}px"></div><span title="${escapeHTML(item.category)}">${escapeHTML(item.category)}</span></div>`;
  }).join("");
}

function renderInsights() {
  const target = $("#insights-list");
  if (!state.insights.length) {
    target.innerHTML = '<div class="empty-note">Your store and stock signals appear here once data is loaded.</div>';
    return;
  }
  target.innerHTML = state.insights.slice(0, 4).map((item) => `
    <div class="insight-item ${escapeHTML(item.severity)}">
      <i class="insight-bullet"></i><div><strong>${escapeHTML(item.title)}</strong><span>${escapeHTML(item.detail)}</span></div>
    </div>`).join("");
}

function moveTitle(move) {
  return `${move.style_name || move.style_id || move.sku_id}${move.size ? ` · ${move.size}` : ""}`;
}
function moveType(move) {
  return ({
    WAREHOUSE_TO_STORE: "WAREHOUSE → STORE",
    STORE_TO_STORE: "STORE → STORE",
    STORE_TO_WAREHOUSE: "STORE → WAREHOUSE",
  })[move.move_type] || move.move_type;
}

function movementLocation(move, endpoint) {
  const locationId = move[`${endpoint}_location`];
  const name = move[`${endpoint}_store_name`] || locationId;
  const city = [move[`${endpoint}_store_city`], move[`${endpoint}_store_state`]]
    .filter(Boolean).join(", ");
  const address = move[`${endpoint}_store_address`];
  const mapUrl = move[`${endpoint}_store_maps_url`];
  const locatorUrl = move[`${endpoint}_store_locator_url`];
  const locationUrl = mapUrl || locatorUrl;
  return `<span class="route-location"><strong>${escapeHTML(name)}</strong><small>${escapeHTML(locationId)}${city ? ` · ${escapeHTML(city)}` : ""}</small>${address ? `<small>${escapeHTML(address)}</small>` : ""}${locationUrl ? `<a href="${escapeHTML(locationUrl)}" target="_blank" rel="noopener noreferrer">Open location ↗</a>` : ""}</span>`;
}

function moveCard(move, index) {
  const picture = illustrativePhoto(move.sku_id || index);
  const validationChecks = (move.validation_checks || []).map((check) => `
    <div class="rule-check ${escapeHTML(check.status)}">
      <strong>${escapeHTML(check.rule)} · ${escapeHTML(
        check.status === "not_applicable" ? "N/A" : check.status.toUpperCase(),
      )}</strong><span>${escapeHTML(check.detail)}</span>
    </div>`).join("");
  const actionButtons = move.decision === "pending" && can("approve")
    ? `<div class="action-buttons"><button class="approve-button" data-decision="approved" data-id="${escapeHTML(move.id)}">✓ Approve</button><button class="reject-button" data-decision="rejected" data-id="${escapeHTML(move.id)}">Decline</button></div>`
    : `<span class="move-badge decision-badge">${move.decision === "pending" ? "PENDING REVIEW" : move.decision === "approved" ? "APPROVED" : "DECLINED"}</span>`;
  return `<article class="move-card">
    <div class="move-photo"><img loading="lazy" src="${picture}" alt="Illustrative Indian occasionwear image, not an exact SKU photograph" onerror="this.style.display='none'"></div>
    <div class="move-copy">
      <div class="move-topline"><span class="move-badge ${move.priority === "P1" ? "p1" : ""}">${escapeHTML(move.priority)}</span><span class="move-badge">${escapeHTML(moveType(move))}</span>${move.requires_human_approval ? `<span class="move-badge p1">HUMAN APPROVAL · ${escapeHTML(rupees(move.approval_threshold_mrp))}+</span>` : ""}<span class="move-badge decision-badge">${escapeHTML(move.decision || "pending").toUpperCase()}</span></div>
      <h3>${escapeHTML(moveTitle(move))}</h3>
      <div class="move-sub">${escapeHTML(move.category)} · ${escapeHTML(move.sku_id)}</div>
      <div class="move-route">${movementLocation(move, "from")}<i>→</i>${movementLocation(move, "to")}${move.route_maps_url ? `<a class="route-map-link" href="${escapeHTML(move.route_maps_url)}" target="_blank" rel="noopener noreferrer">Open driving route ↗</a>` : ""}</div>
      <div class="move-reason">${escapeHTML(move.reason)}</div>
      <div class="decision-logic">
        <div><span>STORE FIT</span><strong>${number(move.store_affinity_score)} / 100</strong></div>
        <div><span>SELL-FIT ESTIMATE</span><strong>${number(move.sell_probability_pct)}%</strong></div>
        <div><span>CAPACITY LEFT</span><strong>${number(move.target_capacity_remaining_units)} units</strong></div>
        <div><span>14-DAY SELL-THROUGH</span><strong>${number(move.estimated_sell_through_units_14d)} units</strong></div>
        <small>${escapeHTML(move.store_fit_reason || "Store-fit signals are unavailable.")}</small>
      </div>
      ${validationChecks ? `<details class="validation-detail"><summary>Rule checks · ${move.validation_checks.length} conditions evaluated</summary><div>${validationChecks}</div></details>` : ""}
      <div class="move-meta"><span>ARRIVES <strong>${escapeHTML(new Date(`${move.arrival_date}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" }))}</strong></span><span>TRANSIT <strong>${number(move.lead_time_days)} days</strong></span><span>VELOCITY <strong>${escapeHTML(move.daily_velocity)} units/day</strong></span><span>SELL-FIT ESTIMATE <strong>${move.sell_probability_pct == null ? "—" : `${move.sell_probability_pct}% · affinity ${move.store_affinity_score || 0}`}</strong></span><span>MRP VALUE <strong>${rupees(move.move_value_mrp)}</strong></span><span title="${escapeHTML(move.margin_basis)}">${move.estimated_gross_profit_14d == null ? "GROSS PROFIT EST." : `${rupees(move.estimated_gross_profit_14d)} · ${move.estimated_gross_margin_pct}%`}<strong>14-day estimate · ${number(move.estimated_sell_through_units_14d)} expected units</strong></span>${move.stock_age_days ? `<span>OLDEST UNIT <strong>${number(move.stock_age_days)} days</strong></span>` : ""}${move.donor_cover_after_days ? `<span>DONOR COVER <strong>${escapeHTML(move.donor_cover_after_days)} days</strong></span>` : ""}</div>
    </div>
    <div class="move-actions"><div class="move-qty">${number(move.qty)}<small>units</small></div>${actionButtons}</div>
  </article>`;
}

function logisticsMoveCard(move, status) {
  const isPending = status === "awaiting_approval";
  const isCompleted = status === "completed";
  const completion = move.completion;
  const action = isPending
    ? can("approve")
      ? `<div class="action-buttons"><button class="approve-button" data-decision="approved" data-id="${escapeHTML(move.id)}">Validate &amp; approve</button><button class="reject-button" data-decision="rejected" data-id="${escapeHTML(move.id)}">Decline</button></div><small class="validation-hint">Approval confirms the displayed SKU, route and quantity.</small>`
      : '<span class="move-badge decision-badge">VALIDATOR REVIEW REQUIRED</span>'
    : isCompleted
      ? `<div class="completion-audit"><strong>Received ${number(completion.received_qty)} units</strong><span>Validated by ${escapeHTML(completion.actor)} · ${escapeHTML(new Date(completion.completed_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }))}</span>${completion.note ? `<span>${escapeHTML(completion.note)}</span>` : ""}</div>`
      : `<form class="completion-form" data-complete-id="${escapeHTML(move.id)}">
          <label>Units received <input type="number" name="received_qty" min="0" step="1" value="${number(move.qty)}" required></label>
          <label class="verification-check"><input type="checkbox" name="sku_verified" required> SKU and size match</label>
          <label class="verification-check"><input type="checkbox" name="route_verified" required> Origin and destination match</label>
          <label>Receipt note <input type="text" name="note" maxlength="500" placeholder="Optional"></label>
          <button class="button button-rust" type="submit">Confirm receipt &amp; complete</button>
        </form>`;
  return `<article class="logistics-card ${isCompleted ? "is-completed" : ""}">
    <div class="logistics-card-heading"><div><span class="move-badge ${move.priority === "P1" ? "p1" : ""}">${escapeHTML(move.priority)}</span><span class="move-badge">${escapeHTML(moveType(move))}</span></div><span class="logistics-state ${isCompleted ? "complete" : isPending ? "ready" : ""}">${isCompleted ? "COMPLETED" : isPending ? "NEXT TO VALIDATE" : "ONGOING"}</span></div>
    <h3>${escapeHTML(moveTitle(move))}</h3>
    <div class="move-sub">${escapeHTML(move.category)} · SKU ${escapeHTML(move.sku_id)}</div>
    <div class="logistics-route">${movementLocation(move, "from")}<i>→</i>${movementLocation(move, "to")}${move.route_maps_url ? `<a class="route-map-link" href="${escapeHTML(move.route_maps_url)}" target="_blank" rel="noopener noreferrer">Open driving route ↗</a>` : ""}<small>Expected ${escapeHTML(new Date(`${move.arrival_date}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }))}</small></div>
    <div class="logistics-evidence"><span>RECOMMENDED QTY<strong>${number(move.qty)} units</strong></span><span>MRP VALUE<strong>${rupees(move.move_value_mrp)}</strong></span><span>DEMAND RATE<strong>${escapeHTML(move.daily_velocity)} / day</strong></span><span title="${escapeHTML(move.margin_basis)}">GROSS PROFIT · 14D<strong>${move.estimated_gross_profit_14d == null ? "—" : `${rupees(move.estimated_gross_profit_14d)} · ${move.estimated_gross_margin_pct}%`}</strong></span></div>
    <p class="logistics-reason">${escapeHTML(move.reason)}</p>
    <div class="logistics-action">${action}</div>
  </article>`;
}

async function refreshLogistics() {
  const target = $("#logistics-next");
  target.innerHTML = '<div class="detail-loading"><span class="spinner"></span><span>Loading the automated move queue…</span></div>';
  try {
    const result = await api("/api/logistics");
    const allMoves = [result.next_move, ...result.ongoing, ...result.completed].filter(Boolean);
    state.moves = allMoves;
    $("#logistics-summary").innerHTML = [
      ["AWAITING VALIDATION", number(result.awaiting_count)],
      ["ONGOING", number(result.ongoing.length)],
      ["COMPLETED", number(result.completed.length)],
    ].map(([label, value]) => `<div class="store-stat"><span>${label}</span><strong>${value}</strong></div>`).join("");
    target.innerHTML = result.next_move
      ? logisticsMoveCard(result.next_move, "awaiting_approval")
      : '<div class="empty-state logistics-empty"><span>✳</span><h3>No move is waiting for validation.</h3><p>The automation has no pending safe recommendations. Refresh after the next analysis run.</p></div>';
    $("#logistics-ongoing-count").textContent = number(result.ongoing.length);
    $("#logistics-ongoing").innerHTML = result.ongoing.length
      ? result.ongoing.map((move) => logisticsMoveCard(move, "ongoing")).join("")
      : '<div class="empty-note">Approved moves waiting for receipt will appear here.</div>';
    $("#logistics-completed-count").textContent = number(result.completed.length);
    $("#logistics-completed").innerHTML = result.completed.length
      ? result.completed.map((move) => logisticsMoveCard(move, "completed")).join("")
      : '<div class="empty-note">Human-validated completed moves will appear here.</div>';
  } catch (error) {
    target.innerHTML = `<div class="empty-state"><h3>Logistics status is not available.</h3><p>${escapeHTML(error.message)}</p></div>`;
  }
}

async function refreshMoves() {
  const params = new URLSearchParams();
  const type = $("#move-type-filter")?.value;
  const decision = $("#move-decision-filter")?.value;
  const search = $("#move-search")?.value.trim();
  if (type) params.set("move_type", type);
  if (decision) params.set("decision", decision);
  if (search) params.set("search", search);
  try {
    const result = await api(`/api/recommendations?${params}`);
    state.moves = result.items;
    $("#move-count").textContent = `${number(result.total)} recommendation${result.total === 1 ? "" : "s"}`;
    $("#move-list").innerHTML = result.items.length
      ? result.items.map(moveCard).join("")
      : `<div class="empty-state"><span>✳</span><h3>Nothing in this view.</h3><p>${state.summary?.has_data ? "Try another filter, or check back after the next analysis." : "Upload the provided workbook to create the daily move list."}</p><button class="button button-rust" data-upload>Upload dataset</button></div>`;
  } catch (error) {
    $("#move-list").innerHTML = `<div class="empty-state"><h3>Move list is not ready.</h3><p>${escapeHTML(error.message)}</p><button class="button button-rust" data-upload>Upload dataset</button></div>`;
  }
}

function renderStores() {
  const rows = state.stores;
  const totalUnits = rows.reduce((sum, item) => sum + item.stock_units, 0);
  const occupied = rows.filter((item) => item.capacity_pct >= 90).length;
  const totalFootfall = rows.reduce((sum, item) => sum + item.footfall, 0);
  const recentBills = rows.reduce((sum, item) => sum + item.recent_bills, 0);
  const conversion = totalFootfall ? `${(100 * recentBills / totalFootfall).toFixed(2)}%` : "—";
  $("#store-summary-strip").innerHTML = [
    ["CONNECTED STORES", number(rows.length)],
    ["NETWORK STOCK", `${number(totalUnits)} units`],
    ["VISIT → BILL", conversion],
    ["NEAR CAPACITY", `${number(occupied)} stores`],
  ].map(([label, value]) => `<div class="store-stat"><span>${label}</span><strong>${value}</strong></div>`).join("");
  $("#store-table").innerHTML = rows.length ? rows.map((store) => `
    <tr class="clickable-row ${state.selectedStore === store.store_id ? "selected-row" : ""}" data-store="${escapeHTML(store.store_id)}"><td>${escapeHTML(store.store_id)}<div class="move-sub">${escapeHTML(store.store_name || store.city)}${store.address ? ` · ${escapeHTML(store.address)}` : ""}</div></td>
    <td><span class="grade-tag tier-tag">${escapeHTML(store.tier || "—")}</span></td>
    <td>${gradeTag(store.sell_through_grade, store.sell_through_grade_basis)}</td>
    <td>${escapeHTML(store.area_manager_id || "—")}</td><td>${number(store.stock_units)}</td>
    <td class="capacity-cell">${store.capacity_pct}%<div class="capacity-track"><i style="width:${Math.min(100, store.capacity_pct)}%"></i></div></td>
    <td>${rupees(store.sales_revenue)}</td><td>${number(store.units_sold)}</td>
    <td>${number(store.bill_count)}</td>
    <td>${number(store.footfall)}</td><td>${store.visit_to_bill_pct}%</td><td>${store.avg_staff_present}</td></tr>`).join("")
    : '<tr><td colspan="12" class="empty-cell">Upload your dataset to see store-level performance.</td></tr>';
}

function renderModel() {
  const model = state.summary?.model;
  const optimizer = state.summary?.optimizer;
  if (!model) return;
  const trained = model.status === "trained";
  const selectedLearned = model.forecast_source === "lightgbm_tweedie";
  $("#model-status").textContent = selectedLearned ? "LIGHTGBM SELECTED"
    : trained ? "MODEL CHECKED · RATE FALLBACK" : "AVAILABILITY-CORRECTED FALLBACK";
  $("#model-name").textContent = selectedLearned ? "LightGBM · Tweedie"
    : trained ? "Recent availability-corrected rate" : "Availability-corrected baseline";
  $("#model-description").textContent = model.reason || (selectedLearned
    ? "The pooled gradient-boosted Tweedie forecast beat the recent-rate baseline on a time-ordered holdout."
    : trained ? "LightGBM was trained, but the recent availability-corrected rate performed better on holdout, so the safer baseline drives moves."
      : "Observed sales are divided by SKU in-stock days; zero sales with no in-stock exposure are not interpreted as slow demand.");
  const validation = model.validation || {};
  $("#model-facts").innerHTML = `<span>TRAINING ROWS<strong>${number(model.training_rows || 0)}</strong></span><span>HORIZON<strong>${number(model.forecast_horizon_days || 14)} days</strong></span><span>HOLDOUT WAPE<strong>${validation.wape == null ? "—" : `${(validation.wape * 100).toFixed(1)}%`}</strong></span><span>RECENT-RATE WAPE<strong>${validation.naive_wape == null ? "—" : `${(validation.naive_wape * 100).toFixed(1)}%`}</strong></span>`;
  $("#optimizer-name").textContent = optimizer?.optimizer === "scipy_milp" ? "Binary MILP · SciPy" : "Constrained proposal selection";
}

function populateAnalyticsFilters(categoryNames = []) {
  const categories = [...new Set(categoryNames.filter(Boolean))].sort();
  $("#product-category-filter").innerHTML = '<option value="">All categories</option>'
    + categories.map((item) => `<option value="${escapeHTML(item)}">${escapeHTML(item)}</option>`).join("");
  const storeOptions = state.stores.map((store) =>
    `<option value="${escapeHTML(store.store_id)}">${escapeHTML(store.store_id)} · ${escapeHTML(store.city)}</option>`
  ).join("");
  ["product-store-filter", "bill-store-filter"].forEach((id) => {
    const select = $(`#${id}`);
    const selected = select.value;
    select.innerHTML = `<option value="">${id === "bill-store-filter" ? "All stores" : "All stores"}</option>${storeOptions}`;
    if ([...select.options].some((option) => option.value === selected)) select.value = selected;
  });
}

function renderTrendChart(container, points, valueKey, color, description) {
  if (!points?.length) {
    container.innerHTML = '<div class="chart-loading">No weekly observations are available.</div>';
    return;
  }
  const width = 720;
  const height = 190;
  const left = 38;
  const right = 18;
  const top = 14;
  const bottom = 34;
  const values = points.map((point) => Number(point[valueKey] || 0));
  const maximum = Math.max(...values, 1);
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const x = (index) => left + (points.length <= 1 ? plotWidth / 2 : index * plotWidth / (points.length - 1));
  const y = (value) => top + plotHeight - value / maximum * plotHeight;
  const path = values.map((value, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ");
  const area = `${path} L${x(points.length - 1)},${top + plotHeight} L${x(0)},${top + plotHeight} Z`;
  const grid = [0, 0.33, 0.66, 1].map((fraction) => {
    const rowY = top + plotHeight * fraction;
    return `<line x1="${left}" y1="${rowY}" x2="${width - right}" y2="${rowY}" class="chart-gridline"/><text x="${left - 7}" y="${rowY + 3}" text-anchor="end" class="chart-axis">${number(maximum * (1 - fraction))}</text>`;
  }).join("");
  const step = Math.max(1, Math.ceil(points.length / 6));
  const labels = points.map((point, index) => index % step === 0 || index === points.length - 1
    ? `<text x="${x(index)}" y="${height - 8}" text-anchor="middle" class="chart-axis">${escapeHTML(point.week?.slice(5) || "")}</text>`
    : "").join("");
  const dots = values.map((value, index) => `<circle cx="${x(index)}" cy="${y(value)}" r="3" fill="${color}"><title>${escapeHTML(points[index].week)} · ${number(value)} ${escapeHTML(description)}</title></circle>`).join("");
  container.innerHTML = `<svg class="trend-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHTML(description)} trend">
    ${grid}<path d="${area}" fill="${color}" opacity=".12"></path><path d="${path}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></path>${dots}${labels}
  </svg>`;
}

async function refreshProducts() {
  const target = $("#product-table");
  target.innerHTML = '<tr><td colspan="10"><div class="table-skeleton"><i></i><i></i><i></i></div></td></tr>';
  const params = new URLSearchParams({ sort: $("#product-sort").value, limit: "500" });
  const query = $("#product-search").value.trim();
  const category = $("#product-category-filter").value;
  const store = $("#product-store-filter").value;
  if (query) params.set("q", query);
  if (category) params.set("category", category);
  if (store) params.set("store_id", store);
  if ($("#product-unsold-only").checked) params.set("unsold_only", "true");
  try {
    const result = await api(`/api/products?${params}`);
    state.products = result.items;
    $("#product-count").textContent = `${number(result.total)} products`;
    if (!result.items.length) {
      target.innerHTML = '<tr><td colspan="10" class="empty-cell">No products match these filters.</td></tr>';
      return;
    }
    target.innerHTML = result.items.map((item) => `<tr class="clickable-row ${state.selectedProduct === item.sku_id ? "selected-row" : ""}" data-product="${escapeHTML(item.sku_id)}">
      <td><strong>${escapeHTML(item.style_name || item.style_id || item.sku_id)}</strong><div class="move-sub">${escapeHTML(item.sku_id)}</div></td>
      <td>${escapeHTML(item.category || "—")}</td><td>${escapeHTML(item.size || "—")}</td>
      <td>${gradeTag(item.sell_through_grade, item.sell_through_grade_basis)}</td>
      <td>${number(item.store_metrics?.sales_units ?? item.total_sold_units)}</td>
      <td><span class="metric-pill ${item.store_metrics?.recent_units || item.recent_sold_units ? "positive" : ""}">${number(item.store_metrics?.recent_units ?? item.recent_sold_units)}</span></td>
      <td>${number(item.store_metrics?.stock_units ?? item.store_stock_units)}</td><td>${number(item.warehouse_units)}</td>
      <td>${escapeHTML(item.best_selling_store || "No sales yet")}</td>
      <td>${item.placement_suggestion?.destination_location
        ? `<span class="placement-cell"><strong>${escapeHTML(item.placement_suggestion.destination_store_name || item.placement_suggestion.destination_location)}</strong><small>${escapeHTML(item.placement_suggestion.status === "ready_for_review" ? "READY FOR REVIEW" : "ADVISORY · VALIDATE RULES")}</small></span>`
        : `<span class="unknown-tag">${escapeHTML(item.placement_suggestion?.status === "not_applicable" ? "NOT MERCHANDISE" : "MONITOR · NO PROVEN DESTINATION")}</span>`}</td></tr>`).join("");
    const selected = result.items.find((item) => item.sku_id === state.selectedProduct) || result.items[0];
    state.selectedProduct = selected.sku_id;
    await loadProductDetail(selected.sku_id);
  } catch (error) {
    target.innerHTML = `<tr><td colspan="10" class="empty-cell">${escapeHTML(error.message)}</td></tr>`;
  }
}

async function loadProductDetail(skuId) {
  const detail = $("#product-detail");
  detail.setAttribute("aria-busy", "true");
  detail.innerHTML = '<div class="detail-loading"><span class="spinner"></span><span>Bringing the product history together…</span></div>';
  try {
    const focusStore = $("#product-store-filter").value;
    const query = focusStore ? `?store_id=${encodeURIComponent(focusStore)}` : "";
    const result = await api(`/api/products/${encodeURIComponent(skuId)}${query}`);
    const product = result.product;
    state.selectedProduct = skuId;
    const photo = illustrativePhoto(product.style_id || product.sku_id);
    const placement = result.placement_suggestion || product.placement_suggestion;
    const trendRows = result.sales_trend.map((row, index) => ({
      week: row.week,
      units: row.units,
      stock: result.stock_trend[index]?.units || 0,
    }));
    detail.innerHTML = `<div class="product-hero">
      <div class="product-image"><img src="${photo}" alt="Illustrative ${escapeHTML(product.category || "occasionwear")} image, not an exact SKU photograph" loading="lazy" onerror="this.parentElement.classList.add('image-fallback');this.remove()"><span>${escapeHTML(product.category || "COLLECTION")}</span></div>
      <div class="product-copy"><div class="product-tags"><span class="grade-tag">${asCore(product) ? "CORE STYLE" : "SEASONAL STYLE"}</span>${gradeTag(product.sell_through_grade, product.sell_through_grade_basis)}<span class="grade-tag sku-tag">${escapeHTML(product.sku_id)}</span></div>
        <h2>${escapeHTML(product.style_name || product.style_id || product.sku_id)}</h2><p>${escapeHTML(product.description)}</p>
        <div class="product-price">${rupees(product.mrp)} <small>MRP · ${escapeHTML(product.size || "Free size")}</small></div>
      </div></div>
      <section class="placement-panel ${placement?.is_executable ? "placement-ready" : ""}">
        <div><span class="eyebrow muted-eyebrow">RIGHT PLACE · PRODUCT PLACEMENT</span>
          <h3>${placement?.destination_location
            ? `${escapeHTML(placement.destination_store_name || placement.destination_location)}${placement.destination_city ? ` · ${escapeHTML(placement.destination_city)}` : ""}`
            : "No proven destination yet"}</h3>
          <p>${escapeHTML(placement?.reason || "Keep under review until a store has verified demand and capacity.")}</p>
        </div>
        <div class="placement-side">${placement?.destination_location
          ? `<span class="placement-status">${escapeHTML(placement.status === "ready_for_review" ? "RULE-CHECKED MOVE" : "ADVISORY LEAD")}</span>${placement.destination_maps_url ? `<a href="${escapeHTML(placement.destination_maps_url)}" target="_blank" rel="noopener noreferrer">Open destination ↗</a>` : ""}${placement.move_id ? '<a href="#" data-go="moves">Review recommendation →</a>' : ""}`
          : '<span class="placement-status">MONITOR</span>'}</div>
      </section>
      <div class="product-stats">
        <div><span>ALL-TIME UNITS</span><strong>${number(product.total_sold_units)}</strong></div>
        <div><span>SALES VALUE</span><strong>${shortRupees(product.sales_revenue)}</strong></div>
        <div><span>STORE STOCK</span><strong>${number(product.store_stock_units)}</strong></div>
        <div><span>WAREHOUSE</span><strong>${number(product.warehouse_units)}</strong></div>
        <div><span>RECENT VELOCITY</span><strong>${product.recent_velocity == null ? "Unknown" : `${product.recent_velocity} / day`}</strong></div>
        <div><span>OLDEST UNIT AGE</span><strong>${number(product.oldest_stock_age_days)} days</strong></div>
        <div><span>GROSS MARGIN PROXY</span><strong>${product.gross_margin_pct_estimate == null ? "Unknown" : `${product.gross_margin_pct_estimate}% · ${rupees(product.unit_gross_margin_estimate)}/unit`}</strong></div>
      </div>
      <div class="chart-pair"><section><div class="mini-heading"><strong>Weekly sell-through</strong><span>12 weeks</span></div><div id="product-sales-chart" class="trend-chart"></div></section>
        <section><div class="mini-heading"><strong>Closing stock</strong><span>Store network</span></div><div id="product-stock-chart" class="trend-chart"></div></section></div>
      <div class="panel-heading store-performance-heading"><div><span class="eyebrow muted-eyebrow">SAME SKU · EVERY STORE</span><h3>Where the style sells and where it waits</h3></div></div>
      <div class="table-wrap"><table class="data-table compact-table"><thead><tr><th>STORE</th><th>SELL GRADE</th><th>RECENT UNITS</th><th>12-WK IN-STOCK DAYS</th><th>RECENT VELOCITY</th><th>ON HAND</th><th>IN TRANSIT</th><th>COVER</th><th>OLDEST UNIT</th><th>LAST SALE</th><th>DEMAND / STOCK SIGNAL</th></tr></thead><tbody>${product.store_performance.map((store) => `<tr><td><strong>${escapeHTML(store.store_name || store.store_id)}</strong><div class="move-sub">${escapeHTML(store.store_id)} · ${escapeHTML(store.city)} · Tier ${escapeHTML(store.tier)}</div>${store.google_maps_url ? `<a class="table-location-link" href="${escapeHTML(store.google_maps_url)}" target="_blank" rel="noopener noreferrer">Open location ↗</a>` : ""}</td><td>${gradeTag(store.sell_through_grade)}</td><td>${number(store.recent_units)}</td><td>${number(store.recent_in_stock_days)}</td><td>${store.recent_velocity == null ? '<span class="unknown-tag">NO EXPOSURE</span>' : `${store.recent_velocity} / day`}</td><td>${number(store.on_hand)}</td><td>${number(store.in_transit)}</td><td>${store.cover_days == null ? "—" : `${number(store.cover_days)} days`}</td><td>${store.stock_age_days == null ? "—" : `${number(store.stock_age_days)} days`}</td><td>${escapeHTML(store.last_sale_date || "No sale recorded")}</td><td>${store.stock_risk === "dead_stock_70_day_no_sale" ? '<span class="risk-tag">DEAD STOCK · 70D</span>' : store.stock_risk === "seasonal_7_day_no_sale" ? '<span class="risk-tag">FESTIVAL · 7D</span>' : store.stock_risk === "no_sales_after_new_design" ? '<span class="risk-tag">NEWER DESIGN</span>' : "—"}</td></tr>`).join("")}</tbody></table></div>
      <div class="data-disclaimer">${product.unsold_locations.length ? `${product.unsold_locations.length} location(s) have stock and recorded availability but no sales in the recent 12-week window.` : "No stock-held location has both recorded recent availability and zero sales."} ${escapeHTML(product.margin_basis)}. Selling prices and MRP are GST-inclusive and cost is illustrative, so gross margin is a directional pre-tax/overhead estimate, not realized profit. ${escapeHTML(result.similarity_notice)}</div>
      <section class="similar-products"><div class="mini-heading"><strong>Similar styles selling now</strong><span>Demand leads, not automatic transfer approvals</span></div>
        <div class="similar-product-list">${result.similar_products.map((item) => `<div><span><strong>${escapeHTML(item.style_name || item.sku_id)}</strong><small>${escapeHTML(item.sku_id)} · ${escapeHTML(item.size)} · ${escapeHTML(item.demand_store || "Network")} · ${escapeHTML(item.similarity_basis)}</small></span><b>${number(item.demand_store_recent_units)} <small>recent units</small></b></div>`).join("") || '<span class="empty-note">No same-category, same-size styles in the 30% price band have recorded recent demand.</span>'}</div>
        <p>Recommendations use only catalogue fields supplied for fabric, cloth type and colour; missing fields are never guessed.</p></section>`;
    renderTrendChart($("#product-sales-chart"), trendRows, "units", "#b97542", "units sold");
    renderTrendChart($("#product-stock-chart"), trendRows, "stock", "#26847c", "units in stock");
  } catch (error) {
    detail.innerHTML = `<div class="empty-note">${escapeHTML(error.message)}</div>`;
  } finally {
    detail.removeAttribute("aria-busy");
  }
}

function asCore(product) {
  return Number(product.is_core_style) === 1;
}

async function loadStoreDetail(storeId) {
  const target = $("#store-detail");
  target.setAttribute("aria-busy", "true");
  target.innerHTML = '<div class="detail-loading"><span class="spinner"></span><span>Loading this store’s selling picture…</span></div>';
  try {
    const result = await api(`/api/stores/${encodeURIComponent(storeId)}`);
    const store = result.store;
    target.innerHTML = `<div class="panel-heading"><div><span class="eyebrow muted-eyebrow">${escapeHTML(store.tier)} · ${escapeHTML(store.city)}</span><h3>${escapeHTML(store.store_id)} · ${escapeHTML(store.store_name || "Store detail")}</h3><span class="store-address">${escapeHTML(store.address || "")}${store.google_maps_url ? ` · <a href="${escapeHTML(store.google_maps_url)}" target="_blank" rel="noopener noreferrer">Open in Maps ↗</a>` : ""}${store.store_locator_url ? ` · <a href="${escapeHTML(store.store_locator_url)}" target="_blank" rel="noopener noreferrer">Official listing ↗</a>` : ""}</span></div><span class="grade-tag">${number(store.capacity_pct)}% CAPACITY</span></div>
      <div class="store-detail-kpis">
        <div><span>SALES VALUE</span><strong>${rupees(store.sales_revenue)}</strong></div>
        <div><span>UNITS SOLD</span><strong>${number(store.units_sold)}</strong></div>
        <div><span>BILLS · ALL DATA</span><strong>${number(store.bill_count)}</strong></div>
        <div><span>RECENT BILLS</span><strong>${number(store.recent_bills)}</strong></div>
        <div><span>AVERAGE BILL</span><strong>${rupees(store.average_bill_value)}</strong></div>
        <div><span>FOOTFALL</span><strong>${number(store.footfall)}</strong></div>
        <div><span>VISIT → BILL</span><strong>${number(store.visit_to_bill_pct)}%</strong></div>
        <div><span>AVG STAFF PRESENT</span><strong>${number(store.avg_staff_present)}</strong></div>
        <div><span>SELL-THROUGH GRADE</span><strong title="${escapeHTML(store.sell_through_grade_basis)}">${escapeHTML(store.sell_through_grade)}</strong></div>
        <div><span>RECENT VELOCITY</span><strong>${store.recent_velocity == null ? "Unknown" : `${store.recent_velocity} units / SKU-day`}</strong></div>
      </div>
      <div class="store-detail-grid"><div><div class="mini-heading"><strong>Weekly sell-through</strong><span>Recent 12 weeks</span></div><div id="store-sales-chart" class="trend-chart"></div></div><div><div class="mini-heading"><strong>Weekly closing stock</strong><span>Store stock</span></div><div id="store-stock-chart" class="trend-chart"></div></div></div>
      <div class="store-detail-grid"><div><div class="mini-heading"><strong>Top sellers</strong><span>Store-specific</span></div><div class="ranked-list">${result.best_sellers.map((item, index) => `<div><i>${String(index + 1).padStart(2, "0")}</i><span><strong>${escapeHTML(item.style_name || item.sku_id)}</strong><small>${escapeHTML(item.sku_id)} · ${escapeHTML(item.category)}</small></span><b>${number(item.store_metrics.recent_units)} <small>12 wk</small></b></div>`).join("") || '<span class="empty-note">No sales recorded in this window.</span>'}</div></div>
        <div><div class="mini-heading"><strong>Unsold with stock</strong><span>12-week exposure confirmed</span></div><div class="ranked-list">${result.dormant_stock.slice(0, 8).map((item) => `<div><i class="dormant-mark">!</i><span><strong>${escapeHTML(item.style_name || item.sku_id)}</strong><small>${escapeHTML(item.sku_id)} · ${number(item.store_metrics.recent_in_stock_days)} in-stock days · no sales in period</small></span><b>${number(item.store_metrics.stock_units)} <small>units</small></b></div>`).join("") || '<span class="empty-note">No exposure-confirmed dormant stock.</span>'}</div></div></div>
      <div class="store-risk-panel"><div class="mini-heading"><strong>Dead-stock / season / new-design signals</strong><span>Review before moving; availability must be confirmed</span></div>
        <div class="ranked-list">${result.stock_risk.map((item) => {
          const metric = item.store_metrics;
          const similar = item.similar_demand?.[0];
          const move = item.recommended_move;
          return `<div><i class="dormant-mark">!</i><span><strong>${escapeHTML(item.style_name || item.sku_id)}</strong><small>${escapeHTML(item.sku_id)} · ${metric.stock_risk === "dead_stock_70_day_no_sale" ? "dead stock · no sale ≥70 days" : metric.stock_risk === "seasonal_7_day_no_sale" ? "festival/wedding · no sale ≥7 days" : `newer design · launched ${escapeHTML(metric.successor_launch_date || "")}`} · last sale ${escapeHTML(metric.last_sale_date || "not recorded")}</small><small>${escapeHTML(metric.stock_risk_reason || "")}${similar ? ` Comparable demand: ${escapeHTML(similar.sku_id)} at ${escapeHTML(similar.demand_store || "another store")} (${number(similar.demand_store_recent_units)} units).` : ""}</small>${move ? `<small class="recommendation-destination">Recommended route: ${movementLocation(move, "to")} · ${number(move.qty)} units · ${number(move.sell_probability_pct)}% sell-fit estimate</small>` : '<small>No safe outbound recommendation is available under current cover, display and capacity rules.</small>'}</span><b>${number(metric.stock_units)} <small>units</small></b></div>`;
        }).join("") || '<span class="empty-note">No confirmed dead-stock or stock-risk signal with recorded availability.</span>'}</div>
        <p>Similar-style demand uses category, size and price, plus supplied material, cloth type and colour when available; it is a review lead, not an automatic move.</p></div>
      <div class="mini-heading"><strong>Category stock position</strong><span>Units on hand + in transit</span></div><div class="category-chip-list">${result.category_stock.map((item) => `<span>${escapeHTML(item.category)} <b>${number(item.units)}</b></span>`).join("")}</div>`;
    target.insertAdjacentHTML("beforeend", `<section class="store-bills"><div class="mini-heading"><strong>Recent bills</strong><span>Latest bills in the recorded footfall period</span></div>
      <div class="table-wrap"><table class="data-table compact-table"><thead><tr><th>DATE</th><th>BILL</th><th>TOP ITEM</th><th>UNITS</th><th>VALUE</th></tr></thead><tbody>${result.recent_bills.map((bill) => `<tr><td>${escapeHTML(bill.bill_date)}</td><td>${escapeHTML(bill.bill_id)}</td><td>${escapeHTML(bill.top_sku_name || "—")}<div class="move-sub">${escapeHTML(bill.top_sku_id || "")}</div></td><td>${number(bill.units)}</td><td><strong>${rupees(bill.revenue)}</strong></td></tr>`).join("") || '<tr><td colspan="5" class="empty-cell">No bills are recorded in this period.</td></tr>'}</tbody></table></div></section>`);
    renderTrendChart($("#store-sales-chart"), result.weekly_trend, "sales_units", "#456db1", "units sold");
    renderTrendChart($("#store-stock-chart"), result.weekly_trend, "stock_units", "#26847c", "units in stock");
  } catch (error) {
    target.innerHTML = `<div class="empty-note">${escapeHTML(error.message)}</div>`;
  } finally {
    target.removeAttribute("aria-busy");
  }
}

async function refreshBillAnalysis() {
  const storeId = $("#bill-store-filter").value;
  const target = $("#bill-table");
  target.innerHTML = '<tr><td colspan="5"><div class="table-skeleton"><i></i><i></i></div></td></tr>';
  try {
    const result = await api(`/api/bill-analysis${storeId ? `?store_id=${encodeURIComponent(storeId)}` : ""}`);
    const summary = result.summary;
    $("#bill-kpis").innerHTML = [
      ["RECENT BILLS", number(summary.bills)],
      ["SALES VALUE", shortRupees(summary.revenue)],
      ["UNITS SOLD", number(summary.units)],
      ["AVG BILL VALUE", rupees(summary.average_bill_value)],
    ].map(([label, value]) => `<div class="store-stat"><span>${label}</span><strong>${value}</strong></div>`).join("");
    $("#bill-product-chart").innerHTML = result.top_products.length
      ? `<div class="horizontal-bars">${result.top_products.slice(0, 8).map((item, index) => {
        const maximum = Math.max(...result.top_products.map((row) => row.store_metrics?.recent_units ?? row.recent_sold_units), 1);
        const units = item.store_metrics?.recent_units ?? item.recent_sold_units;
        const tone = ["#b97542", "#26847c", "#456db1", "#c79a48", "#8d658f", "#569b70"][index % 6];
        return `<div class="hbar-row"><span title="${escapeHTML(item.style_name || item.sku_id)}">${escapeHTML(item.style_name || item.sku_id)}</span><i><b style="width:${Math.max(3, units / maximum * 100)}%;background:${tone}"></b></i><strong>${number(units)}</strong></div>`;
      }).join("")}</div>`
      : '<div class="chart-loading">No product sales in the selected window.</div>';
    target.innerHTML = result.top_bills.length ? result.top_bills.map((bill) => `<tr><td>${escapeHTML(bill.bill_date)}</td><td>${escapeHTML(bill.store_id)}</td><td>${escapeHTML(bill.top_sku_name || bill.top_sku_id || "—")}<div class="move-sub">${escapeHTML(bill.top_sku_id || "")}</div></td><td>${number(bill.units)}</td><td><strong>${rupees(bill.revenue)}</strong></td></tr>`).join("")
      : '<tr><td colspan="5" class="empty-cell">No bills in the selected period.</td></tr>';
    const dormant = result.dormant_stock || [];
    $("#dormant-table").innerHTML = dormant.length ? dormant.map((item) => {
      const store = item.store_metrics;
      const source = item.best_other_store;
      const move = item.recommended_move;
      return `<tr><td><strong>${escapeHTML(item.style_name || item.sku_id)}</strong><div class="move-sub">${escapeHTML(item.sku_id)} · ${escapeHTML(item.category)}</div></td><td>${number(store.stock_units)}</td><td>${number(store.recent_in_stock_days)}</td><td>${store.stock_age_days == null ? "—" : `${number(store.stock_age_days)} days`}</td><td>${source ? `${escapeHTML(source.store_id)} · ${number(source.recent_units)} sold` : "No recent selling store"}</td><td>${move ? `<span class="move-ready">${escapeHTML(move.to_store_name || move.to_location)} · ${escapeHTML(move.to_store_city || "")} · ${number(move.qty)} units · ${number(move.sell_probability_pct)}% fit</span>${move.to_store_maps_url ? `<div class="move-sub"><a href="${escapeHTML(move.to_store_maps_url)}" target="_blank" rel="noopener noreferrer">Open destination ↗</a></div>` : ""}` : '<span class="unknown-tag">NO SAFE MOVE</span>'}</td></tr>`;
    }).join("") : '<tr><td colspan="6" class="empty-cell">No exposure-confirmed dormant inventory for this store.</td></tr>';
  } catch (error) {
    target.innerHTML = `<tr><td colspan="5" class="empty-cell">${escapeHTML(error.message)}</td></tr>`;
    toast(error.message, true);
  }
}

async function loadDashboard() {
  try {
    state.summary = await api("/api/summary");
    updateConnection(true, state.summary.has_data);
    updateSummary();
    if (!state.summary.has_data) return;
    const [moves, insights, stores, meta] = await Promise.all([
      api("/api/recommendations?limit=8&decision=pending"),
      api("/api/insights"),
      api("/api/stores"),
      api("/api/analytics/meta"),
    ]);
    state.moves = moves.items;
    state.insights = insights.items;
    state.stores = stores.items;
    updateSummary();
    populateAnalyticsFilters(meta.categories);
    renderInsights();
    $("#overview-moves").innerHTML = state.moves.length
      ? state.moves.slice(0, 4).map((move, index) => `<div class="preview-row"><span class="preview-priority ${move.priority === "P1" ? "urgent" : ""}">${escapeHTML(move.priority)}</span><div><strong>${escapeHTML(moveTitle(move))}</strong><span>${escapeHTML(move.from_store_name || move.from_location)} <i>→</i> ${escapeHTML(move.to_store_name || move.to_location)} · ${number(move.qty)} units</span><small>${escapeHTML(move.to_store_city || "")}${move.to_store_address ? ` · ${escapeHTML(move.to_store_address)}` : ""}</small></div><b>${escapeHTML(move.arrival_date)}</b></div>`).join("")
      : '<div class="empty-inline"><span class="empty-star">✳</span><strong>Nothing waiting for approval.</strong><span>Your next daily run will appear here.</span></div>';
    renderStores();
    renderModel();
  } catch (error) {
    updateConnection(false, false);
    toast(error.message, true);
  } finally {
    $("#app-loader").classList.add("is-hidden");
  }
}

async function uploadFile(file) {
  if (!file) return;
  if (!/\.(xlsx|zip)$/i.test(file.name)) {
    toast("Choose an Excel workbook (.xlsx) or a ZIP of CSV tables.", true);
    return;
  }
  if (file.size > 200 * 1024 * 1024) {
    toast("The upload exceeds the 200 MB limit.", true);
    return;
  }
  $("#upload-progress").hidden = false;
  $("#upload-message").textContent = `Reading ${file.name} and preparing recommendations…`;
  $$(".top-upload, #welcome-upload, #choose-file").forEach((button) => { button.disabled = true; });
  try {
    let result;
    if (window.__MANYAVAR_CONFIG__?.blobUploads) {
      $("#upload-message").textContent = `Securely uploading ${file.name} to private storage…`;
      const stored = await upload(`datasets/${Date.now()}-${file.name}`, file, {
        access: "private",
        handleUploadUrl: "/api/blob-upload",
      });
      $("#upload-message").textContent = `Validating ${file.name} and preparing recommendations…`;
      result = await api("/api/upload-blob", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ pathname: stored.pathname, filename: file.name }),
      });
    } else {
      const form = new FormData();
      form.append("file", file);
      result = await api("/api/upload", { method: "POST", body: form });
    }
    state.summary = result;
    toast(`Analysis complete · ${number(result.recommendations)} moves are ready to review.`);
    await loadDashboard();
    showView("overview");
  } catch (error) {
    toast(error.message, true);
  } finally {
    $("#upload-progress").hidden = true;
    $$(".top-upload, #welcome-upload, #choose-file").forEach((button) => { button.disabled = false; });
    $("#file-input").value = "";
  }
}

async function runAnalysis() {
  const button = $("#run-button");
  button.disabled = true;
  try {
    const result = await api("/api/run", { method: "POST" });
    state.summary = result;
    await loadDashboard();
    if (state.currentView === "moves") await refreshMoves();
    toast("Daily recommendation run completed.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function updateDecision(id, decision) {
  const move = state.moves.find((item) => item.id === id);
  if (decision === "approved") {
    if (!move) {
      toast("Move details are no longer loaded. Refresh the list and review it again.", true);
      return;
    }
    const confirmed = window.confirm([
      "Validate this automated recommendation before approval:",
      `SKU: ${move.sku_id} · ${moveTitle(move)}`,
      `Route: ${move.from_location} → ${move.to_location}`,
      `Quantity: ${number(move.qty)} units`,
      `Expected arrival: ${move.arrival_date}`,
      `Data reason: ${move.reason}`,
      "",
      "Have you checked these details against the displayed data?",
    ].join("\n"));
    if (!confirmed) return;
  }
  try {
    await api(`/api/recommendations/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        status: decision,
        ...(decision === "approved" ? { validated: true, validated_qty: move.qty } : {}),
      }),
    });
    toast(decision === "approved" ? "Move approved and recorded." : "Move declined and recorded.");
    await loadDashboard();
    if (state.currentView === "moves") await refreshMoves();
    if (state.currentView === "logistics") await refreshLogistics();
  } catch (error) {
    toast(error.message, true);
  }
}

async function completeLogistics(form) {
  const moveId = form.dataset.completeId;
  const formData = new FormData(form);
  const receivedQty = Number(formData.get("received_qty"));
  const skuVerified = formData.has("sku_verified");
  const routeVerified = formData.has("route_verified");
  if (!skuVerified || !routeVerified) {
    toast("Verify the SKU and route before completing logistics.", true);
    return;
  }
  try {
    await api(`/api/logistics/${encodeURIComponent(moveId)}/complete`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        received_qty: receivedQty,
        sku_verified: skuVerified,
        route_verified: routeVerified,
        note: formData.get("note"),
      }),
    });
    toast("Receipt validated and logistics marked complete.");
    await refreshLogistics();
    await loadDashboard();
  } catch (error) {
    toast(error.message, true);
  }
}

$("#file-input").addEventListener("change", (event) => uploadFile(event.target.files[0]));
$("#top-upload-button").addEventListener("click", () => $("#file-input").click());
$("#welcome-upload").addEventListener("click", () => $("#file-input").click());
$("#inline-upload").addEventListener("click", () => $("#file-input").click());
$("#choose-file").addEventListener("click", () => $("#file-input").click());
$("#run-button").addEventListener("click", runAnalysis);
$("#refresh-logistics").addEventListener("click", refreshLogistics);
["move-type-filter", "move-decision-filter"].forEach((id) => $(`#${id}`).addEventListener("change", refreshMoves));
$("#move-search").addEventListener("input", () => {
  clearTimeout(window.searchTimer);
  window.searchTimer = setTimeout(refreshMoves, 200);
});
["product-category-filter", "product-store-filter", "product-sort", "product-unsold-only"]
  .forEach((id) => $(`#${id}`).addEventListener("change", refreshProducts));
$("#product-search").addEventListener("input", () => {
  clearTimeout(window.productSearchTimer);
  window.productSearchTimer = setTimeout(refreshProducts, 200);
});
$("#bill-store-filter").addEventListener("change", refreshBillAnalysis);
$("#dropzone").addEventListener("click", (event) => {
  if (event.target.closest("button")) return;
  $("#file-input").click();
});
$("#dropzone").addEventListener("dragover", (event) => {
  event.preventDefault();
  $("#dropzone").classList.add("drag-over");
});
$("#dropzone").addEventListener("dragleave", () => $("#dropzone").classList.remove("drag-over"));
$("#dropzone").addEventListener("drop", (event) => {
  event.preventDefault();
  $("#dropzone").classList.remove("drag-over");
  uploadFile(event.dataTransfer.files[0]);
});

document.addEventListener("click", (event) => {
  const nav = event.target.closest("[data-view]");
  if (nav) showView(nav.dataset.view);
  const go = event.target.closest("[data-go]");
  if (go) {
    event.preventDefault();
    showView(go.dataset.go);
  }
  const upload = event.target.closest("[data-upload]");
  if (upload) $("#file-input").click();
  const action = event.target.closest("[data-decision]");
  if (action) updateDecision(action.dataset.id, action.dataset.decision);
  const product = event.target.closest("[data-product]");
  if (product) {
    state.selectedProduct = product.dataset.product;
    $$(".product-table tr[data-product]").forEach((row) =>
      row.classList.toggle("selected-row", row.dataset.product === state.selectedProduct));
    loadProductDetail(state.selectedProduct);
  }
  const store = event.target.closest("[data-store]");
  if (store) {
    state.selectedStore = store.dataset.store;
    showView("stores");
    renderStores();
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target.closest("[data-complete-id]");
  if (!form) return;
  event.preventDefault();
  completeLogistics(form);
});

$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("#chat-question").value.trim();
  if (!question) return;
  const answer = $("#chat-answer");
  answer.textContent = "Looking across the computed stock picture…";
  try {
    const result = await api("/api/ask", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
    answer.textContent = result.provider === "local_computed"
      ? `${result.answer}\n\n${result.fallback_reason ? "Gemini fallback · " + result.fallback_reason : "Local answer from backend-computed data."}`
      : result.answer;
  } catch (error) {
    answer.textContent = error.message;
    toast(error.message, true);
  }
});

$("#auth-button").addEventListener("click", async () => {
  if (!authState.required) {
    toast("Local workspace mode is open. Configure APPROVAL_TOKEN to require team access.");
    return;
  }
  if (authState.authenticated) {
    if (!authState.user) {
      toast("This local workspace does not require a sign-in.");
      return;
    }
    try {
      await api("/api/auth/logout", { method: "POST" });
      sessionStorage.removeItem(apiSessionTokenKey);
      window.location.reload();
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  $("#auth-close").hidden = false;
  $("#auth-dialog").showModal();
  $("#demo-role").focus();
});
$("#demo-role").addEventListener("change", () => {
  updateDemoRoleControls();
});
$("#auth-close").addEventListener("click", () => {
  if (authState.required && !authState.authenticated) return;
  $("#auth-dialog").close();
});
$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const role = $("#demo-role").value;
  const areaManagerId = $("#area-manager-account").value;
  const token = $("#auth-token").value;
  const path = authState.demoMode ? "/api/auth/demo-login" : "/api/auth/login";
  const body = authState.demoMode
    ? { role, area_manager_id: role === "area_manager" ? areaManagerId : undefined, token }
    : { token };
  try {
    const result = await api(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (result.sessionToken) {
      sessionStorage.setItem(apiSessionTokenKey, result.sessionToken);
    }
    Object.keys(sessionStorage).filter((key) => key.startsWith("manyavar-demo-face-verified:"))
      .forEach((key) => sessionStorage.removeItem(key));
    window.location.reload();
  } catch (error) {
    toast(error.message, true);
  }
});

async function initialize() {
  await updateAuthState();
  if (authState.required && !authState.authenticated) {
    $("#app-loader").classList.add("is-hidden");
    $("#auth-close").hidden = true;
    $("#auth-dialog").showModal();
    return;
  }
  if (authState.authenticated && authState.user
      && sessionStorage.getItem(demoFaceVerificationKey()) !== "demo-approved"
      && sessionStorage.getItem(demoFaceVerificationKey()) !== "camera-skipped") {
    $("#app-loader").classList.add("is-hidden");
    openDemoFaceCheck();
    return;
  }
  await loadDashboard();
}

initialize();
