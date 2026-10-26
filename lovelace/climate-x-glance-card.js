/* Read-only Home Assistant glance card. All entity IDs and display names arrive in config. */
const climateXEsc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));

class ClimateXGlanceCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode:"open"});
    this._hass = null;
    this._config = null;
    this._signature = "";
  }
  setConfig(config) {
    if (!config || !config.entities || !Array.isArray(config.rooms)) throw new Error("Climate X card needs entities and rooms.");
    this._config = config;
    if (this._hass) this._renderIfChanged(true);
  }
  set hass(value) {
    this._hass = value;
    this._renderIfChanged(false);
  }
  get hass() { return this._hass; }
  getCardSize() { return 8; }

  _deps() {
    const e = this._config?.entities || {};
    return [...new Set([...Object.values(e), ...(this._config?.rooms || []).map(r => r.entity)].filter(Boolean))];
  }
  _signatureFor() {
    const attrKeys = ["status","reason","next_action","raw_latest_source_day","latest_complete_gas_day","latest_eligible_day","confidence_interval_low_w_per_k","confidence_interval_high_w_per_k","days_used","effective_independent_days","r_squared","model_data_through","latest_source_day","source_lag_days","recent_hlc_w_per_k","kwh_per_day_30d","previous_30d_kwh_per_day","change_vs_previous_30d_pct","variable_cost_per_day_gbp_30d","days_used_30d","days_used_7d","litres_per_day_30d","typical_day_litres_30d","high_usage_days_30d","high_usage_threshold_litres","baseload_kwh_per_day","baseload_share_pct","cost_per_day_gbp","baseline_fallback_days","nights_fitted","last_night"];
    return this._deps().map(id => {
      const s = this._hass?.states?.[id];
      if (!s) return `${id}:missing`;
      const a = s.attributes || {};
      const relevant = {};
      for (const k of attrKeys) if (k in a) relevant[k] = a[k];
      return `${id}:${s.state}:${s.last_updated || s.last_changed || ""}:${JSON.stringify(relevant)}`;
    }).join("|");
  }
  _renderIfChanged(force) {
    if (!this._config || !this._hass) return;
    const next = this._signatureFor();
    if (!force && next === this._signature) return;
    this._signature = next;
    this._render();
  }
  _entity(key) { return this._config.entities[key]; }
  _state(key) { const id = this._entity(key); return id ? this._hass?.states?.[id] : null; }
  _num(value) {
    if (value === null || value === undefined || typeof value === "boolean") return null;
    if (typeof value === "string" && value.trim() === "") return null;
    if (["unknown","unavailable","none","null"].includes(String(value).trim().toLowerCase())) return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  _value(key) { return this._num(this._state(key)?.state); }
  _attr(key, name) { return this._num(this._state(key)?.attributes?.[name]); }
  _evidence(key) {
    const a = this._state(key)?.attributes || {};
    const status = a.status || "unavailable";
    const warning = ["valid", "ready"].includes(status) ? "" : `<div class="chip" role="status">${climateXEsc(status.replaceAll("_", " "))}${a.reason ? ` · ${climateXEsc(a.reason)}` : ""}</div>`;
    const day = a.raw_latest_source_day || a.latest_source_day || a.latest_complete_gas_day || a.latest_complete_day;
    return `${warning}${day ? `<div class="source">Source through ${climateXEsc(day)}</div>` : ""}`;
  }
  _fmt(n, digits=1) { return n === null ? "—" : n.toFixed(digits); }
  _info(id) {
    if (!id) return;
    this.dispatchEvent(new CustomEvent("hass-more-info", {detail:{entityId:id}, bubbles:true, composed:true}));
  }
  _panel(title, body, id, cls="") {
    return `<section class="panel ${cls}" aria-label="${climateXEsc(title)}"><div class="panel-head"><h2>${climateXEsc(title)}</h2>${id ? `<button class="info" type="button" data-info="${climateXEsc(id)}" aria-label="More information about ${climateXEsc(title)}" title="More information">ⓘ</button>` : ""}</div>${body}</section>`;
  }
  _bar(label, value, max, unit, id, nights=null) {
    const n = this._num(value), m = this._num(max);
    if (n === null || m === null || m <= 0) return `<div class="room-row"><span>${climateXEsc(label)}</span><span class="dash">—</span></div>`;
    const pct = Math.max(0, Math.min(100, n / m * 100));
    const hint = `${this._fmt(n,1)} ${unit}${nights === null ? "" : `, ${nights} nights fitted`}`;
    return `<button class="room-row room-action" type="button" data-info="${climateXEsc(id)}" title="${climateXEsc(hint)}" aria-label="${climateXEsc(label)}, ${climateXEsc(hint)}"><span>${climateXEsc(label)}</span><span class="bar"><i style="width:${pct.toFixed(2)}%"></i></span><b>${this._fmt(n,1)} ${climateXEsc(unit)}</b></button>`;
  }
  _render() {
    const e=this._config.entities, h=this._value("hlc"), hId=e.hlc;
    const lo=this._attr("hlc","confidence_interval_low_w_per_k"), hi=this._attr("hlc","confidence_interval_high_w_per_k");
    const ci=(h!==null && lo!==null && hi!==null && hi>=lo) ? `<div class="ci" role="img" aria-label="Confidence interval from ${this._fmt(lo,0)} to ${this._fmt(hi,0)} watts per kelvin; estimate ${this._fmt(h,0)}"><div class="ci-label"><span>${this._fmt(lo,0)}</span><span>Confidence interval · W/K</span><span>${this._fmt(hi,0)}</span></div><div class="track"><i style="left:${Math.max(0,Math.min(100,(h-lo)/(hi-lo||1)*100)).toFixed(1)}%"></i></div></div>` : `<div class="quiet">Confidence interval unavailable</div>`;
    const hdate=this._state("hlc")?.attributes?.model_data_through || this._state("hlc")?.attributes?.latest_source_day;
    const hlcBody=`<button type="button" class="primary" data-info="${climateXEsc(hId)}" aria-label="Delivered heat loss coefficient ${h===null?"unavailable":this._fmt(h,0)+" watts per kelvin"}">${h===null?"—":this._fmt(h,0)}<small>W/K delivered HLC</small></button>${this._evidence("hlc")}${ci}<div class="submetric"><b>${h===null?"—":this._fmt(h*20/1000,2)} kW</b><span>HLC × 20 °C difference; excludes free gains</span></div><div class="source">Model data ${climateXEsc(hdate || "unavailable")} · conditional regression interval</div>`;

    const e30=this._attr("electricity","kwh_per_day_30d"), ep=this._attr("electricity","previous_30d_kwh_per_day"), e7=this._value("electricity"), cost=this._attr("electricity","variable_cost_per_day_gbp_30d");
    const change=(e30!==null&&ep!==null&&ep>0)?(e30/ep-1)*100:null, scale=Math.max(e30??0,ep??0,0);
    const elecBars=(e30!==null&&ep!==null&&scale>0)?`<div class="compare"><div><span>Current 30d · ${this._fmt(e30,2)} kWh/day</span><i><b style="width:${(e30/scale*100).toFixed(1)}%"></b></i></div><div><span>Previous 30d · ${this._fmt(ep,2)} kWh/day</span><i><b style="width:${(ep/scale*100).toFixed(1)}%"></b></i></div></div>`:`<div class="quiet">30-day comparison unavailable</div>`;
    const elecBody=`<button type="button" class="primary" data-info="${climateXEsc(e.electricity)}" aria-label="Electricity 30-day mean ${e30===null?"unavailable":this._fmt(e30,2)+" kilowatt hours per day"}">${e30===null?"—":this._fmt(e30,2)}<small>kWh/day · current 30d</small></button><div class="delta ${change===null?"quiet":""}">${change===null?"Change unavailable":`${change>=0?"↑":"↓"} ${Math.abs(change).toFixed(0)}% vs previous 30d`}</div>${elecBars}<div class="caption">${cost===null?"Variable cost unavailable":`£${this._fmt(cost,2)}/day variable · 30d`}; standing charge excluded</div><div class="source">7d mean ${e7===null?"—":this._fmt(e7,2)} kWh/day</div>`;

    const w7=this._value("water"), w30=this._attr("water","litres_per_day_30d"), typical=this._attr("water","typical_day_litres_30d"), wh=this._attr("water","high_usage_days_30d"), whd=this._attr("water","days_used_30d"), threshold=this._attr("water","high_usage_threshold_litres"), w7d=this._attr("water","days_used_7d"), wday=this._state("water")?.attributes?.latest_complete_day, wlag=this._attr("water","source_lag_days");
    const wd=(w7!==null&&w30!==null&&w30>0)?(w7/w30-1)*100:null, wscale=Math.max(w7??0,w30??0,0);
    const waterBars=(w7!==null&&w30!==null&&wscale>0)?`<div class="compare"><div><span>7d · ${this._fmt(w7,0)} L/day</span><i><b style="width:${(w7/wscale*100).toFixed(1)}%"></b></i></div><div><span>30d · ${this._fmt(w30,0)} L/day</span><i><b style="width:${(w30/wscale*100).toFixed(1)}%"></b></i></div></div>`:`<div class="quiet">Rolling comparison unavailable</div>`;
    const waterBody=`<button type="button" class="primary" data-info="${climateXEsc(e.water)}" aria-label="Water 7-day mean ${w7===null?"unavailable":this._fmt(w7,0)+" litres per day"}">${w7===null?"—":this._fmt(w7,0)}<small>L/day · 7d mean</small></button><div class="delta ${wd===null?"quiet":""}">${wd===null?"Change unavailable":`${wd>=0?"+":"−"}${Math.abs(wd).toFixed(1)}% vs overlapping 30d baseline`}</div>${waterBars}<div class="caption">Typical ${typical===null?"—":this._fmt(typical,0)} L/day · high use ${wh===null?"—":`${wh} of ${whd??"—"} observed days`}</div><div class="source" title="High-use threshold ${threshold===null?"unavailable":this._fmt(threshold,0)+" litres per day"}">Complete ${climateXEsc(wday||"—")} · ${wlag===null?"lag —":`lag ${wlag}d`} · threshold ${threshold===null?"—":this._fmt(threshold,0)} L/d</div><span class="sr-only">7-day coverage ${w7d===null?"unavailable":`${w7d} of 7 days`}; high-use threshold ${threshold===null?"unavailable":threshold+" litres per day"}</span>`;

    const rooms=[...(this._config.rooms||[])].map(r=>({name:r.name,id:r.entity,s:this._hass?.states?.[r.entity],v:this._num(this._hass?.states?.[r.entity]?.state)})).sort((a,b)=>a.v===null?1:b.v===null?-1:b.v-a.v);
    const maxTau=Math.max(...rooms.map(r=>r.v??0));
    const roomBody=`<div class="rooms">${rooms.map(r=>this._bar(r.name,r.v,maxTau,"h",r.id,this._num(r.s?.attributes?.nights_fitted))).join("")}</div><div class="caption">Longer bars = slower observed cooling</div>`;

    const baseW=this._value("baseload"), baseK=this._attr("baseload","baseload_kwh_per_day"), totalK=e30;
    const share=(baseK!==null&&totalK!==null&&totalK>0&&baseK>=0&&baseK<=totalK)?baseK/totalK*100:null;
    const donut=share===null?`<div class="donut missing" role="img" aria-label="Baseload share unavailable"><span>—</span></div>`:`<div class="donut" style="--p:${share.toFixed(2)}%" role="img" aria-label="Estimated always-on baseload is ${share.toFixed(0)} percent of 30-day electricity"><span>${share.toFixed(0)}<small>%</small></span></div>`;
    const baseBody=`<div class="base-layout">${donut}<div><button type="button" class="primary compact" data-info="${climateXEsc(e.baseload)}" aria-label="Estimated always-on load ${baseW===null?"unavailable":this._fmt(baseW,0)+" watts"}">${baseW===null?"—":this._fmt(baseW,0)}<small>W estimated always-on</small></button><div class="caption">${baseK===null?"—":this._fmt(baseK,2)} kWh/day of ${totalK===null?"—":this._fmt(totalK,2)} · 30d total</div></div></div>`;

    const dhw=this._value("dhw"), dhwDays=this._attr("dhw","days_used"), dhwCost=this._attr("dhw","cost_per_day_gbp"), gasNow=this._value("gas_today"), gasCost=this._value("gas_cost"), hw7=this._value("gas_7d"), sh7=this._value("heat_7d");
    const gasSplit=(hw7!==null&&sh7!==null)?`<div class="caption">Estimated 7d allocation · hot water ${this._fmt(hw7,1)} + space heating ${this._fmt(sh7,1)} kWh/day</div>`:`<div class="chip">7d gas allocation unavailable</div>`;
    const gasBody=`<button type="button" class="primary" data-info="${climateXEsc(e.dhw)}" aria-label="Estimated non-heating gas baseline ${dhw===null?"unavailable":this._fmt(dhw,1)+" kilowatt hours per day"}">${dhw===null?"—":this._fmt(dhw,1)}<small>kWh/day · estimated baseline</small></button>${this._evidence("dhw")}<div class="caption">${dhwCost===null?"Cost —":`£${this._fmt(dhwCost,2)}/day`} · ${dhwDays===null?"—":dhwDays} heating-off days; includes hot water, may include other gas</div><div class="measured">Measured today so far · ${gasNow===null?"—":this._fmt(gasNow,2)+" kWh"} · ${gasCost===null?"—":"£"+this._fmt(gasCost,2)}</div>${gasSplit}`;

    const panels=[this._panel("Heat loss",hlcBody,hId,"heat"),this._panel("Electricity",elecBody,e.electricity,"electric"),this._panel("Water",waterBody,e.water,"water"),this._panel("Room cooling",roomBody,null,"room"),this._panel("Always-on electricity",baseBody,e.baseload,"base"),this._panel("Non-heating gas",gasBody,e.dhw,"gas")];
    this.shadowRoot.innerHTML=`<style>
      :host{display:block;container-type:inline-size;color:var(--primary-text-color);font:inherit;--cx-accent:var(--primary-color,#03a9f4);--cx-muted:var(--secondary-text-color,#687078);--cx-line:var(--divider-color,rgba(127,127,127,.25));--cx-card:var(--ha-card-background,var(--card-background-color,#fff))}
      *{box-sizing:border-box}.wrap{background:var(--cx-card);border:1px solid var(--cx-line);border-radius:var(--ha-card-border-radius,12px);padding:12px}.top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 2px 10px}.top h1{font-size:1.15rem;margin:0;font-weight:650}.nav{background:transparent;border:1px solid var(--cx-line);border-radius:999px;color:var(--primary-text-color);padding:6px 10px;font:inherit;font-size:.82rem;cursor:pointer}.nav:focus-visible,button:focus-visible{outline:2px solid var(--cx-accent);outline-offset:2px}.grid{display:grid;grid-template-columns:1fr;gap:10px}.panel{--cx-accent:var(--primary-color,#03a9f4);min-width:0;border:1px solid var(--cx-line);border-radius:10px;padding:11px;background:color-mix(in srgb,var(--cx-card) 96%,var(--cx-accent) 4%)}.panel.heat{--cx-accent:#8b5cf6}.panel.electric,.panel.base{--cx-accent:#d69e2e}.panel.water{--cx-accent:#0891b2}.panel.room{--cx-accent:#0f766e}.panel.gas{--cx-accent:#c2410c}.panel-head{display:flex;align-items:center;justify-content:space-between;min-height:23px;margin-bottom:4px}.panel h2{font-size:.94rem;line-height:1.2;margin:0;font-weight:620}.info{border:0;background:transparent;color:var(--cx-muted);font-size:1rem;padding:2px 5px;cursor:pointer}.primary{display:block;border:0;background:transparent;color:var(--primary-text-color);text-align:left;padding:0;font:inherit;font-size:2rem;font-weight:700;line-height:1.12;cursor:pointer}.primary small{display:block;font-size:.72rem;font-weight:500;color:var(--cx-muted);margin-top:3px}.primary.compact{font-size:1.5rem}.ci{margin:9px 0 7px}.ci-label{display:flex;justify-content:space-between;gap:6px;color:var(--cx-muted);font-size:.68rem}.ci-label span:nth-child(2){text-align:center}.track{height:5px;border-radius:5px;background:color-mix(in srgb,var(--cx-accent) 22%,var(--cx-line));position:relative;margin-top:5px}.track i{position:absolute;width:3px;height:11px;top:-3px;background:var(--primary-text-color);border-radius:2px}.submetric{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap;font-size:.8rem}.submetric span,.caption,.source{color:var(--cx-muted);font-size:.74rem;line-height:1.3}.source{font-size:.68rem;margin-top:5px}.delta{font-size:.82rem;font-weight:650;margin:2px 0 4px}.compare{display:grid;gap:5px;margin:7px 0}.compare>div{display:grid;gap:3px;font-size:.7rem;color:var(--cx-muted)}.compare i,.bar{height:7px;background:color-mix(in srgb,var(--cx-accent) 13%,var(--cx-line));border-radius:6px;overflow:hidden;display:block}.compare b,.bar i{height:100%;display:block;border-radius:inherit;background:var(--cx-accent)}.rooms{display:grid;gap:2px}.room-row{width:100%;display:grid;grid-template-columns:minmax(78px,1fr) minmax(50px,1.5fr) auto;align-items:center;gap:8px;border:0;background:transparent;color:var(--primary-text-color);text-align:left;font:inherit;padding:3px 1px;font-size:.73rem}.room-action{cursor:pointer}.room-row b{font-size:.72rem;font-weight:600;white-space:nowrap}.base-layout{display:grid;grid-template-columns:78px 1fr;align-items:center;gap:12px}.donut{width:72px;height:72px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--cx-accent) var(--p),var(--cx-line) 0);position:relative}.donut:before{content:"";position:absolute;inset:8px;border-radius:50%;background:var(--cx-card)}.donut span{position:relative;font-weight:700;font-size:1.1rem}.donut small{font-size:.7rem}.donut.missing{background:var(--cx-line)}.chip{display:inline-block;border:1px solid var(--cx-line);border-radius:999px;padding:4px 8px;color:var(--cx-muted);font-size:.72rem;margin-top:5px}.measured{border-top:1px solid var(--cx-line);margin-top:7px;padding-top:6px;font-size:.75rem}.quiet,.dash{color:var(--cx-muted)}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
      @container (min-width:650px){.wrap{padding:20px}.top{margin-bottom:18px}.top h1{font-size:1.5rem}.nav{font-size:.95rem;padding:10px 14px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.panel{padding:18px;min-height:240px}.panel h2{font-size:1.1rem}.info{font-size:1.2rem}.primary{font-size:2.8rem}.primary small{font-size:.95rem;line-height:1.4}.primary.compact{font-size:2.1rem}.caption,.submetric span{font-size:.95rem;line-height:1.45}.source,.ci-label{font-size:.85rem;line-height:1.4}.submetric{font-size:.95rem}.delta{font-size:1rem}.compare>div{font-size:.9rem}.compare i,.bar{height:10px}.rooms{gap:2px}.room-row{font-size:.95rem;padding:6px 1px}.room-row b{font-size:.9rem}.base-layout{grid-template-columns:96px 1fr}.donut{width:90px;height:90px}.donut span{font-size:1.4rem}.donut small{font-size:.9rem}.chip{font-size:.9rem}.measured{font-size:.95rem}}
      @container (min-width:1000px){.wrap{padding:24px}.top h1{font-size:1.7rem}.grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}.panel{padding:22px;min-height:280px}.panel h2{font-size:1.2rem}.primary{font-size:3.4rem}.primary.compact{font-size:2.5rem}.caption,.submetric span{font-size:1rem}.source,.ci-label{font-size:.9rem}.compare>div{font-size:.95rem}.room-row{font-size:1rem;padding:8px 1px}.room-row b{font-size:.95rem}.base-layout{grid-template-columns:112px 1fr}.donut{width:104px;height:104px}}
      @media(prefers-color-scheme:dark){.panel{background:color-mix(in srgb,var(--cx-card) 97%,white 3%)}}
    </style><div class="wrap"><header class="top"><h1>${climateXEsc(this._config.title||"Home efficiency")}</h1><button class="nav" type="button">${climateXEsc(this._config.navigation_label||"Trends & evidence")} ↗</button></header><div class="grid">${panels.join("")}</div></div>`;
    this.shadowRoot.querySelectorAll("[data-info]").forEach(b=>b.addEventListener("click",()=>this._info(b.getAttribute("data-info"))));
    this.shadowRoot.querySelector(".nav")?.addEventListener("click",()=>{
      const path=this._config.navigation_path;
      if(!path)return;
      if(typeof this._hass?.navigate==="function") this._hass.navigate(path);
      else { history.pushState(null,"",path); window.dispatchEvent(new Event("location-changed")); }
    });
  }
}

if (!customElements.get("climate-x-glance-card")) customElements.define("climate-x-glance-card", ClimateXGlanceCard);
window.customCards = window.customCards || [];
window.customCards.push({type:"climate-x-glance-card",name:"Climate X Glance",description:"Responsive home-efficiency KPIs and room cooling bars",preview:true});
