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

const BEACON_ROWS = 25;

// Three ranges is the fewest that fix a point in 3D, and it is also the fewest
// the solver will place. Mirrors refine.MIN_RADIOS_PER_BEACON.
const MIN_BEACON_RADIOS = 3;

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
    this._showBeacons = true;
    this._expanded = false;
    this._rendered = false;
    this._mapSubscription = null;
    this._subscribing = false;
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
    // Escape is the way out of anything that covers the screen, so it has to
    // work here even though this is a page element rather than a real dialog.
    this._onKeyDown = (event) => {
      if (event.key === "Escape" && this._expanded) this._setExpanded(false);
    };
    window.addEventListener("keydown", this._onKeyDown);
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
    this._unsubscribeMap();
    window.removeEventListener("resize", this._onResize);
    window.removeEventListener("popstate", this._onPopState);
    window.removeEventListener("keydown", this._onKeyDown);
    this._setExpanded(false);
    // Unconditional, not just via _setExpanded: if the shadow root has already
    // gone the toggle cannot run, and a stranded scroll lock on document.body
    // outlives this element with no way left to undo it.
    document.body.style.overflow = "";
    this._expanded = false;
  }

  // Full screen within the browser window: the card is pinned over the
  // viewport rather than handed to the compositor. No permission prompt, no
  // display-mode change, and it composes with Home Assistant's own sidebar.
  //
  // The controls are *moved* into the card rather than duplicated, so the same
  // two checkboxes keep their existing listeners and state either way.
  _setExpanded(expanded) {
    if (!this.shadowRoot) return;
    const card = this.shadowRoot.getElementById("canvas-card");
    const button = this.shadowRoot.getElementById("expand");
    const controls = this.shadowRoot.querySelector(".controls");
    if (!card) {
      this._expanded = false;
      document.body.style.overflow = "";
      return;
    }
    if (this._expanded === expanded) return;
    this._expanded = expanded;

    card.classList.toggle("expanded", expanded);
    if (button) {
      button.textContent = expanded ? "Exit" : "Expand";
      button.title = expanded
        ? "Back to the page (Esc)"
        : "Fill the browser window (Esc to exit)";
    }
    if (controls) {
      if (expanded) {
        this._controlsHome = controls.parentNode;
        this._controlsNext = controls.nextSibling;
        card.appendChild(controls);
      } else if (this._controlsHome) {
        this._controlsHome.insertBefore(controls, this._controlsNext);
      }
    }
    // The page behind must not scroll under a fixed overlay.
    document.body.style.overflow = expanded ? "hidden" : "";

    // The canvas backing store is sized from its measured box, so it can only
    // be resized once the browser has laid the new geometry out.
    requestAnimationFrame(() => this._scene && this._scene.resize());
  }

  // The map arrives by subscription rather than by asking. The integration
  // recomputes it on its own clock and pushes each update, so a poll would only
  // ever re-fetch numbers that had already been sent, and would deliver them
  // somewhere between nothing and one poll late.
  async _subscribeMap() {
    if (this._mapSubscription || this._subscribing || !this._hass) return;
    this._subscribing = true;
    try {
      const unsubscribe = await this._hass.connection.subscribeMessage(
        (map) => {
          // A stale subscription must not paint over the signals view after a
          // tab change that raced the unsubscribe.
          if (this._view !== "map") return;
          this._map = map;
          this._error = null;
          this._renderView();
        },
        { type: "threed_ble_map/subscribe" },
      );
      // Leaving the map while the subscription was being set up: tear it down
      // rather than leaving the server pushing to nobody.
      if (this._view === "map" && this.isConnected) {
        this._mapSubscription = unsubscribe;
      } else {
        unsubscribe();
      }
    } catch (err) {
      this._error = err.message || "Could not subscribe to the map.";
      this._renderView();
    } finally {
      this._subscribing = false;
    }
  }

  _unsubscribeMap() {
    if (!this._mapSubscription) return;
    const unsubscribe = this._mapSubscription;
    this._mapSubscription = null;
    unsubscribe();
  }

  async _refresh() {
    if (!this._hass) return;
    const send = (message) => this._hass.connection.sendMessagePromise(message);
    try {
      if (this._view === "map") {
        this._subscribeMap();
        return;
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
    // The overlay belongs to the map; leaving the tab must take it down.
    this._setExpanded(false);
    this._unsubscribeMap();
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
        <div class="card canvas-card" id="canvas-card">
          <canvas id="scene"></canvas>
          <button class="expand-button" id="expand" type="button"
                  title="Fill the browser window (Esc to exit)">Expand</button>
        </div>
        <div class="controls">
          <label><input type="checkbox" id="show-edges" /> Show links between radios</label>
          <label><input type="checkbox" id="show-beacons" /> Show beacons</label>
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
        <div class="card" id="pairs"></div>
        <h2>Beacons</h2>
        <div class="sub">${BEACON_BLURB}</div>
        <div class="card" id="beacons"></div>`;
      const toggle = this.shadowRoot.getElementById("show-edges");
      toggle.checked = this._showEdges;
      toggle.addEventListener("change", () => {
        this._showEdges = toggle.checked;
        if (this._scene) this._scene.setShowEdges(this._showEdges);
      });

      const beaconToggle = this.shadowRoot.getElementById("show-beacons");
      beaconToggle.checked = this._showBeacons;
      beaconToggle.addEventListener("change", () => {
        this._showBeacons = beaconToggle.checked;
        if (this._scene) this._scene.setShowBeacons(this._showBeacons);
      });

      this._sceneLoading = true;
      loadScene().then(({ AnchorScene }) => {
        this._sceneLoading = false;
        const expand = this.shadowRoot.getElementById("expand");
        if (expand) {
          expand.addEventListener("click", () =>
            this._setExpanded(!this._expanded),
          );
        }
        const canvas = this.shadowRoot.getElementById("scene");
        if (!canvas) return;
        this._scene = new AnchorScene(canvas);
        this._scene.setShowEdges(this._showEdges);
        this._scene.setShowBeacons(this._showBeacons);
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
    this.shadowRoot.getElementById("beacons").innerHTML = this._beaconTable(map);

    if (map.error || !this._scene) {
      if (this._scene) this._scene.setData([], [], []);
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

    const beacons = (map.beacons || []).map((beacon) => ({
      id: `beacon:${beacon.address}`,
      label: beaconLabel(beacon),
      beacon: true,
      uncertainty: beacon.uncertainty_m,
      x: beacon.x,
      y: beacon.y,
      z: beacon.z,
    }));

    this._scene.setData(nodes, edges, beacons);
  }

  _beaconTable(map) {
    const beacons = (map.beacons || [])
      .slice()
      .sort((a, b) => (b.trust ?? 1) - (a.trust ?? 1) || (b.rssi ?? -127) - (a.rssi ?? -127));
    if (!beacons.length) {
      return `<div class="msg">No beacon is heard by ${MIN_BEACON_RADIOS} radios
        yet, which is the fewest that can fix a point in 3D.</div>`;
    }
    const rows = beacons
      .slice(0, BEACON_ROWS)
      .map((beacon) => {
        // The ring on the map is the same number. A beacon whose uncertainty
        // exceeds its distance to the nearest radio is, in plain terms, only
        // located to "somewhere in the house".
        const vague = beacon.uncertainty_m >= beacon.nearest_m;
        const trust = beacon.trust ?? 1;
        const trustClass = trust >= 0.6 ? "ok" : trust >= 0.3 ? "warn" : "bad";
        return `
      <tr>
        <td>${escapeHtml(beaconLabel(beacon))}${
          beacon.known_device
            ? ' <span class="pill info" title="Home Assistant manages this device">known</span>'
            : ""
        }</td>
        <td class="num">
          <span class="pill ${trustClass}" title="${trustTitle(beacon)}">${trust.toFixed(
            2,
          )}</span>
        </td>
        <td class="num">
          <span class="pill ${vague ? "warn" : "ok"}">±${beacon.uncertainty_m} m</span>
        </td>
        <td>${beacon.nearest_anchor ? escapeHtml(beacon.nearest_anchor) : dash()}</td>
        <td>${beacon.nearest_area ? escapeHtml(beacon.nearest_area) : dash()}</td>
        <td class="num">${beacon.radios}</td>
        <td class="num">${beacon.rssi === null ? dash() : `${beacon.rssi} dBm`}</td>
        <td class="mono">${escapeHtml(beacon.address)}</td>
      </tr>`;
      })
      .join("");
    const more =
      beacons.length > BEACON_ROWS
        ? `<div class="hint" style="padding:0 16px 14px">
             Showing the ${BEACON_ROWS} strongest of ${beacons.length} placed.</div>`
        : "";
    return `<table><thead><tr>
        <th>Beacon</th><th class="num">Trust</th><th class="num">Uncertainty</th>
        <th>Nearest radio</th><th>Area</th><th class="num">Radios</th>
        <th class="num">Strongest</th><th>Address</th>
      </tr></thead><tbody>${rows}</tbody></table>${more}`;
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
      [
        "Beacons placed",
        `${(map.beacons || []).length} of ${map.tracked_beacons ?? 0}`,
      ],
      ["Trusted landmarks", `${map.weighted_beacons ?? 0}`],
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
  "the origin is not. Heights are bounded by how houses are built rather than " +
  "by RSSI, which says little that is trustworthy about the vertical axis: " +
  "radios on one storey are held within a ceiling’s height of each other, and " +
  "storeys a floor-to-floor pitch apart. Real height differences within a " +
  "storey are flattened as a result — that detail is below the noise anyway.";

const BEACON_BLURB =
  "Every beacon heard by at least three radios, placed by the same solve that " +
  "positions the radios themselves. Read the uncertainty column before reading " +
  "the position: at the noise a real house produces, a beacon\u2019s range " +
  "error is a large fraction of its distance, and the figure is a lower bound " +
  "\u2014 it assumes the radios surround the beacon, which they often do not. " +
  "The nearest radio is a direct measurement and stays right when the " +
  "coordinates are shaky.";

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
  .canvas-card { overflow: hidden; position: relative; }
  /* Tall enough to be worth orbiting, but bounded at both ends: a short laptop
     window should still show the stats above it, and a tall monitor should not
     stretch the scene into a strip. */
  canvas {
    width: 100%; height: clamp(460px, 68vh, 900px);
    display: block; touch-action: none; cursor: grab;
  }
  canvas:active { cursor: grabbing; }

  /* Full screen *within the page*, not the system compositor: no permission
     prompt, no mode switch, and Home Assistant's own chrome stays one Escape
     away. */
  .canvas-card.expanded {
    position: fixed; inset: 0; z-index: 10;
    margin: 0; border-radius: 0; border: 0;
  }
  .canvas-card.expanded canvas { height: 100%; }
  .canvas-card.expanded .controls {
    position: absolute; left: 16px; bottom: 16px; margin: 0;
    padding: 8px 12px; border-radius: 10px;
    background: rgba(0, 0, 0, .55); backdrop-filter: blur(4px);
  }
  .expand-button {
    position: absolute; top: 12px; right: 12px; z-index: 1;
    padding: 6px 12px; border-radius: 8px; cursor: pointer;
    font: inherit; font-size: 13px; color: inherit;
    border: 1px solid var(--divider-color, rgba(127,127,127,.4));
    background: rgba(0, 0, 0, .45); backdrop-filter: blur(4px);
  }
  .expand-button:hover { background: rgba(0, 0, 0, .7); }
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
  .info { background: rgba(3,169,244,.16); color: var(--info-color, #039be5); }
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

function trustTitle(beacon) {
  const bits = [];
  if (beacon.motion != null) bits.push(`motion ${beacon.motion} dB`);
  if (beacon.persistence != null)
    bits.push(`present ${Math.round(beacon.persistence * 100)}% of the recording`);
  if (beacon.address_kind) bits.push(beacon.address_kind + " address");
  if (beacon.known_device) bits.push("known to Home Assistant");
  return bits.join(" · ");
}

function beaconLabel(beacon) {
  // Most beacons never advertise a name. The last three octets are enough to
  // tell them apart and short enough to sit under a dot on the map.
  return beacon.name || beacon.address.split(":").slice(-3).join(":");
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
