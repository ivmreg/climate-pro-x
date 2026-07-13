/*
 * Thermal Storyboard Card
 *
 * Dependency-free Lovelace card for climate-pro-x. It intentionally consumes
 * only entity states and compact attributes; raw regression points belong in
 * a future diagnostics-backed enhancement rather than large sensor attributes.
 */

const DEFAULTS = {
  title: "Thermal storyboard",
  hlc: "sensor.thermal_efficiency_heat_loss_coefficient",
  fabric: "sensor.metahome_thermal_efficiency_fabric_heat_loss",
  ventilation: "sensor.metahome_thermal_efficiency_ventilation_heat_loss",
  ach: "sensor.metahome_thermal_efficiency_air_change_rate",
  hot_water: "sensor.metahome_thermal_efficiency_hot_water_gas",
  loft: "sensor.thermal_efficiency_loft_ratio",
};

const NUMBER = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

function finite(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function stateValue(state) {
  if (!state || ["unknown", "unavailable"].includes(state.state)) return null;
  return finite(state.state);
}

function attr(state, name) {
  return state?.attributes?.[name];
}

function format(value, suffix = "", digits = 1) {
  if (!Number.isFinite(value)) return "Unavailable";
  return `${Number(value).toLocaleString(undefined, {
    maximumFractionDigits: digits,
  })}${suffix}`;
}

function statusTone(status) {
  const normalized = String(status || "unknown").toLowerCase();
  if (normalized === "valid") return "good";
  if (normalized === "provisional") return "warn";
  return "muted";
}

function roomLabel(state, entityId) {
  const friendly = attr(state, "friendly_name") || entityId.split(".")[1];
  return friendly
    .replace(/^Thermal Efficiency\s+/i, "")
    .replace(/ effective overnight cooling time constant$/i, "")
    .replace(/_/g, " ");
}

function roomBand(tau) {
  if (tau < 10) return { label: "Fast cooling", tone: "bad" };
  if (tau <= 20) return { label: "Typical", tone: "warn" };
  return { label: "Slow cooling", tone: "good" };
}

class ThermalStoryboardCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._signature = null;
    this.shadowRoot.addEventListener("click", (event) => {
      const target = event.target.closest("[data-entity]");
      if (!target) return;
      const entityId = target.dataset.entity;
      if (!this._hass?.states?.[entityId]) return;
      this.dispatchEvent(
        new CustomEvent("hass-more-info", {
          bubbles: true,
          composed: true,
          detail: { entityId },
        }),
      );
    });
  }

  static getStubConfig() {
    return { ...DEFAULTS, rooms: [] };
  }

  setConfig(config) {
    if (!config || !config.hlc) {
      throw new Error("Thermal Storyboard Card requires an 'hlc' entity");
    }
    this._config = { ...DEFAULTS, ...config };
    this._signature = null;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    const entities = this._entityIds(hass);
    const signature = entities
      .map((entityId) => {
        const state = hass.states[entityId];
        return `${entityId}:${state?.last_updated || "missing"}`;
      })
      .join("|");
    if (signature !== this._signature) {
      this._signature = signature;
      this._render();
    }
  }

  getCardSize() {
    return 9;
  }

  getGridOptions() {
    return { columns: 12, rows: 9, min_columns: 6, min_rows: 6 };
  }

  _roomIds(hass = this._hass) {
    const configured = this._config?.rooms;
    if (Array.isArray(configured) && configured.length) return configured;
    if (!hass) return [];
    return Object.entries(hass.states)
      .filter(([entityId, state]) => {
        if (entityId === "sensor.metahome_thermal_efficiency_loft_time_constant") {
          return false;
        }
        return (
          entityId.startsWith("sensor.") &&
          entityId.endsWith("_time_constant") &&
          attr(state, "nights_fitted") !== undefined
        );
      })
      .map(([entityId]) => entityId);
  }

  _entityIds(hass = this._hass) {
    if (!this._config) return [];
    return [
      this._config.hlc,
      this._config.fabric,
      this._config.ventilation,
      this._config.ach,
      this._config.hot_water,
      this._config.loft,
      ...this._roomIds(hass),
    ].filter(Boolean);
  }

  _diagnosis(data) {
    if (data.hlc === null) {
      return "The delivered heat-loss estimate is unavailable. Check that the configured temperature and gas entities have long-term statistics.";
    }
    const caveats = [];
    if (data.status !== "valid") caveats.push("the HLC fit is provisional");
    if (data.r2 !== null && data.r2 < 0.7) caveats.push("the regression fit is noisy");
    if (data.co2Sensors === 1) caveats.push("the ventilation split uses one room");
    if (data.baselineSource === "indoor low-percentile fallback") {
      caveats.push("outdoor CO₂ is inferred from indoor data");
    }

    let lead;
    if (data.fabric !== null && data.ventilation !== null) {
      const share = data.hlc > 0 ? data.fabric / data.hlc : 0;
      lead =
        share >= 0.7
          ? "The strongest signal is fabric loss; investigate the weakest rooms, glazing, walls, and loft details before broad ventilation work."
          : "Ventilation is a material share of delivered loss; investigate controlled ventilation and draught paths alongside fabric measures.";
    } else {
      lead = "The HLC estimate is available, but there is not yet a physically consistent fabric/ventilation split.";
    }
    if (caveats.length) return `${lead} Treat this as directional because ${caveats.join(" and ")}.`;
    return lead;
  }

  _collect() {
    const hass = this._hass;
    const config = this._config;
    const hlcState = hass?.states?.[config.hlc];
    const fabricState = hass?.states?.[config.fabric];
    const ventilationState = hass?.states?.[config.ventilation];
    const achState = hass?.states?.[config.ach];
    const hotWaterState = hass?.states?.[config.hot_water];
    const loftState = hass?.states?.[config.loft];

    const rooms = this._roomIds(hass)
      .map((entityId) => {
        const state = hass.states[entityId];
        return {
          entityId,
          state,
          label: roomLabel(state, entityId),
          tau: stateValue(state),
          nights: finite(attr(state, "nights_fitted")),
          lastNight: attr(state, "last_night"),
          windowDays: finite(attr(state, "window_days")),
        };
      })
      .filter((room) => room.tau !== null)
      .sort((a, b) => a.tau - b.tau);

    return {
      hlcState,
      fabricState,
      ventilationState,
      achState,
      hotWaterState,
      loftState,
      hlc: stateValue(hlcState),
      status: attr(hlcState, "status") || "unknown",
      days: finite(attr(hlcState, "days_used")),
      r2: finite(attr(hlcState, "r_squared")),
      low: finite(attr(hlcState, "confidence_interval_low_w_per_k")),
      high: finite(attr(hlcState, "confidence_interval_high_w_per_k")),
      recent: finite(attr(hlcState, "recent_hlc_w_per_k")),
      recentDays: finite(attr(hlcState, "recent_days_used")),
      recentWindow: finite(attr(hlcState, "recent_window_days")),
      intercept: finite(attr(hlcState, "regression_intercept_kwh_per_day")),
      dhwCorrection: attr(hlcState, "dhw_correction"),
      fabric: stateValue(fabricState),
      ventilation: stateValue(ventilationState),
      ventilationShare: finite(attr(ventilationState, "share_of_delivered_hlc_pct")),
      ach: stateValue(achState),
      co2Sensors: finite(attr(achState, "co2_sensors_used")),
      decayWindows: finite(attr(achState, "decay_windows_used")),
      baselineSource: attr(achState, "co2_baseline_source"),
      hotWater: stateValue(hotWaterState),
      hotWaterCost: finite(attr(hotWaterState, "cost_per_year_gbp")),
      hotWaterDays: finite(attr(hotWaterState, "days_used")),
      loft: stateValue(loftState),
      rooms,
    };
  }

  _evidence(data) {
    const hasCi = data.low !== null && data.high !== null && data.hlc !== null;
    const scaleMax = hasCi ? Math.max(data.high * 1.15, data.hlc * 1.15, 1) : 1;
    const lowPct = hasCi ? Math.max(0, (data.low / scaleMax) * 100) : 0;
    const widthPct = hasCi ? Math.max(1, ((data.high - data.low) / scaleMax) * 100) : 0;
    const pointPct = hasCi ? Math.min(100, (data.hlc / scaleMax) * 100) : 0;
    const recentDelta =
      data.recent !== null && data.hlc
        ? ((data.recent - data.hlc) / data.hlc) * 100
        : null;

    return `
      <section class="panel evidence" aria-label="HLC evidence">
        <div class="panel-heading">
          <div><span class="eyebrow">Evidence</span><h3>Can I trust it?</h3></div>
          <ha-icon icon="mdi:chart-scatter-plot"></ha-icon>
        </div>
        <div class="metric-grid">
          <div><span>Fit</span><strong>${data.r2 === null ? "—" : `R² ${data.r2.toFixed(3)}`}</strong></div>
          <div><span>Heating days</span><strong>${data.days ?? "—"}</strong></div>
          <div><span>95% interval</span><strong>${hasCi ? `${Math.round(data.low)}–${Math.round(data.high)} W/K` : "—"}</strong></div>
        </div>
        <div class="confidence ${hasCi ? "" : "empty"}">
          <span class="ci-band" style="left:${lowPct}%;width:${widthPct}%"></span>
          <span class="ci-point" style="left:${pointPct}%"></span>
        </div>
        <div class="comparison">
          <div><span>Full window</span><strong>${format(data.hlc, " W/K", 0)}</strong></div>
          <div><span>Recent ${data.recentWindow ? `(${data.recentWindow}d)` : ""}</span><strong>${format(data.recent, " W/K", 0)}</strong></div>
          <div><span>Change</span><strong class="${recentDelta !== null && Math.abs(recentDelta) >= 15 ? "warn-text" : ""}">${recentDelta === null ? "—" : `${recentDelta > 0 ? "+" : ""}${recentDelta.toFixed(0)}%`}</strong></div>
        </div>
        ${
          data.intercept !== null
            ? `<p class="fineprint">Regression intercept: ${format(data.intercept, " kWh/day", 1)}. ${escapeHtml(data.dhwCorrection || "")}</p>`
            : ""
        }
        <p class="progressive"><ha-icon icon="mdi:information-outline"></ha-icon> Daily scatter points require the planned diagnostics data mode.</p>
      </section>`;
  }

  _sankey(data) {
    const total = data.hlc && data.hlc > 0 ? data.hlc : null;
    const fabric = data.fabric !== null ? Math.max(0, data.fabric) : null;
    const ventilation = data.ventilation !== null ? Math.max(0, data.ventilation) : null;
    const consistent = total !== null && fabric !== null && ventilation !== null;
    const fabricRatio = consistent ? Math.max(0.08, Math.min(0.92, fabric / total)) : 0.7;
    const ventRatio = consistent ? Math.max(0.08, Math.min(0.92, ventilation / total)) : 0.3;
    const fabricWidth = 14 + 42 * fabricRatio;
    const ventWidth = 14 + 42 * ventRatio;
    const provisional = data.status !== "valid" || data.co2Sensors === 1;

    return `
      <section class="panel flow" aria-label="Heat-loss flow">
        <div class="panel-heading">
          <div><span class="eyebrow">Diagnosis</span><h3>Where heat goes</h3></div>
          <span class="quality ${provisional ? "warn" : "good"}">${provisional ? "Directional" : "Supported"}</span>
        </div>
        ${
          consistent
            ? `<svg class="sankey" viewBox="0 0 620 230" role="img" aria-label="Delivered HLC split between fabric and ventilation">
                <defs>
                  <linearGradient id="sourceFlow" x1="0" x2="1"><stop stop-color="#ffb74d"/><stop offset="1" stop-color="#ff8a65"/></linearGradient>
                </defs>
                <path d="M146 114 C260 114 260 58 405 58" fill="none" stroke="#ef6c00" stroke-opacity=".76" stroke-width="${fabricWidth}" stroke-linecap="round" data-entity="${escapeHtml(this._config.fabric)}"/>
                <path d="M146 135 C260 135 260 177 405 177" fill="none" stroke="#42a5f5" stroke-opacity=".8" stroke-width="${ventWidth}" stroke-linecap="round" data-entity="${escapeHtml(this._config.ventilation)}"/>
                <rect x="20" y="82" width="132" height="84" rx="18" fill="url(#sourceFlow)" data-entity="${escapeHtml(this._config.hlc)}"/>
                <rect x="400" y="25" width="198" height="66" rx="16" fill="#ef6c00" data-entity="${escapeHtml(this._config.fabric)}"/>
                <rect x="400" y="144" width="198" height="66" rx="16" fill="#1e88e5" data-entity="${escapeHtml(this._config.ventilation)}"/>
                <text x="86" y="112" text-anchor="middle" class="svg-label">DELIVERED</text>
                <text x="86" y="142" text-anchor="middle" class="svg-value">${Math.round(total)} W/K</text>
                <text x="421" y="53" class="svg-label">FABRIC</text>
                <text x="421" y="78" class="svg-value">${Math.round(fabric)} W/K</text>
                <text x="421" y="172" class="svg-label">VENTILATION</text>
                <text x="421" y="197" class="svg-value">${Math.round(ventilation)} W/K</text>
              </svg>`
            : `<div class="empty-state"><ha-icon icon="mdi:chart-sankey-variant"></ha-icon><p>No physically consistent fabric/ventilation split is available.</p></div>`
        }
        <div class="flow-meta">
          <button data-entity="${escapeHtml(this._config.ach)}">
            <ha-icon icon="mdi:weather-windy"></ha-icon>
            <span>Room-derived ACH<strong>${format(data.ach, "/h", 2)}</strong></span>
            <small>${data.co2Sensors ?? "—"} CO₂ sensors · ${data.decayWindows ?? "—"} windows</small>
          </button>
          <button data-entity="${escapeHtml(this._config.hot_water)}">
            <ha-icon icon="mdi:water-boiler"></ha-icon>
            <span>Non-space-heating gas<strong>${format(data.hotWater, " kWh/day", 1)}</strong></span>
            <small>${data.hotWaterCost === null ? "Separate from W/K flow" : `£${NUMBER.format(data.hotWaterCost)}/modelled year`}</small>
          </button>
        </div>
      </section>`;
  }

  _rooms(data) {
    const maxTau = Math.max(...data.rooms.map((room) => room.tau), 1);
    return `
      <section class="panel rooms" aria-label="Room thermal fingerprints">
        <div class="panel-heading">
          <div><span class="eyebrow">Action</span><h3>Room thermal fingerprints</h3></div>
          <span class="sort-label">Fastest cooling first</span>
        </div>
        ${
          data.rooms.length
            ? `<div class="room-list">${data.rooms
                .map((room, index) => {
                  const band = roomBand(room.tau);
                  const width = Math.max(6, (room.tau / maxTau) * 100);
                  return `<button class="room-card" data-entity="${escapeHtml(room.entityId)}">
                    <div class="room-rank">${index + 1}</div>
                    <div class="room-main">
                      <div class="room-title"><strong>${escapeHtml(room.label)}</strong><span class="quality ${band.tone}">${band.label}</span></div>
                      <div class="tau-track"><span class="${band.tone}" style="width:${width}%"></span></div>
                      <div class="room-meta"><span>${room.nights ?? "—"} nights</span><span>${room.windowDays ?? "—"}d window</span>${room.lastNight ? `<span>Last ${escapeHtml(room.lastNight)}</span>` : ""}</div>
                    </div>
                    <div class="tau"><strong>${format(room.tau, " h", 1)}</strong><span>effective τ</span></div>
                  </button>`;
                })
                .join("")}</div>`
            : `<div class="empty-state"><ha-icon icon="mdi:thermometer-alert"></ha-icon><p>No usable room cooling fits. Check overnight heating, mild weather, and statistics coverage.</p></div>`
        }
        <p class="fineprint">Lower effective τ means faster observed cooling. It combines fabric, draughts, thermal mass, and heat exchange with adjacent rooms; it does not identify a defect by itself.</p>
      </section>`;
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    if (!this._hass) {
      this.shadowRoot.innerHTML = `<ha-card><div class="loading">Waiting for Home Assistant…</div></ha-card>`;
      return;
    }
    const data = this._collect();
    const tone = statusTone(data.status);
    const missingHlc = !data.hlcState;
    const diagnosis = this._diagnosis(data);

    this.shadowRoot.innerHTML = `
      <style>${ThermalStoryboardCard.styles}</style>
      <ha-card>
        <header class="hero" data-entity="${escapeHtml(this._config.hlc)}">
          <div>
            <span class="eyebrow">${escapeHtml(this._config.title)}</span>
            <h2>Delivered heat loss</h2>
            <p>Evidence → diagnosis → action</p>
          </div>
          <div class="headline">
            <strong>${format(data.hlc, " W/K", 0)}</strong>
            <div class="badges">
              <span class="quality ${tone}">${escapeHtml(data.status)}</span>
              ${data.days === null ? "" : `<span>${data.days} days</span>`}
              ${data.r2 === null ? "" : `<span>R² ${data.r2.toFixed(2)}</span>`}
            </div>
          </div>
        </header>
        ${missingHlc ? `<div class="config-error">Entity not found: <code>${escapeHtml(this._config.hlc)}</code></div>` : ""}
        <main>
          <div class="top-grid">${this._evidence(data)}${this._sankey(data)}</div>
          ${this._rooms(data)}
          <section class="interpretation">
            <ha-icon icon="mdi:lightbulb-on-outline"></ha-icon>
            <div><span class="eyebrow">Interpretation</span><p>${escapeHtml(diagnosis)}</p></div>
          </section>
        </main>
      </ha-card>`;
  }

  static styles = `
    :host { display:block; --fabric:#ef6c00; --vent:#1e88e5; --good:#2e7d32; --warn:#f9a825; --bad:#c62828; }
    * { box-sizing:border-box; }
    ha-card { overflow:hidden; color:var(--primary-text-color); background:var(--ha-card-background,var(--card-background-color)); }
    button, .hero, [data-entity] { cursor:pointer; }
    .hero { display:flex; justify-content:space-between; gap:24px; align-items:flex-end; padding:24px 28px; color:#fff; background:linear-gradient(120deg,#263238 0%,#37474f 55%,#4e342e 100%); }
    .hero h2 { margin:3px 0 2px; font-size:25px; line-height:1.2; }
    .hero p { margin:0; opacity:.72; }
    .eyebrow { display:block; font-size:11px; font-weight:800; letter-spacing:.12em; text-transform:uppercase; opacity:.68; }
    .headline { text-align:right; }
    .headline>strong { display:block; font-size:36px; line-height:1; white-space:nowrap; }
    .badges { display:flex; justify-content:flex-end; gap:6px; flex-wrap:wrap; margin-top:10px; }
    .badges span, .quality { padding:4px 8px; border-radius:999px; font-size:11px; font-weight:700; background:rgba(255,255,255,.14); }
    .quality { text-transform:capitalize; background:var(--secondary-background-color); }
    .quality.good { color:var(--good); background:color-mix(in srgb,var(--good) 14%,transparent); }
    .quality.warn { color:#9a6700; background:color-mix(in srgb,var(--warn) 19%,transparent); }
    .quality.bad { color:var(--bad); background:color-mix(in srgb,var(--bad) 13%,transparent); }
    .quality.muted { color:var(--secondary-text-color); }
    main { padding:18px; background:color-mix(in srgb,var(--secondary-background-color) 42%,transparent); }
    .top-grid { display:grid; grid-template-columns:minmax(280px,.9fr) minmax(390px,1.2fr); gap:16px; }
    .panel { padding:18px; border:1px solid var(--divider-color); border-radius:16px; background:var(--ha-card-background,var(--card-background-color)); box-shadow:0 2px 10px rgba(0,0,0,.04); }
    .panel-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:16px; }
    .panel-heading h3 { margin:2px 0 0; font-size:18px; }
    .panel-heading>ha-icon { color:var(--secondary-text-color); }
    .metric-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
    .metric-grid div { padding:10px; border-radius:10px; background:var(--secondary-background-color); }
    .metric-grid span, .comparison span { display:block; color:var(--secondary-text-color); font-size:11px; }
    .metric-grid strong { display:block; margin-top:4px; font-size:13px; }
    .confidence { position:relative; height:28px; margin:16px 3px 10px; border-radius:99px; background:linear-gradient(90deg,color-mix(in srgb,var(--good) 22%,transparent),color-mix(in srgb,var(--warn) 22%,transparent),color-mix(in srgb,var(--bad) 20%,transparent)); }
    .confidence.empty { opacity:.25; }
    .ci-band { position:absolute; top:7px; height:14px; border-radius:10px; background:rgba(55,71,79,.48); }
    .ci-point { position:absolute; top:2px; width:4px; height:24px; margin-left:-2px; border-radius:3px; background:var(--primary-text-color); box-shadow:0 0 0 2px var(--card-background-color); }
    .comparison { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
    .comparison strong { display:block; margin-top:2px; }
    .warn-text { color:#d17b00; }
    .fineprint { margin:14px 0 0; color:var(--secondary-text-color); font-size:11px; line-height:1.45; }
    .progressive { display:flex; gap:6px; align-items:center; margin:12px 0 0; color:var(--secondary-text-color); font-size:11px; }
    .progressive ha-icon { --mdc-icon-size:16px; }
    .sankey { display:block; width:100%; max-height:245px; overflow:visible; }
    .sankey [data-entity] { transition:opacity .15s; }
    .sankey [data-entity]:hover { opacity:.78; }
    .svg-label { fill:white; font:700 11px sans-serif; letter-spacing:.1em; }
    .svg-value { fill:white; font:800 20px sans-serif; }
    .flow-meta { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .flow-meta button { display:grid; grid-template-columns:auto 1fr; gap:2px 10px; align-items:center; padding:11px; text-align:left; color:inherit; border:1px solid var(--divider-color); border-radius:12px; background:transparent; }
    .flow-meta button:hover, .room-card:hover { background:var(--secondary-background-color); }
    .flow-meta ha-icon { grid-row:1/3; color:var(--secondary-text-color); }
    .flow-meta span { font-size:11px; color:var(--secondary-text-color); }
    .flow-meta strong { display:block; color:var(--primary-text-color); font-size:14px; }
    .flow-meta small { color:var(--secondary-text-color); font-size:10px; }
    .rooms { margin-top:16px; }
    .sort-label { color:var(--secondary-text-color); font-size:11px; }
    .room-list { display:grid; grid-template-columns:repeat(2,minmax(260px,1fr)); gap:8px; }
    .room-card { display:grid; grid-template-columns:28px 1fr auto; align-items:center; gap:10px; width:100%; padding:12px; color:inherit; text-align:left; border:1px solid var(--divider-color); border-radius:12px; background:transparent; }
    .room-rank { display:grid; place-items:center; width:26px; height:26px; border-radius:50%; color:var(--secondary-text-color); background:var(--secondary-background-color); font-size:11px; font-weight:800; }
    .room-title { display:flex; justify-content:space-between; gap:8px; align-items:center; }
    .room-title strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; text-transform:capitalize; }
    .tau-track { height:5px; margin:8px 0 6px; overflow:hidden; border-radius:10px; background:var(--secondary-background-color); }
    .tau-track span { display:block; height:100%; border-radius:10px; background:var(--warn); }
    .tau-track span.good { background:var(--good); }
    .tau-track span.bad { background:var(--bad); }
    .room-meta { display:flex; gap:9px; flex-wrap:wrap; color:var(--secondary-text-color); font-size:10px; }
    .tau { min-width:72px; text-align:right; }
    .tau strong { display:block; font-size:18px; }
    .tau span { color:var(--secondary-text-color); font-size:9px; }
    .interpretation { display:flex; align-items:flex-start; gap:12px; margin-top:16px; padding:16px 18px; border-radius:14px; color:var(--primary-text-color); background:color-mix(in srgb,var(--primary-color) 10%,var(--card-background-color)); }
    .interpretation ha-icon { color:var(--primary-color); }
    .interpretation p { margin:3px 0 0; line-height:1.45; }
    .empty-state { display:grid; place-items:center; min-height:160px; padding:20px; text-align:center; color:var(--secondary-text-color); }
    .empty-state ha-icon { --mdc-icon-size:40px; opacity:.45; }
    .config-error { margin:14px 18px 0; padding:10px 12px; border-radius:8px; color:var(--error-color); background:color-mix(in srgb,var(--error-color) 10%,transparent); }
    .loading { padding:24px; }
    @media (max-width:800px) {
      .hero { align-items:flex-start; padding:20px; }
      .headline>strong { font-size:28px; }
      main { padding:10px; }
      .top-grid { grid-template-columns:1fr; }
      .room-list { grid-template-columns:1fr; }
    }
    @media (max-width:520px) {
      .hero { display:block; }
      .headline { margin-top:18px; text-align:left; }
      .badges { justify-content:flex-start; }
      .metric-grid { grid-template-columns:1fr 1fr; }
      .flow-meta { grid-template-columns:1fr; }
      .sankey { max-height:190px; }
      .room-card { grid-template-columns:24px 1fr; }
      .tau { grid-column:2; text-align:left; }
    }
  `;
}

if (!customElements.get("thermal-storyboard-card")) {
  customElements.define("thermal-storyboard-card", ThermalStoryboardCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "thermal-storyboard-card",
    name: "Thermal Storyboard Card",
    description: "Evidence, heat-loss flow, and room thermal fingerprints.",
    preview: true,
  });
}

