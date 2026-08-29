// Minimal orbiting 3D scatter renderer. Five labelled points and their links do
// not justify pulling in a WebGL library, and a 2D canvas keeps the integration
// dependency-free and inside Home Assistant's content security policy.

const NODE_RADIUS = 7;
const GRID_DIVISIONS = 6;

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

  _camera() {
    const dpr = window.devicePixelRatio || 1;
    const cx = this.canvas.width / (2 * dpr);
    const cy = this.canvas.height / (2 * dpr);

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

    return {
      cx,
      cy,
      scale: (Math.min(cx, cy) / extent) * this.zoom,
      cosA: Math.cos(this.azimuth),
      sinA: Math.sin(this.azimuth),
      cosE: Math.cos(this.elevation),
      sinE: Math.sin(this.elevation),
    };
  }

  _projectPoint(camera, x, y, z) {
    // Rotate about the vertical axis, then tilt. z is up in the solver's
    // output, so it maps to screen-up here.
    const px = x * camera.cosA - y * camera.sinA;
    const depth = x * camera.sinA + y * camera.cosA;
    const py = z * camera.cosE - depth * camera.sinE;
    return {
      sx: camera.cx + px * camera.scale,
      sy: camera.cy - py * camera.scale,
      depth: depth * camera.cosE + z * camera.sinE,
    };
  }

  _project() {
    const camera = this._camera();
    return this.nodes.map((node) => ({
      node,
      ...this._projectPoint(camera, node.x, node.y, node.z),
    }));
  }

  draw() {
    const ctx = this.ctx;
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

    if (!this.nodes.length) return;

    const camera = this._camera();
    const projected = this.nodes.map((node) => ({
      node,
      ...this._projectPoint(camera, node.x, node.y, node.z),
    }));
    const byId = new Map(projected.map((point) => [point.node.id, point]));
    const styles = getComputedStyle(this.canvas);
    const textColor = styles.getPropertyValue("--primary-text-color") || "#fff";
    const mutedColor =
      styles.getPropertyValue("--secondary-text-color") || "#888";

    this._drawFloors(ctx, camera, mutedColor);
    this._drawStems(ctx, camera, projected, mutedColor);

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
    }
  }

  _floors() {
    // One plane per building floor, sitting at the mean height of the radios on
    // it. The solver places radios, not storeys, so this is the best available
    // reading of where a floor is.
    const groups = new Map();
    for (const node of this.nodes) {
      if (!node.floor) continue;
      if (!groups.has(node.floor)) groups.set(node.floor, []);
      groups.get(node.floor).push(node);
    }
    return [...groups.entries()]
      .map(([name, nodes]) => ({
        name,
        color: nodes[0].color,
        z: nodes.reduce((sum, n) => sum + n.z, 0) / nodes.length,
      }))
      .sort((a, b) => a.z - b.z);
  }

  _bounds() {
    const xs = this.nodes.map((n) => n.x);
    const ys = this.nodes.map((n) => n.y);
    const padX = Math.max(1, (Math.max(...xs) - Math.min(...xs)) * 0.25);
    const padY = Math.max(1, (Math.max(...ys) - Math.min(...ys)) * 0.25);
    return {
      minX: Math.min(...xs) - padX,
      maxX: Math.max(...xs) + padX,
      minY: Math.min(...ys) - padY,
      maxY: Math.max(...ys) + padY,
    };
  }

  _drawFloors(ctx, camera, mutedColor) {
    const floors = this._floors();
    if (!floors.length) return;
    const { minX, maxX, minY, maxY } = this._bounds();

    // Lowest first, so an upper floor is painted over the one beneath it.
    for (const floor of floors) {
      const corners = [
        [minX, minY],
        [maxX, minY],
        [maxX, maxY],
        [minX, maxY],
      ].map(([x, y]) => this._projectPoint(camera, x, y, floor.z));

      ctx.beginPath();
      ctx.moveTo(corners[0].sx, corners[0].sy);
      for (const corner of corners.slice(1)) ctx.lineTo(corner.sx, corner.sy);
      ctx.closePath();
      ctx.fillStyle = withAlpha(floor.color, 0.1);
      ctx.fill();
      ctx.strokeStyle = withAlpha(floor.color, 0.45);
      ctx.lineWidth = 1;
      ctx.stroke();

      this._drawFloorGrid(ctx, camera, floor, minX, maxX, minY, maxY);

      ctx.fillStyle = withAlpha(floor.color, 0.9);
      ctx.font = "12px sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(floor.name, corners[3].sx + 6, corners[3].sy - 6);
    }
  }

  _drawFloorGrid(ctx, camera, floor, minX, maxX, minY, maxY) {
    ctx.strokeStyle = withAlpha(floor.color, 0.18);
    ctx.lineWidth = 1;
    for (let i = 1; i < GRID_DIVISIONS; i += 1) {
      const t = i / GRID_DIVISIONS;
      const x = minX + (maxX - minX) * t;
      const y = minY + (maxY - minY) * t;

      const a = this._projectPoint(camera, x, minY, floor.z);
      const b = this._projectPoint(camera, x, maxY, floor.z);
      ctx.beginPath();
      ctx.moveTo(a.sx, a.sy);
      ctx.lineTo(b.sx, b.sy);
      ctx.stroke();

      const c = this._projectPoint(camera, minX, y, floor.z);
      const d = this._projectPoint(camera, maxX, y, floor.z);
      ctx.beginPath();
      ctx.moveTo(c.sx, c.sy);
      ctx.lineTo(d.sx, d.sy);
      ctx.stroke();
    }
  }

  _drawStems(ctx, camera, projected, color) {
    // A vertical stem from each radio down to its own floor plane, so the eye
    // can tell which storey a point belongs to when the view is tilted.
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.35;
    ctx.lineWidth = 1;
    const floors = new Map(this._floors().map((f) => [f.name, f.z]));
    const lowest = Math.min(...this.nodes.map((n) => n.z));

    for (const point of projected) {
      const base = floors.has(point.node.floor)
        ? floors.get(point.node.floor)
        : lowest;
      const foot = this._projectPoint(camera, point.node.x, point.node.y, base);
      ctx.beginPath();
      ctx.moveTo(point.sx, point.sy);
      ctx.lineTo(foot.sx, foot.sy);
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

function withAlpha(color, alpha) {
  // The palette is hex, and canvas has no opacity channel of its own.
  const hex = color.replace("#", "");
  const value = parseInt(
    hex.length === 3 ? hex.split("").map((c) => c + c).join("") : hex,
    16,
  );
  const r = (value >> 16) & 255;
  const g = (value >> 8) & 255;
  const b = value & 255;
  return `rgba(${r},${g},${b},${alpha})`;
}
