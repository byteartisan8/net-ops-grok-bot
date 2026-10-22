// The roles' words over the NOC room: what each robot says (a speech bubble) and what it is doing (a caption: the
// tool it calls, then the tool's result), as HTML text on a layer over the canvas. The room's 3x5 pixel font suits its
// signs and labels; sentences need a real font at a readable size. room.js decides what each robot says and where it
// stands; this puts the text above its head, positioned in room pixels (--px: one room pixel in CSS pixels).

const LIFT = 22; // a callout's bottom above the robot's feet (its tail reaches down to just above the antenna)
const LIFT_EMOTE = 32; // ... and above the emote, while one shows
const GAP = 2; // room pixels kept between two callouts and to the room's edges

export class Speech {
  constructor(layer, canvas, w, h) {
    this.layer = layer;
    this.canvas = canvas;
    this.w = w;
    this.h = h;
    this.px = 0;
    this.callouts = new Map();
    new ResizeObserver(() => this.resize()).observe(canvas);
  }

  resize() {
    const px = this.canvas.clientWidth / this.w;
    if (!px || px === this.px) return;
    this.px = px;
    this.layer.style.setProperty('--px', `${px}px`);
    for (const c of this.callouts.values()) {
      c.size = null;
      c.at = '';
    }
  }

  callout(bot) {
    let c = this.callouts.get(bot.name);
    if (c) return c;
    const root = document.createElement('div');
    root.className = 'callout';
    root.style.setProperty('--c', bot.color);
    root.innerHTML = '<div class="bubble"></div><div class="doing"></div><i class="tail"></i>';
    this.layer.append(root);
    c = { root, bubble: root.children[0], doing: root.children[1], tail: root.children[2], key: null, size: null, at: '' };
    this.callouts.set(bot.name, c);
    return c;
  }

  // Once per frame, after the room is drawn: the DOM is only touched when a text or a position changes.
  render(bots) {
    if (!this.px) this.resize();
    if (!this.px) return;
    const px = this.px;
    const boxes = [];
    for (const bot of bots) {
      const c = this.callout(bot);
      const b = bot.bubble, d = bot.caption;
      const key = `${b?.kind}\u0000${b?.text}\u0000${d?.kind}\u0000${d?.text}`;
      if (key !== c.key) {
        c.key = key;
        show(c.bubble, b);
        show(c.doing, d);
        setData(c.root, 'kind', b?.kind);
        setData(c.root, 'doing', d?.kind);
        c.root.style.display = b || d ? '' : 'none';
        c.size = null;
      }
      if (!b && !d) continue;
      if (!c.size) {
        // the pixel border is a box-shadow one room pixel outside the boxes: +2 on each axis
        const low = d ? c.doing : c.bubble;
        c.size = { w: c.root.offsetWidth / px + 2, h: c.root.offsetHeight / px + 2, low: low.offsetWidth / px };
      }
      const { w, h } = c.size;
      const lift = bot.emote ? LIFT_EMOTE : LIFT;
      const x = Math.max(GAP, Math.min(this.w - w - GAP, Math.round(bot.x - w / 2)));
      boxes.push({ bot, c, x, y: Math.round(bot.y) - lift - h, w, h, lift });
    }
    // callouts that would overlap are stacked upwards; the lowest keeps its place
    boxes.sort((a, b) => b.y - a.y);
    const placed = [];
    for (const box of boxes) {
      for (let guard = 0; guard < 8; guard++) {
        const hit = placed.find((o) => box.x < o.x + o.w + GAP && o.x < box.x + box.w + GAP && box.y < o.y + o.h + GAP && o.y < box.y + box.h + GAP);
        if (!hit) break;
        box.y = hit.y - box.h - GAP;
      }
      box.y = Math.max(GAP, box.y);
      placed.push(box);
    }
    for (const { bot, c, x, y, w, h, lift } of placed) {
      // the tail hangs from the lowest box (centered in the callout) down to the robot; dotted when stacked up
      const left = (w - 2 - c.size.low) / 2;
      const tx = Math.max(left + 1, Math.min(left + c.size.low - 2, Math.round(bot.x) - x - 1));
      const tl = Math.max(2, Math.round(bot.y) - lift + 2 - y - h);
      const at = `${x},${y},${tx},${tl}`;
      if (at === c.at) continue;
      c.at = at;
      c.root.style.transform = `translate(${Math.round((x + 1) * px)}px, ${Math.round((y + 1) * px)}px)`;
      c.tail.style.left = `${Math.round(tx * px)}px`;
      c.tail.style.height = `${Math.round(tl * px)}px`;
      c.tail.classList.toggle('far', tl > 2);
    }
  }
}

// ids such as INC-0005 or spoke-1<->hub-b are not split over two lines: a hyphen inside a word becomes a
// non-breaking one
function show(el, line) {
  el.style.display = line ? '' : 'none';
  if (line) el.textContent = String(line.text).replace(/(\S)-(?=\S)/g, '$1‑');
}

function setData(el, name, value) {
  if (value) el.dataset[name] = value;
  else delete el.dataset[name];
}
