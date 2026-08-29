// Minimal orbiting 3D scatter renderer. Five labelled points and their links do
// not justify pulling in a WebGL library, and a 2D canvas keeps the integration
// dependency-free and inside Home Assistant's content security policy.

const BACKGROUND = "rgba(0,0,0,0)";
const NODE_RADIUS = 7;

export class AnchorScene {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.nodes = [];
    this.edges = [];
    this.azimuth = 0.9;
    this.elevation = 0.45;
    this.zoom = 1;
    this.hovered = null;
    this._bindPointer();
  }

  setData(nodes, edges) {
    this.nodes = nodes;
    this.edges = edges;
    this.draw();
  }

  _bindPointer() {
    let dragging = false;
    let lastX = 0;
    let lastY = 0;

    this.canvas.addEventListener("pointerdown", (event) => {
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
      this.canvas.setPointerCapture(event.pointerId);
    });

    this.canvas.addEventListener("pointermove", (event) => {
      if (!dragging) {
        this._updateHover(event);
        return;
      }
      this.azimuth -= (event.clientX - lastX) * 0.01;
      // Stop short of the poles so the scene never flips over.
      this.elevation = clamp(
        this.elevation + (event.clientY - lastY) * 0.01,
        -1.5,
        1.5,
      );
      lastX = event.clientX;
      lastY = event.clientY;
      this.draw();
    });

    const stop = (event) => {
      dragging = false;
      if (this.canvas.hasPointerCapture(event.pointerId)) {
        this.canvas.releasePointerCapture(event.pointerId);
      }
    };
    this.canvas.addEventListener("pointerup", stop);
    this.canvas.addEventListener("pointercancel", stop);

    this.canvas.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        this.zoom = clamp(this.zoom * (event.deltaY > 0 ? 0.9 : 1.1), 0.3, 4);
        this.draw();
      },
      { passive: false },
    );
  }

  _updateHover(event) {
    const rect = this.canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const projected = this._project();
    let found = null;
    for (const point of projected) {
      if (Math.hypot(point.sx - x, point.sy - y) <= NODE_RADIUS * 2) {
        found = point.node.id;
      }
    }
    if (found !== this.hovered) {
      this.hovered = found;
      this.draw();
    }
  }

  _project() {
    const { width, height } = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const cx = width / (2 * dpr);
    const cy = height / (2 * dpr);

    // Fit the whole scene in view regardless of how big the house is.
    const extent =
      Math.max(
        1,
        ...this.nodes.flatMap((n) => [
          Math.abs(n.x),
          Math.abs(n.y),
          Math.abs(n.z),
        ]),
      ) * 1.6;
    const scale = (Math.min(cx, cy) / extent) * this.zoom;

    const cosA = Math.cos(this.azimuth);
    const sinA = Math.sin(this.azimuth);
    const cosE = Math.cos(this.elevation);
    const sinE = Math.sin(this.elevation);

    return this.nodes.map((node) => {
      // Rotate about the vertical axis, then tilt. z is up in the solver's
      // output, so it maps to screen-up here.
      const x = node.x * cosA - node.y * sinA;
      const depth = node.x * sinA + node.y * cosA;
      const y = node.z * cosE - depth * sinE;
      return {
        node,
        sx: cx + x * scale,
        sy: cy - y * scale,
        depth: depth * cosE + node.z * sinE,
      };
    });
  }

  draw() {
    const ctx = this.ctx;
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.fillStyle = BACKGROUND;

    if (!this.nodes.length) return;

    const projected = this._project();
    const byId = new Map(projected.map((point) => [point.node.id, point]));
    const styles = getComputedStyle(this.canvas);
    const textColor = styles.getPropertyValue("--primary-text-color") || "#fff";
    const mutedColor =
      styles.getPropertyValue("--secondary-text-color") || "#888";

    this._drawGround(ctx, projected, mutedColor);

    // Painter's algorithm: far edges and nodes first.
    for (const edge of [...this.edges].sort(
      (a, b) =>
        (byId.get(a.a)?.depth ?? 0) + (byId.get(a.b)?.depth ?? 0) -
        ((byId.get(b.a)?.depth ?? 0) + (byId.get(b.b)?.depth ?? 0)),
    )) {
      const from = byId.get(edge.a);
      const to = byId.get(edge.b);
      if (!from || !to) continue;
      ctx.beginPath();
      ctx.moveTo(from.sx, from.sy);
      ctx.lineTo(to.sx, to.sy);
      ctx.strokeStyle = edge.inferred
        ? "rgba(255,167,38,.55)"
        : "rgba(3,169,244,.45)";
      ctx.lineWidth = edge.inferred ? 1 : 1.5;
      if (edge.inferred) ctx.setLineDash([4, 4]);
      ctx.stroke();
      ctx.setLineDash([]);

      const midX = (from.sx + to.sx) / 2;
      const midY = (from.sy + to.sy) / 2;
      ctx.fillStyle = mutedColor;
      ctx.font = "11px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.fillText(`${edge.distance}m`, midX, midY - 3);
    }

    for (const point of [...projected].sort((a, b) => a.depth - b.depth)) {
      const isHovered = this.hovered === point.node.id;
      ctx.beginPath();
      ctx.arc(point.sx, point.sy, NODE_RADIUS, 0, Math.PI * 2);
      ctx.fillStyle = point.node.color;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = isHovered ? textColor : "rgba(0,0,0,.35)";
      ctx.stroke();

      ctx.fillStyle = textColor;
      ctx.font = `${isHovered ? "600 " : ""}13px sans-serif`;
      ctx.textAlign = "center";
      ctx.fillText(point.node.label, point.sx, point.sy - NODE_RADIUS - 8);

      if (point.node.sublabel) {
        ctx.fillStyle = mutedColor;
        ctx.font = "11px sans-serif";
        ctx.fillText(point.node.sublabel, point.sx, point.sy + NODE_RADIUS + 14);
      }
    }
  }

  _drawGround(ctx, projected, color) {
    // A vertical stem per anchor down to the lowest point in the scene, so the
    // eye can read height without a full floor grid.
    const floor = Math.min(...this.nodes.map((n) => n.z));
    const cosE = Math.cos(this.elevation);
    const sinE = Math.sin(this.elevation);
    const cosA = Math.cos(this.azimuth);
    const sinA = Math.sin(this.azimuth);
    const { width, height } = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const cx = width / (2 * dpr);
    const cy = height / (2 * dpr);
    const extent =
      Math.max(
        1,
        ...this.nodes.flatMap((n) => [
          Math.abs(n.x),
          Math.abs(n.y),
          Math.abs(n.z),
        ]),
      ) * 1.6;
    const scale = (Math.min(cx, cy) / extent) * this.zoom;

    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.25;
    ctx.lineWidth = 1;
    for (let i = 0; i < this.nodes.length; i += 1) {
      const node = this.nodes[i];
      const x = node.x * cosA - node.y * sinA;
      const depth = node.x * sinA + node.y * cosA;
      const baseY = floor * cosE - depth * sinE;
      ctx.beginPath();
      ctx.moveTo(projected[i].sx, projected[i].sy);
      ctx.lineTo(cx + x * scale, cy - baseY * scale);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = rect.width * dpr;
    this.canvas.height = rect.height * dpr;
    this.draw();
  }
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}
