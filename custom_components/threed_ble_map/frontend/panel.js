const REFRESH_MS = 5000;
const SIGNAL_LIMIT = 20;

class ThreeDBleMapPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._adapters = null;
    this._signals = null;
    this._signalTotal = 0;
    this._error = null;
    this._timer = null;
    this._rendered = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._refresh();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    this._renderShell();
    // Poll rather than subscribe: these lists change slowly, and this keeps the
    // first version free of a server-side subscription to maintain.
    this._timer = setInterval(() => this._refresh(), REFRESH_MS);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
  }

  async _refresh() {
    if (!this._hass) return;
    try {
      const [adapters, signals] = await Promise.all([
        this._hass.connection.sendMessagePromise({
          type: "threed_ble_map/adapters",
        }),
        this._hass.connection.sendMessagePromise({
          type: "threed_ble_map/signals",
          limit: SIGNAL_LIMIT,
        }),
      ]);
      this._adapters = adapters.adapters;
      this._signals = signals.signals;
      this._signalTotal = signals.total;
      this._error = null;
    } catch (err) {
      this._error = err.message || "Could not load Bluetooth data.";
    }
    this._renderAdapters();
    this._renderSignals();
  }

  _renderShell() {
    if (this._rendered) return;
    this._rendered = true;
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          background: var(--primary-background-color);
          min-height: 100vh;
          box-sizing: border-box;
          padding: 24px;
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
          color: var(--primary-text-color);
        }
        h1 {
          font-size: 24px;
          font-weight: 400;
          margin: 0 0 4px;
        }
        h2 {
          font-size: 18px;
          font-weight: 400;
          margin: 32px 0 4px;
        }
        .sub {
          color: var(--secondary-text-color);
          font-size: 14px;
          margin-bottom: 16px;
        }
        .card {
          background: var(--card-background-color);
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.14));
          overflow-x: auto;
        }
        table {
          border-collapse: collapse;
          width: 100%;
          font-size: 14px;
        }
        th, td {
          text-align: left;
          padding: 12px 16px;
          border-bottom: 1px solid var(--divider-color);
          white-space: nowrap;
        }
        th {
          font-weight: 500;
          color: var(--secondary-text-color);
        }
        tr:last-child td { border-bottom: none; }
        .mono {
          font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
          color: var(--secondary-text-color);
        }
        .num { text-align: right; }
        .pill {
          display: inline-block;
          padding: 2px 8px;
          border-radius: 12px;
          font-size: 12px;
          line-height: 18px;
        }
        .ok { background: rgba(76,175,80,.16); color: var(--success-color, #4caf50); }
        .bad { background: rgba(244,67,54,.16); color: var(--error-color, #f44336); }
        .count {
          display: inline-block;
          min-width: 22px;
          text-align: center;
          padding: 2px 8px;
          border-radius: 12px;
          font-size: 12px;
          line-height: 18px;
          background: rgba(3,169,244,.16);
          color: var(--info-color, #039be5);
        }
        .count.solvable {
          background: rgba(76,175,80,.16);
          color: var(--success-color, #4caf50);
        }
        .muted { color: var(--secondary-text-color); }
        .heard {
          color: var(--secondary-text-color);
          font-size: 13px;
          white-space: normal;
        }
        .msg { padding: 24px; color: var(--secondary-text-color); }
        .msg.err { color: var(--error-color, #f44336); }
      </style>
      <h1>3D BLE Map</h1>
      <div class="sub">Bluetooth adapters and proxies reporting to Home Assistant.</div>
      <div class="card" id="adapters"><div class="msg">Loading adapters…</div></div>

      <h2>Signals</h2>
      <div class="sub" id="signals-sub">Loading signals…</div>
      <div class="card" id="signals"><div class="msg">Loading signals…</div></div>
    `;
  }

  _renderAdapters() {
    const card = this.shadowRoot.getElementById("adapters");
    if (!card) return;

    if (this._error) {
      card.innerHTML = `<div class="msg err">${escapeHtml(this._error)}</div>`;
      return;
    }
    if (!this._adapters) return;
    if (!this._adapters.length) {
      card.innerHTML = `<div class="msg">No Bluetooth adapters found. Set up the Bluetooth integration or add an ESPHome Bluetooth proxy.</div>`;
      return;
    }

    const rows = this._adapters.map((a) => `
      <tr>
        <td>${escapeHtml(a.device_name || a.name)}</td>
        <td class="mono">${escapeHtml(a.source)}</td>
        <td>${a.area ? escapeHtml(a.area) : '<span class="muted">—</span>'}</td>
        <td>${pill(a.scanning, "scanning", "stopped")}</td>
        <td>${pill(a.connectable, "connectable", "advertisements only")}</td>
        <td class="num">${a.device_count}</td>
        <td>${a.seconds_since_last_detection === null ? '<span class="muted">—</span>' : `${a.seconds_since_last_detection}s ago`}</td>
      </tr>
    `).join("");

    card.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Adapter</th><th>Source</th><th>Area</th><th>State</th>
            <th>Mode</th><th class="num">Devices seen</th><th>Last detection</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  _renderSignals() {
    const card = this.shadowRoot.getElementById("signals");
    const sub = this.shadowRoot.getElementById("signals-sub");
    if (!card || !sub) return;

    if (this._error) {
      sub.textContent = "";
      card.innerHTML = `<div class="msg err">${escapeHtml(this._error)}</div>`;
      return;
    }
    if (!this._signals) return;

    sub.textContent =
      `Top ${Math.min(SIGNAL_LIMIT, this._signals.length)} of ${this._signalTotal} ` +
      `BLE addresses, by how many radios hear them. ` +
      `Three or more is enough to place a device in 2D, four with height for 3D.`;

    if (!this._signals.length) {
      card.innerHTML = `<div class="msg">No BLE devices are currently being heard.</div>`;
      return;
    }

    const rows = this._signals.map((s) => `
      <tr>
        <td>${s.name ? escapeHtml(s.name) : '<span class="muted">unnamed</span>'}</td>
        <td class="mono">${escapeHtml(s.address)}</td>
        <td class="num"><span class="count${s.scanner_count >= 3 ? " solvable" : ""}">${s.scanner_count}</span></td>
        <td class="num">${s.best_rssi === null ? '<span class="muted">—</span>' : `${s.best_rssi} dBm`}</td>
        <td class="heard">${s.heard_by.map(formatHeardBy).join(", ")}</td>
      </tr>
    `).join("");

    card.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Name</th><th>Address</th><th class="num">Radios</th>
            <th class="num">Best RSSI</th><th>Heard by</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }
}

function formatHeardBy(entry) {
  const rssi = entry.rssi === null ? "—" : `${entry.rssi}`;
  return `${escapeHtml(entry.label)} <span class="mono">${rssi}</span>`;
}

function pill(value, yes, no) {
  return `<span class="pill ${value ? "ok" : "bad"}">${value ? yes : no}</span>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

customElements.define("threed-ble-map-panel", ThreeDBleMapPanel);
