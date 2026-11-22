"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const agentNames = ["Coordinator", "Scout", "Executor", "Auditor", "Advisor"];
  const decisionKinds = ["HOLD", "BLOCK", "APPROVE"];
  let state = null;
  let decisionFilter = "ALL";
  let activityTab = "orders";
  let chartRange = "all";
  const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
  const text = (value) => value === null || value === undefined || value === "" ? "N/A" : String(value);
  const num = (value, digits = 2) => isNumber(value) ? value.toLocaleString("en-US", {maximumFractionDigits: digits, minimumFractionDigits: digits}) : "N/A";
  const money = (value, signed = false) => isNumber(value) ? `${value < 0 ? "−" : signed && value > 0 ? "+" : ""}$${num(Math.abs(value))}` : "N/A";
  const pct = (value, signed = false) => isNumber(value) && isNumber(value * 100) ? `${value < 0 ? "−" : signed && value > 0 ? "+" : ""}${num(Math.abs(value * 100))}%` : "N/A";
  const date = (value) => {
    const parsed = new Date(value || "invalid");
    return Number.isNaN(parsed.getTime()) ? "N/A" : parsed.toISOString().replace("T", " ").slice(0, 19) + " UTC";
  };
  function el(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = text(value);
    return node;
  }
  function set(id, value) { $(id).textContent = text(value); }
  function tone(node, value) {
    node.classList.toggle("positive", isNumber(value) && value > 0);
    node.classList.toggle("negative", isNumber(value) && value < 0);
  }
  function empty(container, title, description) {
    const box = el("div", "empty-state");
    box.append(el("strong", "", title), el("span", "", description));
    container.replaceChildren(box);
  }
  function emptyTable(body, columns, message) {
    const row = el("tr");
    const cell = el("td", "empty-cell", message);
    cell.colSpan = columns;
    row.append(cell);
    body.replaceChildren(row);
  }
  function addCell(row, value, className = "") {
    const cell = el("td", className);
    if (value !== "") cell.textContent = text(value);
    row.append(cell);
    return cell;
  }
  function tag(value, kind) { return el("span", `tag ${kind || "other"}`, value); }
  function auditList(values, className = "audit-grid") {
    const list = el("dl", className);
    for (const [label, value, wide] of values) {
      const item = el("div", wide ? "wide" : "");
      item.append(el("dt", "", label), el("dd", "", value));
      list.append(item);
    }
    return list;
  }
  function selectButtons(container, attribute, selection) {
    for (const button of container.querySelectorAll("button")) {
      const selected = button.dataset[attribute] === selection;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    }
  }

  function renderErrors(data) {
    const panel = $("error-panel");
    panel.replaceChildren();
    panel.hidden = data.parse_error_count === 0;
    if (!panel.hidden) {
      panel.append(el("strong", "", `${data.parse_error_count} source issue${data.parse_error_count === 1 ? "" : "s"} · incomplete data must not be treated as a complete account view.`));
      const list = el("ul");
      for (const issue of data.errors || []) {
        list.append(el("li", "", `${text(issue.source)}${issue.line ? `, row ${issue.line}` : ""}: ${text(issue.message)}`));
      }
      panel.append(list);
    }
  }

  function render(data) {
    const snapshot = data.snapshot || {};
    const summary = data.summary;
    const demo = data.synthetic;
    $("source-banner").classList.toggle("demo", demo);
    set("source-title", demo ? "SIMULATED DEMO — no connected account" : data.source_mode === "snapshot" ? "LOCAL SNAPSHOT — no connected account" : "Not connected / no data");
    set("source-description", demo ? "Fictional, static paper-trading records for interface demonstration. These are not live results." : data.source_mode === "snapshot" ? "Explicitly configured local files. Values reflect the supplied snapshot, not a live account." : "Start with --snapshot PATH to read local data, or --fixture to explicitly opt in to synthetic demo records.");
    set("source-tag", demo ? "SYNTHETIC DATA" : data.source_mode === "snapshot" ? "FILE VIEW" : "NO SOURCE");
    set("account-id", snapshot.account_id);
    set("account-type", snapshot.account_type);
    set("mode", snapshot.mode);
    set("as-of", date(snapshot.as_of));
    set("equity-value", money(summary.equity));
    set("equity-note", isNumber(summary.total_pnl) ? `${money(summary.total_pnl, true)} since initial equity` : "Initial equity not supplied");
    set("pnl-value", money(summary.daily_pnl, true));
    tone($("pnl-value"), summary.daily_pnl);
    set("pnl-note", isNumber(summary.daily_pnl_pct) ? `${pct(summary.daily_pnl_pct, true)} from previous close` : "Valid previous close not supplied");
    set("drawdown-value", pct(summary.drawdown_pct));
    set("drawdown-note", isNumber(snapshot.peak_equity) && snapshot.peak_equity > 0 ? `Peak equity ${money(snapshot.peak_equity)}` : "Valid peak equity not supplied");
    set("beta-value", isNumber(summary.net_beta) ? `${num(summary.net_beta)}×` : "N/A");
    set("beta-note", summary.unknown_beta_count > 0 ? `${summary.unknown_beta_count} position beta${summary.unknown_beta_count === 1 ? "" : "s"} unknown` : isNumber(summary.net_beta) ? "Σ (market value × beta) / equity" : "Complete positions and equity required");
    set("gross-exposure", pct(summary.gross_exposure_pct));
    set("net-exposure", pct(summary.net_exposure_pct, true));
    set("chart-caption", demo ? "Synthetic history · not live performance" : "Supplied history · not a forward-return forecast");
    renderErrors(data);
    renderChart();
    renderExposures(snapshot);
    renderPositions(snapshot);
    renderActivity();
    renderAgents(snapshot);
    renderDecisions();
    const sources = data.provenance?.sources || {};
    set("provenance-text", `${demo ? "SIMULATED DEMO. " : ""}Source: ${sources.snapshot || "none configured"}. Snapshot as of ${date(snapshot.as_of)}. Last local read ${date(data.read_at)}.`);
    set("journal-provenance", data.source_mode === "none" ? "No snapshot or journals read. No automatic refresh; use Refresh data." : `Decisions: ${sources.decisions?.status || "not supplied"} · Fills: ${sources.fills?.status || "not supplied"} · Up to ${data.provenance?.journal_row_limit || 500} valid records per journal, in file order. No automatic refresh.`);
  }

  function svgNode(tagName, attributes, value) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tagName);
    for (const [key, val] of Object.entries(attributes || {})) node.setAttribute(key, String(val));
    if (value !== undefined) node.textContent = String(value);
    return node;
  }
  function renderChart() {
    const container = $("equity-chart");
    const history = (state?.snapshot?.equity_history || []).filter((point) => isNumber(point.equity) && Number.isFinite(Date.parse(point.timestamp))).slice().sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
    const latest = history.length ? Date.parse(history[history.length - 1].timestamp) : 0;
    const points = history.filter((point) => chartRange === "all" || Date.parse(point.timestamp) >= latest - Number(chartRange) * 86400000);
    set("history-count", `${points.length} observation${points.length === 1 ? "" : "s"}`);
    const change = points.length > 1 ? points[points.length - 1].equity - points[0].equity : null;
    set("period-change", money(change, true));
    tone($("period-change"), change);
    if (!points.length) {
      empty(container, "No equity history", "A chart appears only when timestamped equity observations are supplied.");
      return;
    }
    const svg = svgNode("svg", {viewBox: "0 0 690 260", role: "img", "aria-label": `Equity and historical drawdown; ${points.length} supplied observations; period change ${money(change, true)}`});
    svg.append(svgNode("title", {}, "Supplied equity observations and drawdown from the running peak"));
    const defs = svgNode("defs");
    const gradient = svgNode("linearGradient", {id: "equity-gradient", x1: "0", x2: "0", y1: "0", y2: "1"});
    gradient.append(svgNode("stop", {offset: "0%", "stop-color": "#bfdc91", "stop-opacity": ".18"}), svgNode("stop", {offset: "100%", "stop-color": "#bfdc91", "stop-opacity": "0"}));
    defs.append(gradient);
    svg.append(defs);
    const values = points.map((point) => point.equity);
    const low = Math.min(...values), high = Math.max(...values);
    const span = high - low || Math.max(Math.abs(high) * .01, 1);
    // Extreme finite inputs can still overflow chart arithmetic. Never emit NaN SVG.
    if (!isNumber(span) || !isNumber(low - span * .12) || !isNumber(high + span * .12)) {
      empty(container, "History outside display range", "Values cannot be plotted safely. Inspect the local source.");
      return;
    }
    const min = low - span * .12, max = high + span * .12;
    if (!isNumber(max - min)) {
      empty(container, "History outside display range", "Values cannot be plotted safely. Inspect the local source.");
      return;
    }
    const firstTime = Date.parse(points[0].timestamp), lastTime = Date.parse(points[points.length - 1].timestamp);
    const x = (point) => points.length === 1 || firstTime === lastTime ? 320 : 14 + (Date.parse(point.timestamp) - firstTime) / (lastTime - firstTime) * 595;
    const y = (value) => 164 - (value - min) / (max - min) * 139;
    for (let i = 0; i <= 3; i++) {
      const value = min + (max - min) * i / 3;
      const pos = y(value);
      svg.append(svgNode("line", {x1: 14, x2: 609, y1: pos, y2: pos, class: "chart-grid"}));
      const label = Math.abs(value) >= 1000 ? `$${num(value / 1000, 1)}k` : money(value);
      svg.append(svgNode("text", {x: 677, y: pos + 3, "text-anchor": "end", class: "chart-axis"}, label));
    }
    const coordinates = points.map((point) => `${x(point)},${y(point.equity)}`);
    const line = "M" + coordinates.join(" L");
    if (points.length > 1) svg.append(svgNode("path", {d: `${line} L${x(points[points.length - 1])},174 L${x(points[0])},174 Z`, fill: "url(#equity-gradient)"}));
    svg.append(svgNode("path", {d: line, fill: "none", stroke: "#c6e29a", "stroke-width": 2, "stroke-linejoin": "round"}));
    for (const point of points) {
      const dot = svgNode("circle", {cx: x(point), cy: y(point.equity), r: points.length === 1 ? 4 : 2, fill: "#c6e29a"});
      dot.append(svgNode("title", {}, `${date(point.timestamp)} · ${money(point.equity)}`));
      svg.append(dot);
    }
    svg.append(svgNode("line", {x1: 14, x2: 609, y1: 191, y2: 191, class: "chart-grid"}), svgNode("text", {x: 677, y: 194, "text-anchor": "end", class: "chart-axis"}, "0.0%"));
    let peak = -Infinity;
    const drawdowns = points.map((point) => {
      peak = Math.max(peak, point.equity);
      const value = peak > 0 ? Math.min(0, (point.equity - peak) / peak) : null;
      return {point, value: isNumber(value) ? value : null};
    });
    if (drawdowns.every((point) => point.value !== null)) {
      const maxDrawdown = Math.max(.005, ...drawdowns.map((item) => Math.abs(item.value)));
      const ddCoords = drawdowns.map((item) => `${x(item.point)},${191 + Math.abs(item.value) / maxDrawdown * 32}`);
      svg.append(svgNode("path", {d: "M" + ddCoords.join(" L") + ` L${x(points[points.length - 1])},191 L${x(points[0])},191 Z`, fill: "#71879e", "fill-opacity": ".16", stroke: "#7d92a9", "stroke-width": 1}));
      svg.append(svgNode("text", {x: 677, y: 225, "text-anchor": "end", class: "chart-axis"}, pct(-maxDrawdown)));
    } else {
      svg.append(svgNode("text", {x: 24, y: 215, class: "chart-axis"}, "Drawdown unavailable: nonpositive running peak"));
    }
    const indexes = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];
    for (const index of indexes) {
      const point = points[index];
      svg.append(svgNode("text", {x: x(point), y: 249, class: "chart-axis", "text-anchor": index === 0 ? "start" : index === points.length - 1 ? "end" : "middle"}, new Date(point.timestamp).toISOString().slice(5, 10)));
    }
    container.replaceChildren(svg);
  }

  function renderExposures(snapshot) {
    const container = $("exposure-bars");
    container.replaceChildren();
    if (!snapshot.positions?.length) {
      empty(container, snapshot.positions ? "No open positions" : "No positions supplied", "Exposure is not inferred from orders or fills.");
      return;
    }
    for (const position of snapshot.positions) {
      const marketValue = position.quantity * position.price;
      const weight = snapshot.equity > 0 ? marketValue / snapshot.equity : null;
      const row = el("div", "exposure-row");
      const label = el("div", "exposure-label");
      const symbol = el("span", "", position.symbol);
      symbol.append(el("small", "", position.quantity < 0 ? "SHORT" : "LONG"));
      label.append(symbol, el("span", "", pct(weight, position.quantity < 0)));
      const track = el("div", "exposure-track");
      const bar = el("span", `exposure-fill${position.quantity < 0 ? " short" : ""}`);
      bar.style.width = `${isNumber(weight) ? Math.min(100, Math.abs(weight) * 100) : 0}%`;
      track.append(bar);
      row.append(label, track);
      container.append(row);
    }
  }

  function renderPositions(snapshot) {
    const body = $("positions-body");
    const positions = snapshot.positions || [];
    set("position-count", positions.length);
    body.replaceChildren();
    if (!positions.length) {
      emptyTable(body, 7, snapshot.positions ? "No open positions in the supplied snapshot." : "No position data supplied. No account is connected.");
      return;
    }
    for (const position of positions) {
      const row = el("tr");
      const symbol = el("div", "symbol-cell");
      const shortSymbol = position.symbol.replace(/^US\./, "");
      symbol.append(el("span", "symbol-avatar", shortSymbol.slice(0, 1)), el("span", "", position.symbol));
      addCell(row, "").append(symbol);
      addCell(row, "").append(tag(position.quantity < 0 ? "SHORT" : "LONG", position.quantity < 0 ? "short" : "long"));
      const marketValue = position.quantity * position.price;
      addCell(row, num(position.quantity, 3), "numeric");
      addCell(row, money(position.price), "numeric");
      addCell(row, money(marketValue), "numeric");
      addCell(row, pct(snapshot.equity > 0 ? marketValue / snapshot.equity : null), "numeric");
      addCell(row, isNumber(position.beta) ? num(position.beta) : "Unknown", "numeric");
      body.append(row);
    }
  }

  function renderActivity() {
    const orders = state?.snapshot?.orders || [];
    const fills = state?.fills || [];
    set("order-count", orders.length);
    set("fill-count", fills.length);
    set("activity-caption", activityTab === "orders" ? "Supplied order records" : "Supplied fill records");
    const headers = activityTab === "orders" ? ["SYMBOL", "SIDE", "QUANTITY", "PRICE", "STATUS / RECORD"] : ["SYMBOL", "SIDE", "QUANTITY", "FILL PRICE", "FEES / RECORD"];
    const heading = el("tr");
    headers.forEach((name, index) => heading.append(el("th", index === 2 || index === 3 ? "numeric" : "", name)));
    $("activity-head").replaceChildren(heading);
    const body = $("activity-body");
    body.replaceChildren();
    const rows = (activityTab === "orders" ? orders : fills).slice().reverse();
    set("activity-note", activityTab === "orders" ? "Records do not imply pending executable instructions. No submission or cancellation controls." : "Fees include supplied commission, platform and regulatory fees. Missing fee components remain unknown.");
    if (!rows.length) {
      emptyTable(body, 5, `No ${activityTab === "orders" ? "order" : "fill"} records supplied. This dashboard never executes orders.`);
      return;
    }
    for (const record of rows) {
      const row = el("tr");
      addCell(row, record.symbol, "mono");
      addCell(row, record.side || "N/A");
      addCell(row, num(record.quantity, 3), "numeric");
      addCell(row, money(record.price), "numeric");
      const details = el("details", "record-details");
      const summary = el("summary");
      let fields;
      if (activityTab === "orders") {
        summary.append(tag(record.status || "Unknown", record.status?.toUpperCase() === "FILLED" ? "filled" : "other"));
        fields = [["Order ID", record.order_id], ["Timestamp", date(record.timestamp)], ["Type", record.type], ["Filled quantity", num(record.filled_quantity, 3)]];
      } else {
        const fees = [record.commission, record.platform_fee, record.regulatory_fee];
        summary.textContent = fees.every(isNumber) ? money(fees.reduce((a, b) => a + b, 0)) : "Fees N/A";
        fields = [["Order ID", record.order_id], ["Fill ID", record.fill_id], ["Timestamp", date(record.timestamp)], ["Commission", money(record.commission)], ["Platform fee", money(record.platform_fee)], ["Regulatory fee", money(record.regulatory_fee)], ["Arrival price", money(record.arrival_price)], ["Slippage (bps), supplied", num(record.slippage_bps)]];
      }
      details.append(summary, auditList(fields, ""));
      addCell(row, "").append(details);
      body.append(row);
    }
  }

  function renderAgents(snapshot) {
    const list = $("agent-list");
    list.replaceChildren();
    for (const name of agentNames) {
      const agent = (snapshot.agents || []).find((item) => item.name.toLowerCase() === name.toLowerCase());
      const row = el("div", "agent-row");
      const info = el("div", "agent-info");
      const session = el("div", "agent-session", agent?.session_id || "No session supplied");
      session.title = text(agent?.session_id);
      info.append(el("div", "agent-name", name), session);
      row.append(el("span", "agent-initial", name.slice(0, 1)), info, el("span", `agent-status${agent?.status ? "" : " unknown"}`, agent?.status || "Unknown"));
      list.append(row);
    }
    const usage = snapshot.usage || {};
    set("credits-value", !state.synthetic && isNumber(usage.credits) ? num(usage.credits) : "N/A");
    set("credits-date", !state.synthetic && isNumber(usage.credits) ? `Supplied usage · ${date(usage.as_of)}` : "Actual supplied usage only · not estimated");
  }

  function category(row) {
    const action = String(row.action || "").toUpperCase();
    const kind = String(row.kind || "").toUpperCase();
    return decisionKinds.includes(kind) ? kind : decisionKinds.includes(action) ? action : "OTHER";
  }
  function renderDecisions() {
    const all = state?.decisions || [];
    const search = $("decision-search").value.trim().toLowerCase();
    const decisions = all.filter((row) => (decisionFilter === "ALL" || category(row) === decisionFilter) && (!search || JSON.stringify(row).toLowerCase().includes(search))).slice().reverse();
    set("decision-count", all.length);
    set("decision-result-count", `${decisions.length} of ${all.length} records`);
    const list = $("decision-list");
    list.replaceChildren();
    if (!decisions.length) {
      empty(list, all.length ? "No matching decisions" : "No decisions recorded", all.length ? "Try another filter or clear your search." : "Only supplied journal entries appear here. No agent is run by this dashboard.");
      return;
    }
    for (const decision of decisions) {
      const row = el("details", "decision-row");
      const summary = el("summary");
      const timestamp = date(decision.timestamp);
      const time = el("span", "decision-time", timestamp === "N/A" ? "N/A" : timestamp.slice(11, 19));
      if (timestamp !== "N/A") time.append(el("small", "", timestamp.slice(0, 10) + " UTC"));
      const kind = category(decision);
      summary.append(time, el("span", "decision-agent", decision.agent), el("span", "decision-symbol", decision.symbol || "—"), tag(kind, kind.toLowerCase()), el("span", "decision-reason", decision.reason), el("span", "decision-expand", "+"));
      const fields = [["Reason", decision.reason, true], ["Event ID", decision.event_id], ["Timestamp", timestamp], ["Account / mode", `${text(decision.account_id)} / ${text(decision.mode)}`], ["Run ID", decision.run_id], ["Session ID", decision.session_id], ["Agent", decision.agent], ["Kind / action", `${text(decision.kind)} / ${text(decision.action)}`], ["Symbol", decision.symbol], ["Target checksum", decision.target_checksum], ["Order fingerprint", decision.order_fingerprint], ["Config checksum", decision.config_checksum], ["Rule checks", decision.rule_checks, true]];
      row.append(summary, auditList(fields));
      list.append(row);
    }
  }

  async function refresh() {
    const button = $("refresh");
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    set("refresh-status", "Reading configured local files…");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch("/api/state", {cache: "no-store", signal: controller.signal, credentials: "omit"});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!data || data.schema_version !== 1 || !data.summary || !Array.isArray(data.decisions) || !Array.isArray(data.fills) || !isNumber(data.parse_error_count) || !["none", "fixture", "snapshot"].includes(data.source_mode)) throw new Error("Invalid state response");
      state = data;
      render(data);
      set("refresh-status", `${data.status === "empty" ? "Not connected / no data" : data.status === "error" ? "Source unavailable" : data.status === "partial" ? "Local read completed with issues" : "Local read complete"} · ${date(data.read_at)} · manual refresh only`);
    } catch (error) {
      const panel = $("error-panel");
      panel.hidden = false;
      panel.replaceChildren(el("strong", "", "Refresh failed — " + (state ? "showing the last successful read, which may now be stale." : "no data loaded.")), el("p", "", `The local server could not provide a valid response (${error.name === "AbortError" ? "request timed out" : error.message}). Check the server and use Refresh data to retry.`));
      set("refresh-status", "Refresh failed · displayed values are not current");
    } finally {
      clearTimeout(timeout);
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  $("refresh").addEventListener("click", refresh);
  $("decision-search").addEventListener("input", renderDecisions);
  $("decision-filters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (!button) return;
    decisionFilter = button.dataset.filter;
    selectButtons($("decision-filters"), "filter", decisionFilter);
    renderDecisions();
  });
  $("activity-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (!button) return;
    activityTab = button.dataset.tab;
    selectButtons($("activity-tabs"), "tab", activityTab);
    renderActivity();
  });
  $("range-filters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-range]");
    if (!button) return;
    chartRange = button.dataset.range;
    selectButtons($("range-filters"), "range", chartRange);
    renderChart();
  });
  for (const link of document.querySelectorAll(".nav-link")) {
    link.addEventListener("click", () => {
      document.querySelectorAll(".nav-link").forEach((item) => item.classList.toggle("active", item === link));
    });
  }
  refresh();
})();
