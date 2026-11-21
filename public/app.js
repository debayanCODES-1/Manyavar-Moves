const state = {
  summary: null, moves: [], stores: [], insights: [], currentView: "overview",
  selectedProduct: null, selectedStore: null, products: [],
};
const authState = { required: false, authenticated: true, demoMode: false, user: null };
const photoUrls = [
  "https://images.unsplash.com/photo-1610030469983-98e550d6193c?auto=format&fit=crop&w=320&q=80",
  "https://images.unsplash.com/photo-1597983073493-88cd35cf93b0?auto=format&fit=crop&w=320&q=80",
  "https://images.unsplash.com/photo-1603400521630-9f2de124b33b?auto=format&fit=crop&w=320&q=80",
];
let toastTimer;

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
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const result = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof result === "object" ? result.detail || result.message : result;
    throw new Error(message || `Request failed (${response.status})`);
  }
  return result;
}

async function updateAuthState() {
  try {
    Object.assign(authState, await api("/api/auth/status"));
    const button = $("#auth-button");
    button.classList.toggle("locked", authState.required && !authState.authenticated);
    button.title = authState.authenticated && authState.user
      ? `${authState.user.name} · sign out`
      : authState.required ? "Choose a workspace role" : "Local workspace";
    button.textContent = authState.user?.name?.slice(0, 1) || (authState.required ? "↗" : "M");
    $("#current-user-label").textContent = authState.user?.name || (authState.required ? "Sign in" : "Local workspace");
    const demoRole = $("#demo-role");
    demoRole.hidden = !authState.demoMode;
    $("#area-manager-label").hidden = !authState.demoMode || demoRole.value !== "area_manager";
    $("#area-manager-account").hidden = !authState.demoMode || demoRole.value !== "area_manager";
    $("#token-label").hidden = !authState.demoMode && !authState.required;
    $("#auth-token").hidden = !authState.demoMode && !authState.required;
    $("#auth-token").required = !authState.demoMode && authState.required;
    applyPermissions();
  } catch {
    authState.required = false;
  }
}

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
    overview: "Overview", moves: "Move approvals", stores: "Store network",
    products: "Product intelligence", bills: "Bill analyzer", model: "Demand & AI", data: "Data & rules",
  };
  $("#breadcrumb-current").textContent = labels[name] || labels.overview;
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
    $("#data-period").textContent = "Upload the shared hackathon dataset to activate live numbers";
    $("#data-source-name").textContent = "No dataset loaded";
    $(".source-dot").classList.remove("ready");
    $("#kpi-p1").textContent = "Upload data for recommendations";
    $("#nav-pending").textContent = "—";
    $("#category-chart-total").textContent = "—";
    return;
  }
  $("#welcome-banner").classList.add("data-loaded");
  $("#data-period").textContent = data.period_start && data.period_end
    ? `Stock snapshot · ${new Date(`${data.period_end}T00:00:00`).toLocaleDateString("en-IN", { dateStyle: "medium" })}`
    : "Dataset loaded";
  $("#data-source-name").textContent = `${data.source_name || "Uploaded dataset"} · ${number(data.stores)} stores · ${number(data.skus)} SKUs`;
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

function moveCard(move, index) {
  const picture = photoUrls[index % photoUrls.length];
  const actionButtons = move.decision === "pending" && can("approve")
    ? `<div class="action-buttons"><button class="approve-button" data-decision="approved" data-id="${escapeHTML(move.id)}">✓ Approve</button><button class="reject-button" data-decision="rejected" data-id="${escapeHTML(move.id)}">Decline</button></div>`
    : `<span class="move-badge decision-badge">${move.decision === "pending" ? "PENDING REVIEW" : move.decision === "approved" ? "APPROVED" : "DECLINED"}</span>`;
  return `<article class="move-card">
    <div class="move-photo"><img loading="lazy" src="${picture}" alt="Editorial stock photograph" onerror="this.style.display='none'"></div>
    <div class="move-copy">
      <div class="move-topline"><span class="move-badge ${move.priority === "P1" ? "p1" : ""}">${escapeHTML(move.priority)}</span><span class="move-badge">${escapeHTML(moveType(move))}</span>${move.requires_human_approval ? `<span class="move-badge p1">HUMAN APPROVAL · ${escapeHTML(rupees(move.approval_threshold_mrp))}+</span>` : ""}<span class="move-badge decision-badge">${escapeHTML(move.decision || "pending").toUpperCase()}</span></div>
      <h3>${escapeHTML(moveTitle(move))}</h3>
      <div class="move-sub">${escapeHTML(move.category)} · ${escapeHTML(move.sku_id)}</div>
      <div class="move-route"><span>${escapeHTML(move.from_location)}</span><i>→</i><span>${escapeHTML(move.to_location)}</span></div>
      <div class="move-reason">${escapeHTML(move.reason)}</div>
      <div class="move-meta"><span>ARRIVES <strong>${escapeHTML(new Date(`${move.arrival_date}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" }))}</strong></span><span>VELOCITY <strong>${escapeHTML(move.daily_velocity)} units/day</strong></span><span>MRP VALUE <strong>${rupees(move.move_value_mrp)}</strong></span>${move.stock_age_days ? `<span>OLDEST UNIT <strong>${number(move.stock_age_days)} days</strong></span>` : ""}${move.donor_cover_after_days ? `<span>DONOR COVER <strong>${escapeHTML(move.donor_cover_after_days)} days</strong></span>` : ""}</div>
    </div>
    <div class="move-actions"><div class="move-qty">${number(move.qty)}<small>units</small></div>${actionButtons}</div>
  </article>`;
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
    <tr class="clickable-row ${state.selectedStore === store.store_id ? "selected-row" : ""}" data-store="${escapeHTML(store.store_id)}"><td>${escapeHTML(store.store_id)}<div class="move-sub">${escapeHTML(store.city)}${store.state ? ` · ${escapeHTML(store.state)}` : ""}</div></td>
    <td><span class="grade-tag">${escapeHTML(store.tier || "—")}</span></td>
    <td>${escapeHTML(store.area_manager_id || "—")}</td><td>${number(store.stock_units)}</td>
    <td class="capacity-cell">${store.capacity_pct}%<div class="capacity-track"><i style="width:${Math.min(100, store.capacity_pct)}%"></i></div></td>
    <td>${rupees(store.sales_revenue)}</td><td>${number(store.units_sold)}</td>
    <td>${number(store.footfall)}</td><td>${store.visit_to_bill_pct}%</td><td>${store.avg_staff_present}</td></tr>`).join("")
    : '<tr><td colspan="10" class="empty-cell">Upload your dataset to see store-level performance.</td></tr>';
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
  target.innerHTML = '<tr><td colspan="8"><div class="table-skeleton"><i></i><i></i><i></i></div></td></tr>';
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
      target.innerHTML = '<tr><td colspan="8" class="empty-cell">No products match these filters.</td></tr>';
      return;
    }
    target.innerHTML = result.items.map((item) => `<tr class="clickable-row ${state.selectedProduct === item.sku_id ? "selected-row" : ""}" data-product="${escapeHTML(item.sku_id)}">
      <td><strong>${escapeHTML(item.style_name || item.style_id || item.sku_id)}</strong><div class="move-sub">${escapeHTML(item.sku_id)}</div></td>
      <td>${escapeHTML(item.category || "—")}</td><td>${escapeHTML(item.size || "—")}</td>
      <td>${number(item.store_metrics?.sales_units ?? item.total_sold_units)}</td>
      <td><span class="metric-pill ${item.store_metrics?.recent_units || item.recent_sold_units ? "positive" : ""}">${number(item.store_metrics?.recent_units ?? item.recent_sold_units)}</span></td>
      <td>${number(item.store_metrics?.stock_units ?? item.store_stock_units)}</td><td>${number(item.warehouse_units)}</td>
      <td>${escapeHTML(item.best_selling_store || "No sales yet")}</td></tr>`).join("");
    const selected = result.items.find((item) => item.sku_id === state.selectedProduct) || result.items[0];
    state.selectedProduct = selected.sku_id;
    await loadProductDetail(selected.sku_id);
  } catch (error) {
    target.innerHTML = `<tr><td colspan="8" class="empty-cell">${escapeHTML(error.message)}</td></tr>`;
  }
}

async function loadProductDetail(skuId) {
  const detail = $("#product-detail");
  detail.setAttribute("aria-busy", "true");
  detail.innerHTML = '<div class="detail-loading"><span class="spinner"></span><span>Bringing the product history together…</span></div>';
  try {
    const result = await api(`/api/products/${encodeURIComponent(skuId)}`);
    const product = result.product;
    state.selectedProduct = skuId;
    const photo = photoUrls[["Sherwani", "Indo-Western", "Jacket"].includes(product.category) ? 0
      : product.category === "Kurta Set" ? 1 : 2];
    const trendRows = result.sales_trend.map((row, index) => ({
      week: row.week,
      units: row.units,
      stock: result.stock_trend[index]?.units || 0,
    }));
    detail.innerHTML = `<div class="product-hero">
      <div class="product-image"><img src="${photo}" alt="Manyavar occasionwear editorial photograph" loading="lazy" onerror="this.parentElement.classList.add('image-fallback');this.remove()"><span>${escapeHTML(product.category || "COLLECTION")}</span></div>
      <div class="product-copy"><div class="product-tags"><span class="grade-tag">${asCore(product) ? "CORE STYLE" : "SEASONAL STYLE"}</span><span class="grade-tag teal-tag">${escapeHTML(product.sku_id)}</span></div>
        <h2>${escapeHTML(product.style_name || product.style_id || product.sku_id)}</h2><p>${escapeHTML(product.description)}</p>
        <div class="product-price">${rupees(product.mrp)} <small>MRP · ${escapeHTML(product.size || "Free size")}</small></div>
      </div></div>
      <div class="product-stats">
        <div><span>ALL-TIME UNITS</span><strong>${number(product.total_sold_units)}</strong></div>
        <div><span>SALES VALUE</span><strong>${shortRupees(product.sales_revenue)}</strong></div>
        <div><span>STORE STOCK</span><strong>${number(product.store_stock_units)}</strong></div>
        <div><span>WAREHOUSE</span><strong>${number(product.warehouse_units)}</strong></div>
        <div><span>RECENT VELOCITY</span><strong>${product.recent_velocity == null ? "Unknown" : `${product.recent_velocity} / day`}</strong></div>
        <div><span>OLDEST UNIT AGE</span><strong>${number(product.oldest_stock_age_days)} days</strong></div>
      </div>
      <div class="chart-pair"><section><div class="mini-heading"><strong>Weekly sell-through</strong><span>12 weeks</span></div><div id="product-sales-chart" class="trend-chart"></div></section>
        <section><div class="mini-heading"><strong>Closing stock</strong><span>Store network</span></div><div id="product-stock-chart" class="trend-chart"></div></section></div>
      <div class="panel-heading store-performance-heading"><div><span class="eyebrow muted-eyebrow">SAME SKU · EVERY STORE</span><h3>Where the style sells and where it waits</h3></div></div>
      <div class="table-wrap"><table class="data-table compact-table"><thead><tr><th>STORE</th><th>RECENT UNITS</th><th>12-WK IN-STOCK DAYS</th><th>RECENT VELOCITY</th><th>ON HAND</th><th>IN TRANSIT</th><th>COVER</th><th>OLDEST UNIT</th></tr></thead><tbody>${product.store_performance.map((store) => `<tr><td><strong>${escapeHTML(store.store_id)}</strong><div class="move-sub">${escapeHTML(store.city)} · ${escapeHTML(store.tier)}</div></td><td>${number(store.recent_units)}</td><td>${number(store.recent_in_stock_days)}</td><td>${store.recent_velocity == null ? '<span class="unknown-tag">NO EXPOSURE</span>' : `${store.recent_velocity} / day`}</td><td>${number(store.on_hand)}</td><td>${number(store.in_transit)}</td><td>${store.cover_days == null ? "—" : `${number(store.cover_days)} days`}</td><td>${store.stock_age_days == null ? "—" : `${number(store.stock_age_days)} days`}</td></tr>`).join("")}</tbody></table></div>
      <div class="data-disclaimer">${product.unsold_locations.length ? `${product.unsold_locations.length} location(s) have stock and recorded availability but no sales in the recent 12-week window.` : "No stock-held location has both recorded recent availability and zero sales."} This is an exposure-aware signal, not a claim that the style will never sell.</div>`;
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
    target.innerHTML = `<div class="panel-heading"><div><span class="eyebrow muted-eyebrow">${escapeHTML(store.tier)} · ${escapeHTML(store.city)}</span><h3>${escapeHTML(store.store_id)} · Store detail</h3></div><span class="grade-tag">${number(store.capacity_pct)}% CAPACITY</span></div>
      <div class="store-detail-grid"><div><div class="mini-heading"><strong>Weekly sell-through</strong><span>Recent 12 weeks</span></div><div id="store-sales-chart" class="trend-chart"></div></div><div><div class="mini-heading"><strong>Weekly closing stock</strong><span>Store stock</span></div><div id="store-stock-chart" class="trend-chart"></div></div></div>
      <div class="store-detail-grid"><div><div class="mini-heading"><strong>Top sellers</strong><span>Store-specific</span></div><div class="ranked-list">${result.best_sellers.map((item, index) => `<div><i>${String(index + 1).padStart(2, "0")}</i><span><strong>${escapeHTML(item.style_name || item.sku_id)}</strong><small>${escapeHTML(item.sku_id)} · ${escapeHTML(item.category)}</small></span><b>${number(item.store_metrics.recent_units)} <small>12 wk</small></b></div>`).join("") || '<span class="empty-note">No sales recorded in this window.</span>'}</div></div>
        <div><div class="mini-heading"><strong>Unsold with stock</strong><span>Recent exposure confirmed</span></div><div class="ranked-list">${result.dormant_stock.slice(0, 8).map((item) => `<div><i class="dormant-mark">!</i><span><strong>${escapeHTML(item.style_name || item.sku_id)}</strong><small>${escapeHTML(item.sku_id)} · ${number(item.store_metrics.recent_in_stock_days)} in-stock days</small></span><b>${number(item.store_metrics.stock_units)} <small>units</small></b></div>`).join("") || '<span class="empty-note">No exposure-confirmed dormant stock.</span>'}</div></div></div>
      <div class="mini-heading"><strong>Category stock position</strong><span>Units on hand + in transit</span></div><div class="category-chip-list">${result.category_stock.map((item) => `<span>${escapeHTML(item.category)} <b>${number(item.units)}</b></span>`).join("")}</div>`;
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
      return `<tr><td><strong>${escapeHTML(item.style_name || item.sku_id)}</strong><div class="move-sub">${escapeHTML(item.sku_id)} · ${escapeHTML(item.category)}</div></td><td>${number(store.stock_units)}</td><td>${number(store.recent_in_stock_days)}</td><td>${store.stock_age_days == null ? "—" : `${number(store.stock_age_days)} days`}</td><td>${source ? `${escapeHTML(source.store_id)} · ${number(source.recent_units)} sold` : "No recent selling store"}</td><td>${move ? `<span class="move-ready">${escapeHTML(move.from_location)} → ${escapeHTML(move.to_location)} · ${number(move.qty)}</span>` : '<span class="unknown-tag">NO SAFE MOVE</span>'}</td></tr>`;
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
    populateAnalyticsFilters(meta.categories);
    renderInsights();
    $("#overview-moves").innerHTML = state.moves.length
      ? state.moves.slice(0, 4).map((move, index) => `<div class="preview-row"><span class="preview-priority ${move.priority === "P1" ? "urgent" : ""}">${escapeHTML(move.priority)}</span><div><strong>${escapeHTML(moveTitle(move))}</strong><span>${escapeHTML(move.from_location)} <i>→</i> ${escapeHTML(move.to_location)} · ${number(move.qty)} units</span></div><b>${escapeHTML(move.arrival_date)}</b></div>`).join("")
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
  $("#upload-progress").hidden = false;
  $("#upload-message").textContent = `Reading ${file.name} and preparing recommendations…`;
  $$(".top-upload, #welcome-upload, #choose-file").forEach((button) => { button.disabled = true; });
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api("/api/upload", { method: "POST", body: form });
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
  try {
    await api(`/api/recommendations/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ status: decision }),
    });
    toast(decision === "approved" ? "Move approved and recorded." : "Move declined and recorded.");
    await loadDashboard();
    if (state.currentView === "moves") await refreshMoves();
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
    answer.textContent = result.answer;
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
  const isAreaManager = $("#demo-role").value === "area_manager";
  $("#area-manager-label").hidden = !isAreaManager;
  $("#area-manager-account").hidden = !isAreaManager;
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
    await api(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
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
  await loadDashboard();
}

initialize();
