// Minimal orbiting 3D scatter renderer. Five labelled points and their links do
// not justify pulling in a WebGL library, and a 2D canvas keeps the integration
// dependency-free and inside Home Assistant's content security policy.

const NODE_RADIUS = 7;
const BEACON_RADIUS = 3.5;

// A beacon's uncertainty runs to metres, so its ring is frequently wider than
// the house. Drawing every ring at once was tried and is unreadable: thirty-odd
// translucent discs cover the canvas and hide the radios they are drawn against.
// Only the hovered beacon shows its ring, and the figure is in the table too.
const MAX_HALO_PX = 140;

// One badly-solved beacon can land far outside the house. The view is framed on
// the radios and stretched only this far to accommodate beacons, so an outlier
// drifts off the edge instead of shrinking everything else to a dot.
const MAX_BEACON_EXTENT = 1.35;
const GRID_DIVISIONS = 6;
const ANIMATION_MS = 700;

// The floor plane is drawn under the lowest radio on its storey, clear of it by
// this fraction of the scene's height so the radio never sits inside the plane.
const FLOOR_CLEARANCE = 0.06;
const MIN_FLOOR_CLEARANCE_M = 0.3;

export class AnchorScene {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.nodes = [];
    this.edges = [];
    this.beacons = [];
    this.azimuth = 0.9;
    this.elevation = 0.45;
    this.zoom = 1;
    this.hovered = null;
    this.showEdges = true;
    this.showBeacons = true;
    this._frame = null;
    this._bindPointer();
  }

  setData(nodes, edges, beacons = []) {
    this.edges = edges;
    // Beacons are placed in the same solve as the radios, so they must be spun
    // by the same rotation. Aligning them independently would slide them
    // through the house relative to the radios that positioned them.
    const transform = this._alignment(nodes);
    const aligned = nodes.map(transform);
    const alignedBeacons = beacons.map(transform);
    if (!this.nodes.length) {
      this.nodes = aligned;
      this.beacons = alignedBeacons;
      this.draw();
      return;
    }
    this._animateTo(aligned, alignedBeacons);
  }

  setShowEdges(value) {
    this.showEdges = value;
    this.draw();
  }

  setShowBeacons(value) {
    this.showBeacons = value;
    this.draw();
  }

  // Rotate a new solution onto the previous one. The solver has no preferred
  // rotation about the vertical axis, nor a preferred handedness, so
  // consecutive solves come back arbitrarily spun and the view appears to jump.
  // Applying the rotation and reflection that best match the previous positions
  // is a change of viewpoint only: it leaves every distance untouched.
  _alignment(nodes) {
    const previous = new Map(this.nodes.map((node) => [node.id, node]));
    const pairs = nodes
      .filter((node) => previous.has(node.id))
      .map((node) => [node, previous.get(node.id)]);
    if (pairs.length < 2) return (point) => point;

    let best = null;
    for (const mirror of [1, -1]) {
      // Closed-form best rotation about z for a set of point pairs.
      let cross = 0;
      let dot = 0;
      for (const [next, prior] of pairs) {
        const x = mirror * next.x;
        cross += x * prior.y - next.y * prior.x;
        dot += x * prior.x + next.y * prior.y;
      }
      const angle = Math.atan2(cross, dot);
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);

      let error = 0;
      for (const [next, prior] of pairs) {
        const x = mirror * next.x;
        error +=
          (x * cos - next.y * sin - prior.x) ** 2 +
          (x * sin + next.y * cos - prior.y) ** 2 +
          (next.z - prior.z) ** 2;
      }
      if (!best || error < best.error) best = {error, angle, mirror};
    }

    const cos = Math.cos(best.angle);
    const sin = Math.sin(best.angle);
    return (point) => {
      const x = best.mirror * point.x;
      return {...point, x: x * cos - point.y * sin, y: x * sin + point.y * cos};
    };
  }

  _animateTo(target, targetBeacons) {
    const from = new Map(
      [...this.nodes, ...this.beacons].map((node) => [
        node.id,
        {x: node.x, y: node.y, z: node.z},
      ]),
    );
    const started = performance.now();
    cancelAnimationFrame(this._frame);

    const tick = (now) => {
      const t = Math.min(1, (now - started) / ANIMATION_MS);
      const eased = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      const move = (node) => {
        const start = from.get(node.id);
        if (!start) return node;
        return {
          ...node,
          x: start.x + (node.x - start.x) * eased,
          y: start.y + (node.y - start.y) * eased,
          z: start.z + (node.z - start.z) * eased,
        };
      };
      this.nodes = target.map(move);
      this.beacons = targetBeacons.map(move);
      this.draw();
      if (t < 1) this._frame = requestAnimationFrame(tick);
    };
    this._frame = requestAnimationFrame(tick);
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
    const camera = this._camera();
    let found = null;
    for (const node of [...this._visibleBeacons(), ...this.nodes]) {
      const point = this._projectPoint(camera, node.x, node.y, node.z);
      const reach = node.beacon ? BEACON_RADIUS * 2.5 : NODE_RADIUS * 2;
      if (Math.hypot(point.sx - x, point.sy - y) <= reach) found = node.id;
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
    const reach = (points) =>
      Math.max(
        1,
        ...points.flatMap((n) => [Math.abs(n.x), Math.abs(n.y), Math.abs(n.z)]),
      );
    const radios = reach(this.nodes);
    const extent =
      Math.min(
        radios * MAX_BEACON_EXTENT,
        Math.max(radios, this._visibleBeacons().length ? reach(this.beacons) : 0),
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

  _visibleBeacons() {
    return this.showBeacons ? this.beacons : [];
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
    for (const edge of this.showEdges ? [...this.edges].sort(
      (a, b) =>
        (byId.get(a.a)?.depth ?? 0) + (byId.get(a.b)?.depth ?? 0) -
        ((byId.get(b.a)?.depth ?? 0) + (byId.get(b.b)?.depth ?? 0)),
    ) : []) {
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

    const everything = [
      ...this._visibleBeacons().map((node) => ({
        node,
        ...this._projectPoint(camera, node.x, node.y, node.z),
      })),
      ...projected,
    ].sort((a, b) => a.depth - b.depth);

    // Dots first, labels after, so a radio drawn later cannot land on top of a
    // label already written.
    const labels = [];
    for (const point of everything) {
      const isHovered = this.hovered === point.node.id;
      if (point.node.beacon) {
        this._drawBeacon(ctx, camera, point, isHovered, textColor);
        continue;
      }
      ctx.beginPath();
      ctx.arc(point.sx, point.sy, NODE_RADIUS, 0, Math.PI * 2);
      ctx.fillStyle = point.node.color;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = isHovered ? textColor : "rgba(0,0,0,.35)";
      ctx.stroke();
      labels.push({ point, isHovered });
    }
    this._drawNodeLabels(ctx, labels, textColor);
  }

  // Radio names collide as soon as a house has more than a few radios, and they
  // collide worst exactly where the map is densest. Nearest-to-camera wins its
  // preferred spot and everything behind it steps up out of the way; a label
  // with nowhere to go is dropped rather than written over its neighbour, since
  // two overlapping names are less use than one readable one. Hovering always
  // wins, so nothing is permanently unreadable.
  _drawNodeLabels(ctx, labels, textColor) {
    const LINE = 15;
    const placed = [];
    const collides = (box) =>
      placed.some(
        (other) =>
          box.left < other.right &&
          box.right > other.left &&
          box.top < other.bottom &&
          box.bottom > other.top,
      );

    ctx.textAlign = "center";
    // Nearest first: `everything` is sorted far-to-near for painting, so the
    // closest radio is last and gets first claim on its own label position.
    for (const { point, isHovered } of [...labels].reverse()) {
      ctx.font = `${isHovered ? "600 " : ""}13px sans-serif`;
      const width = ctx.measureText(point.node.label).width;
      const baseY = point.sy - NODE_RADIUS - 8;

      let y = baseY;
      let box = null;
      for (let step = 0; step < 6; step += 1) {
        const candidate = {
          left: point.sx - width / 2 - 3,
          right: point.sx + width / 2 + 3,
          top: y - 12,
          bottom: y + 3,
        };
        if (!collides(candidate)) {
          box = candidate;
          break;
        }
        y -= LINE;
      }
      if (!box) {
        if (!isHovered) continue;
        box = {
          left: point.sx - width / 2 - 3,
          right: point.sx + width / 2 + 3,
          top: baseY - 12,
          bottom: baseY + 3,
        };
        y = baseY;
      }
      placed.push(box);

      // A thin ground-coloured stroke keeps the name readable where it crosses
      // the beacon cloud, which is most places.
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(0,0,0,.55)";
      ctx.strokeText(point.node.label, point.sx, y);
      ctx.fillStyle = textColor;
      ctx.fillText(point.node.label, point.sx, y);

      // When a label has been pushed clear of its dot, tie it back.
      if (y < baseY - 2) {
        ctx.beginPath();
        ctx.moveTo(point.sx, y + 4);
        ctx.lineTo(point.sx, baseY);
        ctx.lineWidth = 1;
        ctx.strokeStyle = "rgba(255,255,255,.25)";
        ctx.stroke();
      }
    }
  }

  // A beacon is drawn as a dot inside a ring showing how far it could actually
  // be. At a real house's noise that ring is metres wide and frequently larger
  // than the dot's distance from its neighbours, which is the honest picture:
  // the dot says roughly where, the ring says do not read too much into it.
  _drawBeacon(ctx, camera, point, isHovered, textColor) {
    const node = point.node;
    const halo = Math.min(MAX_HALO_PX, (node.uncertainty || 0) * camera.scale);
    if (isHovered && halo > BEACON_RADIUS) {
      ctx.beginPath();
      ctx.arc(point.sx, point.sy, halo, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(255,167,38,.13)";
      ctx.fill();
      ctx.strokeStyle = "rgba(255,167,38,.5)";
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    ctx.beginPath();
    ctx.arc(point.sx, point.sy, BEACON_RADIUS, 0, Math.PI * 2);
    ctx.fillStyle = isHovered ? "#ffa726" : "rgba(255,167,38,.65)";
    ctx.fill();

    // 120 labels at once is noise, so a beacon names itself only on hover.
    if (!isHovered) return;
    ctx.fillStyle = textColor;
    ctx.font = "600 12px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(node.label, point.sx, point.sy - BEACON_RADIUS - 8);
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillText(
      `+/-${(node.uncertainty || 0).toFixed(1)}m`,
      point.sx,
      point.sy + BEACON_RADIUS + 14,
    );
  }

  _floors() {
    // One plane per building floor, sitting just below the lowest radio on that
    // storey. A radio stands on its floor, so the plane has to clear all of
    // them -- placing it at the mean would leave half the radios underneath.
    const groups = new Map();
    for (const node of this.nodes) {
      if (!node.floor) continue;
      if (!groups.has(node.floor)) groups.set(node.floor, []);
      groups.get(node.floor).push(node);
    }
    if (!groups.size) return [];

    const zs = this.nodes.map((node) => node.z);
    const clearance = Math.max(
      MIN_FLOOR_CLEARANCE_M,
      (Math.max(...zs) - Math.min(...zs)) * FLOOR_CLEARANCE,
    );

    return [...groups.entries()]
      .map(([name, nodes]) => ({
        name,
        color: nodes[0].color,
        z: Math.min(...nodes.map((node) => node.z)) - clearance,
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
