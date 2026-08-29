// The module URL carries a cache-busting token; pass it on so an edit to an
// imported module is picked up too. The renderer is imported lazily rather than
// with a top-level await: awaiting here would delay customElements.define past
// the point Home Assistant creates the element, and an element created before
// its definition keeps `hass` as an own property that shadows the setter below.
const VERSION = new URL(import.meta.url).searchParams.get("v") || "";
let scenePromise = null;

function loadScene() {
  if (!scenePromise) {
    scenePromise = import(`./map3d.js${VERSION ? `?v=${VERSION}` : ""}`);
  }
  return scenePromise;
}

const REFRESH_MS = 5000;
const SIGNAL_LIMIT = 20;
const BASE_PATH = "/threed-ble-map";

const FLOOR_COLORS = ["#42a5f5", "#66bb6a", "#ab47bc", "#ffa726"];
const UNKNOWN_FLOOR_COLOR = "#78909c";

class ThreeDBleMapPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._view = pathToView(location.pathname);
    this._adapters = null;
    this._signals = null;
    this._signalTotal = 0;
    this._map = null;
    this._error = null;
    this._timer = null;
    this._scene = null;
    this._sceneLoading = false;
    this._showEdges = true;
    this._rendered = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._refresh();
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    // If this element was created before its definition loaded, `hass` is an
    // own property sitting in front of the setter. Re-assign it so the setter
    // actually runs; without this the panel never receives any data.
    this._upgradeProperty("hass");
    this._renderShell();
    this._timer = setInterval(() => this._refresh(), REFRESH_MS);
    this._onResize = () => this._scene && this._scene.resize();
    window.addEventListener("resize", this._onResize);
    this._onPopState = () => this._setView(pathToView(location.pathname), false);
    window.addEventListener("popstate", this._onPopState);
  }

  _upgradeProperty(name) {
    if (Object.prototype.hasOwnProperty.call(this, name)) {
      const value = this[name];
      delete this[name];
      this[name] = value;
    }
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
    window.removeEventListener("resize", this._onResize);
    window.removeEventListener("popstate", this._onPopState);
  }

  async _refresh() {
    if (!this._hass) return;
    const send = (message) => this._hass.connection.sendMessagePromise(message);
    try {
      if (this._view === "map") {
        this._map = await send({ type: "threed_ble_map/anchor_map" });
      } else {
        const [adapters, signals] = await Promise.all([
          send({ type: "threed_ble_map/adapters" }),
          send({ type: "threed_ble_map/signals", limit: SIGNAL_LIMIT }),
        ]);
        this._adapters = adapters.adapters;
        this._signals = signals.signals;
        this._signalTotal = signals.total;
      }
      this._error = null;
    } catch (err) {
      this._error = err.message || "Could not load Bluetooth data.";
    }
    this._renderView();
  }

  _setView(view, push = true) {
    if (view === this._view) return;
    this._view = view;
    if (push) {
      history.pushState(
        null,
        "",
        view === "map" ? `${BASE_PATH}/map` : BASE_PATH,
      );
    }
    this._scene = null;
    this._sceneLoading = false;
    this._renderView();
    this._refresh();
  }

  _renderShell() {
    if (this._rendered) return;
    this._rendered = true;
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <h1>3D BLE Map</h1>
      <div class="tabs">
        <button data-view="signals">Adapters &amp; signals</button>
        <button data-view="map">Anchor map</button>
      </div>
      <div id="view"></div>
    `;
    for (const button of this.shadowRoot.querySelectorAll(".tabs button")) {
      button.addEventListener("click", () =>
        this._setView(button.dataset.view),
      );
    }
    this._renderView();
  }

  _renderView() {
    const container = this.shadowRoot.getElementById("view");
    if (!container) return;

    for (const button of this.shadowRoot.querySelectorAll(".tabs button")) {
      button.classList.toggle("active", button.dataset.view === this._view);
    }

    if (this._error) {
      container.innerHTML = `<div class="card"><div class="msg err">${escapeHtml(this._error)}</div></div>`;
      return;
    }
    if (this._view === "map") this._renderMap(container);
    else this._renderSignalsView(container);
  }

  // ---------------------------------------------------------------- adapters

  _renderSignalsView(container) {
    if (!this._adapters) {
      container.innerHTML = `<div class="card"><div class="msg">Loading…</div></div>`;
      return;
    }

    container.innerHTML = `
      <div class="sub">Bluetooth adapters and proxies reporting to Home Assistant.</div>
      <div class="card">${this._adapterTable()}</div>
      <h2>Signals</h2>
      <div class="sub">${this._signalSummary()}</div>
      <div class="card">${this._signalTable()}</div>
    `;
  }

  _adapterTable() {
    if (!this._adapters.length) {
      return `<div class="msg">No Bluetooth adapters found. Set up the Bluetooth integration or add an ESPHome Bluetooth proxy.</div>`;
    }
    const rows = this._adapters
      .map(
        (a) => `
      <tr>
        <td>${escapeHtml(a.device_name || a.name)}</td>
        <td class="mono">${escapeHtml(a.source)}</td>
        <td>${a.area ? escapeHtml(a.area) : dash()}</td>
        <td>${pill(a.scanning, "scanning", "stopped")}</td>
        <td>${pill(a.connectable, "connectable", "advertisements only")}</td>
        <td class="num">${a.device_count}</td>
        <td>${a.seconds_since_last_detection === null ? dash() : `${a.seconds_since_last_detection}s ago`}</td>
      </tr>`,
      )
      .join("");
    return `<table><thead><tr>
        <th>Adapter</th><th>Source</th><th>Area</th><th>State</th>
        <th>Mode</th><th class="num">Devices seen</th><th>Last detection</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  }

  _signalSummary() {
    if (!this._signals) return "Loading signals…";
    return (
      `Top ${Math.min(SIGNAL_LIMIT, this._signals.length)} of ${this._signalTotal} ` +
      `BLE addresses, by how many radios hear them. ` +
      `Three or more is enough to place a device in 2D, four with height for 3D.`
    );
  }

  _signalTable() {
    if (!this._signals) return `<div class="msg">Loading signals…</div>`;
    if (!this._signals.length) {
      return `<div class="msg">No BLE devices are currently being heard.</div>`;
    }
    const rows = this._signals
      .map(
        (s) => `
      <tr>
        <td>${s.name ? escapeHtml(s.name) : '<span class="muted">unnamed</span>'}</td>
        <td class="mono">${escapeHtml(s.address)}</td>
        <td class="num"><span class="count${s.scanner_count >= 3 ? " solvable" : ""}">${s.scanner_count}</span></td>
        <td class="num">${s.best_rssi === null ? dash() : `${s.best_rssi} dBm`}</td>
        <td class="heard">${s.heard_by.map(formatHeardBy).join(", ")}</td>
      </tr>`,
      )
      .join("");
    return `<table><thead><tr>
        <th>Name</th><th>Address</th><th class="num">Radios</th>
        <th class="num">Best RSSI</th><th>Heard by</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  }

  // --------------------------------------------------------------- anchor map

  _renderMap(container) {
    const map = this._map;
    if (!map) {
      container.innerHTML = `<div class="card"><div class="msg">Loading…</div></div>`;
      return;
    }

    if (!map.ready) {
      const remaining = Math.max(0, map.min_seconds - map.elapsed);
      container.innerHTML = `
        <div class="sub">${MAP_BLURB}</div>
        <div class="card"><div class="msg">
          Collecting signal history — ${map.elapsed}s of ${map.min_seconds}s.
          Ready in about ${remaining}s.
        </div></div>`;
      this._scene = null;
      return;
    }

    const colors = floorColors(map.anchors);

    if (!this._scene && !this._sceneLoading) {
      container.innerHTML = `
        <div class="sub">${MAP_BLURB}</div>
        <div class="stats" id="stats"></div>
        <div class="card canvas-card"><canvas id="scene"></canvas></div>
        <div class="controls">
          <label><input type="checkbox" id="show-edges" /> Show links between radios</label>
        </div>
        <div class="hint">Drag to orbit · scroll to zoom · solid lines are direct
          radio-to-radio links, dashed lines are inferred from shared beacons.
          Each plane sits just below the radios on that floor.</div>
        <h2>Radios</h2>
        <div class="sub">Gain is how far each radio reads from the group average,
          solved from the data rather than assumed. A radio reading hot would
          otherwise be placed too close to everything it hears.</div>
        <div class="card" id="radios"></div>
        <h2>Pair distances</h2>
        <div class="card" id="pairs"></div>`;
      const toggle = this.shadowRoot.getElementById("show-edges");
      toggle.checked = this._showEdges;
      toggle.addEventListener("change", () => {
        this._showEdges = toggle.checked;
        if (this._scene) this._scene.setShowEdges(this._showEdges);
      });

      this._sceneLoading = true;
      loadScene().then(({ AnchorScene }) => {
        this._sceneLoading = false;
        const canvas = this.shadowRoot.getElementById("scene");
        if (!canvas) return;
        this._scene = new AnchorScene(canvas);
        this._scene.setShowEdges(this._showEdges);
        this._scene.resize();
        this._renderView();
      });
    }

    const stats = this.shadowRoot.getElementById("stats");
    if (!stats) return;
    stats.innerHTML = this._mapStats(map);
    this.shadowRoot.getElementById("radios").innerHTML = this._radioTable(
      map,
      colors,
    );
    this.shadowRoot.getElementById("pairs").innerHTML = this._pairTable(
      map,
      colors,
    );

    if (map.error || !this._scene) {
      if (this._scene) this._scene.setData([], []);
      return;
    }

    const nodes = map.anchors
      .filter((anchor) => map.positions[anchor.source])
      .map((anchor) => ({
        id: anchor.source,
        label: anchor.label,
        floor: anchor.floor,
        color: colors.get(anchor.floor) || UNKNOWN_FLOOR_COLOR,
        ...map.positions[anchor.source],
      }));

    const edges = map.pairs.map((pair) => ({
      a: pair.a,
      b: pair.b,
      distance: pair.distance,
      inferred: pair.method === "inferred",
    }));

    this._scene.setData(nodes, edges);
  }

  _mapStats(map) {
    const solved = Object.keys(map.positions).length;
    const inferred = map.pairs.filter((p) => p.method === "inferred").length;
    const cards = [
      ["Anchors placed", `${solved} of ${map.anchors.length}`],
      ["Recording", `${formatDuration(map.elapsed)}`],
      [
        "Fit error",
        map.stress === null ? "—" : `${(map.stress * 100).toFixed(1)}%`,
      ],
      ["Inferred links", `${inferred} of ${map.pairs.length}`],
      [
        "Fit residual",
        map.residual_db === null ? "—" : `${map.residual_db} dB`,
      ],
      ["Calibrated", map.refined ? `${map.beacons_used} beacons` : "not enough data"],
    ];
    const stats = cards
      .map(
        ([label, value]) =>
          `<div class="stat"><div class="stat-value">${escapeHtml(value)}</div>
           <div class="stat-label">${escapeHtml(label)}</div></div>`,
      )
      .join("");
    const warning = map.error
      ? `<div class="card"><div class="msg err">${escapeHtml(map.error)}</div></div>`
      : "";
    return stats + warning;
  }

  _radioTable(map, colors) {
    const rows = map.anchors
      .map((anchor) => {
        const gain = map.gains ? map.gains[anchor.source] : undefined;
        const color = colors.get(anchor.floor) || UNKNOWN_FLOOR_COLOR;
        return `
      <tr>
        <td><span class="dot" style="background:${color}"></span>${escapeHtml(anchor.label)}</td>
        <td>${anchor.area ? escapeHtml(anchor.area) : dash()}</td>
        <td>${anchor.floor ? escapeHtml(anchor.floor) : dash()}</td>
        <td class="num">${gain === undefined ? dash() : `${gain > 0 ? "+" : ""}${gain} dB`}</td>
        <td class="num">${anchor.tracked_beacons}</td>
      </tr>`;
      })
      .join("");
    return `<table><thead><tr>
        <th>Radio</th><th>Area</th><th>Floor</th>
        <th class="num">Gain</th><th class="num">Beacons tracked</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  }

  _pairTable(map, colors) {
    if (!map.pairs.length) {
      return `<div class="msg">No anchor pairs could be measured yet.</div>`;
    }
    const labels = new Map(map.anchors.map((a) => [a.source, a.label]));
    const rows = map.pairs
      .slice()
      .sort((a, b) => a.distance - b.distance)
      .map(
        (p) => `
      <tr>
        <td>${escapeHtml(labels.get(p.a) || p.a)}</td>
        <td>${escapeHtml(labels.get(p.b) || p.b)}</td>
        <td class="num">${p.distance} m</td>
        <td>${
          p.method === "direct"
            ? '<span class="pill ok">direct link</span>'
            : '<span class="pill warn">inferred</span>'
        }</td>
        <td class="num">${p.shared_beacons}</td>
      </tr>`,
      )
      .join("");
    return `<table><thead><tr>
        <th>Anchor</th><th>Anchor</th><th class="num">Estimated distance</th>
        <th>Method</th><th class="num">Shared beacons</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  }
}

const MAP_BLURB =
  "A rough relative layout, solved from how loudly each radio hears the others " +
  "and the beacons they share. Position is relative: the shape is meaningful, " +
  "the origin is not. Note that the vertical axis is stretched — a floor " +
  "between two radios eats signal that the path-loss model reads as distance, " +
  "so storeys come out further apart than they are.";

const STYLES = `
  :host {
    display: block;
    background: var(--primary-background-color);
    min-height: 100vh;
    box-sizing: border-box;
    padding: 24px;
    font-family: var(--paper-font-body1_-_font-family, sans-serif);
    color: var(--primary-text-color);
  }
  h1 { font-size: 24px; font-weight: 400; margin: 0 0 16px; }
  h2 { font-size: 18px; font-weight: 400; margin: 32px 0 8px; }
  .sub { color: var(--secondary-text-color); font-size: 14px; margin-bottom: 16px; max-width: 70ch; }
  .hint { color: var(--secondary-text-color); font-size: 13px; margin-top: 8px; }
  .tabs { display: flex; gap: 8px; margin-bottom: 20px; border-bottom: 1px solid var(--divider-color); }
  .tabs button {
    background: none; border: none; cursor: pointer; font: inherit;
    padding: 10px 16px; color: var(--secondary-text-color);
    border-bottom: 2px solid transparent; margin-bottom: -1px;
  }
  .tabs button.active { color: var(--primary-color, #03a9f4); border-bottom-color: var(--primary-color, #03a9f4); }
  .card {
    background: var(--card-background-color);
    border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.14));
    overflow-x: auto;
  }
  .canvas-card { overflow: hidden; }
  canvas { width: 100%; height: 460px; display: block; touch-action: none; cursor: grab; }
  canvas:active { cursor: grabbing; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; }
  th, td { text-align: left; padding: 12px 16px; border-bottom: 1px solid var(--divider-color); white-space: nowrap; }
  th { font-weight: 500; color: var(--secondary-text-color); }
  tr:last-child td { border-bottom: none; }
  .mono { font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; color: var(--secondary-text-color); }
  .num { text-align: right; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; line-height: 18px; }
  .ok { background: rgba(76,175,80,.16); color: var(--success-color, #4caf50); }
  .bad { background: rgba(244,67,54,.16); color: var(--error-color, #f44336); }
  .warn { background: rgba(255,167,38,.16); color: var(--warning-color, #ffa726); }
  .count {
    display: inline-block; min-width: 22px; text-align: center; padding: 2px 8px;
    border-radius: 12px; font-size: 12px; line-height: 18px;
    background: rgba(3,169,244,.16); color: var(--info-color, #039be5);
  }
  .count.solvable { background: rgba(76,175,80,.16); color: var(--success-color, #4caf50); }
  .muted { color: var(--secondary-text-color); }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }
  .heard { color: var(--secondary-text-color); font-size: 13px; white-space: normal; }
  .msg { padding: 24px; color: var(--secondary-text-color); }
  .msg.err { color: var(--error-color, #f44336); }
  .controls { display: flex; gap: 20px; margin-top: 12px; font-size: 14px; }
  .controls label { display: flex; align-items: center; gap: 8px; cursor: pointer; }
  .controls input { accent-color: var(--primary-color, #03a9f4); cursor: pointer; }
  .stats { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }
  .stat {
    background: var(--card-background-color);
    border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.14));
    padding: 12px 20px; min-width: 120px;
  }
  .stat-value { font-size: 20px; }
  .stat-label { font-size: 12px; color: var(--secondary-text-color); margin-top: 2px; }
`;

function pathToView(pathname) {
  return pathname.startsWith(`${BASE_PATH}/map`) ? "map" : "signals";
}

function floorColors(anchors) {
  const floors = [...new Set(anchors.map((a) => a.floor).filter(Boolean))].sort();
  return new Map(floors.map((floor, i) => [floor, FLOOR_COLORS[i % FLOOR_COLORS.length]]));
}

function formatDuration(seconds) {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function formatHeardBy(entry) {
  const rssi = entry.rssi === null ? "—" : `${entry.rssi}`;
  return `${escapeHtml(entry.label)} <span class="mono">${rssi}</span>`;
}

function pill(value, yes, no) {
  return `<span class="pill ${value ? "ok" : "bad"}">${value ? yes : no}</span>`;
}

function dash() {
  return '<span class="muted">—</span>';
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

customElements.define("threed-ble-map-panel", ThreeDBleMapPanel);
