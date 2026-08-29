const REFRESH_MS = 5000;

class ThreeDBleMapPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._adapters = null;
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
    // Poll rather than subscribe: the adapter list changes rarely, and this
    // keeps the first version free of a server-side subscription to maintain.
    this._timer = setInterval(() => this._refresh(), REFRESH_MS);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
  }

  async _refresh() {
    if (!this._hass) return;
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "threed_ble_map/adapters",
      });
      this._adapters = result.adapters;
      this._error = null;
    } catch (err) {
      this._error = err.message || "Could not load Bluetooth adapters.";
    }
    this._renderBody();
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
        .sub {
          color: var(--secondary-text-color);
          font-size: 14px;
          margin-bottom: 24px;
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
        .pill {
          display: inline-block;
          padding: 2px 8px;
          border-radius: 12px;
          font-size: 12px;
          line-height: 18px;
        }
        .ok { background: rgba(76,175,80,.16); color: var(--success-color, #4caf50); }
        .bad { background: rgba(244,67,54,.16); color: var(--error-color, #f44336); }
        .muted { color: var(--secondary-text-color); }
        .msg { padding: 24px; color: var(--secondary-text-color); }
        .msg.err { color: var(--error-color, #f44336); }
      </style>
      <h1>3D BLE Map</h1>
      <div class="sub">Bluetooth adapters and proxies reporting to Home Assistant.</div>
      <div class="card"><div class="msg">Loading adapters…</div></div>
    `;
  }

  _renderBody() {
    const card = this.shadowRoot.querySelector(".card");
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
        <td>${a.device_count}</td>
        <td>${a.seconds_since_last_detection === null ? '<span class="muted">—</span>' : `${a.seconds_since_last_detection}s ago`}</td>
      </tr>
    `).join("");

    card.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Adapter</th><th>Source</th><th>Area</th><th>State</th>
            <th>Mode</th><th>Devices seen</th><th>Last detection</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }
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
