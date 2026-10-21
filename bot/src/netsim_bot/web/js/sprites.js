// Pixel art for the NOC room, drawn with rectangles (no image files): the palette, the robots and the furniture.

export const P = {
  ink: '#090c14', wallTop: '#121726', wall: '#1f2638', wallPanel: '#242c42', wallSeam: '#1a2032', base: '#0f131d',
  floorA: '#283047', floorB: '#252c42', floorSeam: '#20273a', rug: '#2c3758', rugEdge: '#3b4a74', rugMark: '#44558a',
  desk: '#56627e', deskLight: '#6d7b9b', deskDark: '#3a4259', deskEdge: '#2c3245', wood: '#7b5236', woodDark: '#5c3c28',
  woodLight: '#9a6b47', metal: '#8f99ad', metalDark: '#566075', metalLight: '#c3cbda', plastic: '#d9dde6',
  screen: '#0a1322', screenEdge: '#2d3954', glow: '#38b6ff', glowSoft: '#1d5f8f', cyan: '#5cf2dc', paper: '#efece2',
  paperLine: '#b9b4a6', cork: '#8a6844', corkDark: '#6c4f31', corkDot: '#a07c55', leaf: '#3fa45b', leafDark: '#2a7a42',
  pot: '#b0603c', shadow: 'rgba(5,8,14,0.45)', white: '#f4f6fb', text: '#dfe5f2', dim: '#8792ab', red: '#ff4d5e',
  amber: '#ffb648', green: '#39d98a', blue: '#4aa3ff', yellow: '#ffd84d', purple: '#b18cff',
};

// Zabbix's severity order, with our colors.
export const SEV = {
  'Not classified': '#97a3b6', Information: '#5aa7ff', Warning: '#ffd84d', Average: '#ffa64d',
  High: '#ff6b5e', Disaster: '#e8283c', OK: '#39d98a',
};
export const SEV_RANK = { OK: 0, 'Not classified': 1, Information: 2, Warning: 3, Average: 4, High: 5, Disaster: 6 };
export const TIER = { gold: '#f5c542', silver: '#c9d2dc', bronze: '#d08a4a' };

export function statusColor(s) {
  return SEV[s] || (s ? '#97a3b6' : '#4b556b');
}

export function shade(hex, amt) {
  const n = parseInt(hex.slice(1), 16);
  const f = (v) => Math.max(0, Math.min(255, Math.round(v + amt * 255)));
  const r = f((n >> 16) & 255), g = f((n >> 8) & 255), b = f(n & 255);
  return '#' + ((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1);
}

export function rect(ctx, x, y, w, h, c) {
  ctx.fillStyle = c;
  ctx.fillRect(Math.round(x), Math.round(y), w, h);
}

// ------------------------------------------------------------------------------------------------ robots
// (x, y) is the point between the feet. 12 x 17 px. dir: down (front), up (back), left, right.
// pose: stand, walk, type, sit, carry, cheer. eyes: normal, blink, happy, alert, sleep, look.
export function drawBot(ctx, x, y, o) {
  const color = o.color || '#4fc3f7';
  const dark = shade(color, -0.22);
  const shell = o.shell || '#e7ebf2';
  const shellDark = '#b7bfcd';
  const frame = o.frame || 0;
  const pose = o.pose || 'stand';
  const dir = o.dir || 'down';
  const bob = pose === 'walk' ? (frame % 2) : pose === 'cheer' ? -(frame % 2) * 2 : 0;
  const L = Math.round(x - 6);
  const T = Math.round(y - 17 + bob);
  const R = (dx, dy, w, h, c) => rect(ctx, L + dx, T + dy, w, h, c);
  const seated = pose === 'sit' || pose === 'type';

  // shadow
  if (!seated) {
    ctx.fillStyle = P.shadow;
    ctx.beginPath();
    ctx.ellipse(Math.round(x), Math.round(y) + 0.5, 5.5, 1.8, 0, 0, Math.PI * 2);
    ctx.fill();
  }
  const side = dir === 'left' || dir === 'right';
  const flip = dir === 'left';
  const X = (dx, w = 1) => (flip ? 12 - dx - w : dx);

  // antenna / hats
  if (o.hat === 'hardhat') {
    R(2, 1, 8, 2, '#ffcf3a');
    R(1, 3, 10, 1, '#e0a91f');
    R(5, 0, 2, 1, '#ffe48a');
  } else {
    R(side ? X(6, 2) : 5, 0, 2, 1, o.blink ? '#ffffff' : color);
    R(side ? X(6, 2) : 5, 1, 2, 1, P.metalDark);
  }
  // head (rounded: the corner pixels are left out)
  if (side) {
    R(3, 2, 6, 1, shell);
    R(2, 3, 8, 6, shell);
    R(3, 9, 6, 1, shellDark);
    R(X(5, 4), 3, 4, 5, '#121d2c');
    eyesSide(R, X, o.eyes, color);
    R(X(2), 5, 1, 2, color);
  } else {
    R(2, 2, 8, 1, shell);
    R(1, 3, 10, 6, shell);
    R(2, 9, 8, 1, shellDark);
    R(0, 5, 1, 2, color);
    R(11, 5, 1, 2, color);
    if (dir === 'down') {
      R(2, 3, 8, 5, '#121d2c');
      eyesFront(R, o.eyes, color);
    } else {
      R(4, 4, 4, 3, shellDark);
      R(5, 5, 2, 1, color);
    }
  }
  if (o.hat === 'visor') {
    // the outside agent: a dark visor across the head with a light slit
    R(1, 3, 10, 1, '#0b0d12');
    R(side ? X(4, 6) : 2, 4, side ? 6 : 8, 1, '#0b0d12');
    if (dir !== 'up') R(side ? X(5, 3) : 4, 4, side ? 3 : 4, 1, o.blink ? '#ffffff' : '#9fb3d9');
  }
  if (o.hat === 'headset' && !side) {
    R(1, 1, 10, 1, '#2a3142');
    R(0, 2, 1, 3, '#2a3142');
    R(11, 2, 1, 3, '#2a3142');
    if (dir === 'down') {
      R(10, 7, 2, 1, '#2a3142');
      R(9, 7, 1, 1, color);
    }
  }
  // neck + body
  R(side ? 5 : 5, 10, 2, 1, P.metalDark);
  if (side) {
    R(3, 11, 6, 4, shell);
    R(X(5, 3), 12, 3, 2, color);
  } else {
    R(2, 11, 8, 4, shell);
    if (dir === 'down') {
      R(4, 12, 4, 2, color);
      R(5, 12, 2, 1, o.light ? '#ffffff' : shade(color, 0.25));
    } else {
      R(3, 12, 6, 1, color);
    }
  }
  // arms
  const swing = pose === 'walk' ? (frame % 2 ? 1 : -1) : 0;
  if (pose === 'cheer') {
    R(0, 8, 2, 3, dark);
    R(10, 8, 2, 3, dark);
  } else if (pose === 'carry') {
    R(1, 11, 2, 2, dark);
    R(9, 11, 2, 2, dark);
    R(3, 10, 6, 5, P.paper);
    R(4, 11, 4, 1, P.paperLine);
    R(4, 13, 3, 1, P.paperLine);
  } else if (pose === 'type') {
    const k = frame % 2;
    R(1, 12 + k, 2, 2, dark);
    R(9, 13 - k, 2, 2, dark);
  } else if (side) {
    R(X(5, 2), 11 + Math.max(0, swing), 2, 3, dark);
  } else {
    R(0, 11 + Math.max(0, swing), 2, 3, dark);
    R(10, 11 + Math.max(0, -swing), 2, 3, dark);
  }
  // legs
  if (!seated) {
    const lift = pose === 'walk' ? frame % 2 : -1;
    if (side) {
      R(4, 15, 2, lift === 0 ? 1 : 2, '#343b4c');
      R(6, 15, 2, lift === 1 ? 1 : 2, '#2a303f');
    } else {
      R(3, 15, 2, lift === 0 ? 1 : 2, '#343b4c');
      R(7, 15, 2, lift === 1 ? 1 : 2, '#343b4c');
    }
  }
}

function eyesFront(R, eyes, color) {
  const c = eyes === 'alert' ? '#ffd35c' : eyes === 'sleep' ? '#3d6d7a' : '#7ff6ff';
  switch (eyes) {
    case 'blink':
    case 'sleep':
      R(3, 5, 2, 1, c);
      R(7, 5, 2, 1, c);
      break;
    case 'happy':
      R(2, 5, 1, 1, c); R(3, 4, 1, 1, c); R(4, 5, 1, 1, c);
      R(7, 5, 1, 1, c); R(8, 4, 1, 1, c); R(9, 5, 1, 1, c);
      break;
    case 'alert':
      R(3, 3, 2, 3, c);
      R(7, 3, 2, 3, c);
      break;
    case 'look':
      R(3, 3, 2, 2, c);
      R(7, 3, 2, 2, c);
      break;
    default:
      R(3, 4, 2, 2, c);
      R(7, 4, 2, 2, c);
  }
  void color;
}

function eyesSide(R, X, eyes) {
  const c = eyes === 'alert' ? '#ffd35c' : '#7ff6ff';
  if (eyes === 'blink' || eyes === 'sleep') R(X(7, 2), 5, 2, 1, c);
  else R(X(7, 2), 4, 2, 2, c);
}

// A small icon above a bot: ! ? … z ★ ✓ ×
export function drawEmote(ctx, x, y, icon, t) {
  const colors = { '!': P.red, '?': P.amber, '…': P.dim, z: P.blue, '★': P.yellow, '✓': P.green, '×': P.red, '♥': '#ff6b9a' };
  const bob = Math.round(Math.sin(t * 6) * 1);
  const cx = Math.round(x), cy = Math.round(y - 25 + bob);
  rect(ctx, cx - 4, cy - 4, 9, 9, P.white);
  rect(ctx, cx - 5, cy - 3, 1, 7, P.white);
  rect(ctx, cx + 5, cy - 3, 1, 7, P.white);
  rect(ctx, cx - 1, cy + 5, 3, 1, P.white);
  rect(ctx, cx, cy + 6, 1, 1, P.white);
  return colors[icon] || P.ink;
}

// ------------------------------------------------------------------------------------------------ furniture
export function drawDesk(ctx, x, y, w, o = {}) {
  rect(ctx, x, y, w, 3, P.deskLight);
  rect(ctx, x, y + 3, w, 7, P.desk);
  rect(ctx, x, y + 10, w, 2, P.deskDark);
  rect(ctx, x + 2, y + 12, 2, 6, P.deskEdge);
  rect(ctx, x + w - 4, y + 12, 2, 6, P.deskEdge);
  rect(ctx, x, y + 3, w, 1, shade(P.desk, 0.06));
  if (o.drawer) {
    rect(ctx, x + w - 16, y + 5, 12, 4, P.deskDark);
    rect(ctx, x + w - 11, y + 6, 3, 1, P.metalLight);
  }
}

export function drawMonitor(ctx, x, y, w, h, t, o = {}) {
  // a monitor whose screen faces us; o.mode: code, graph, alert, off, map
  rect(ctx, x, y, w, h, '#1a2133');
  rect(ctx, x + 1, y + 1, w - 2, h - 2, o.mode === 'off' ? '#0a0f18' : o.mode === 'alert' ? '#2a0b12' : P.screen);
  rect(ctx, x + Math.floor(w / 2) - 1, y + h, 3, 2, P.metalDark);
  rect(ctx, x + Math.floor(w / 2) - 3, y + h + 2, 7, 1, P.metalDark);
  if (o.mode === 'off') return;
  const iw = w - 4, ih = h - 4;
  const c = o.color || P.glow;
  if (o.mode === 'graph') {
    for (let i = 0; i < iw; i++) {
      const v = Math.round((Math.sin((i + t * 8) * 0.6) * 0.5 + 0.5) * (ih - 1));
      rect(ctx, x + 2 + i, y + 2 + ih - 1 - v, 1, 1, c);
    }
  } else if (o.mode === 'alert') {
    if (Math.floor(t * 3) % 2 === 0) rect(ctx, x + 2, y + 2, iw, ih, '#5c1420');
    rect(ctx, x + Math.floor(w / 2) - 1, y + 3, 2, ih - 4, P.red);
  } else {
    const rows = Math.floor(ih / 2);
    for (let r = 0; r < rows; r++) {
      const seed = (r * 7 + Math.floor(t * (o.typing ? 6 : 1.2))) % 11;
      const len = 2 + ((seed * 5) % (iw - 2));
      rect(ctx, x + 2 + (r % 3 === 0 ? 0 : 2), y + 2 + r * 2, Math.min(len, iw - 2), 1, r % 4 === 0 ? P.cyan : c);
    }
  }
}

export function drawChair(ctx, x, y, color = '#384157', facing = 'up') {
  // (x, y): seat center
  if (facing === 'up') {
    rect(ctx, x - 5, y - 3, 10, 4, shade(color, 0.08));
    rect(ctx, x - 4, y + 1, 8, 2, color);
  } else {
    rect(ctx, x - 5, y - 8, 10, 6, shade(color, 0.08));
    rect(ctx, x - 5, y - 2, 10, 3, color);
  }
  rect(ctx, x - 1, y + 3, 2, 2, P.metalDark);
  rect(ctx, x - 4, y + 5, 8, 1, P.metalDark);
}

export function drawPlant(ctx, x, y, t) {
  const sway = Math.round(Math.sin(t * 1.3 + x) * 0.6);
  rect(ctx, x - 4, y - 7, 8, 7, P.pot);
  rect(ctx, x - 5, y - 8, 10, 2, shade(P.pot, 0.1));
  rect(ctx, x - 1 + sway, y - 20, 2, 12, P.leafDark);
  rect(ctx, x - 6 + sway, y - 16, 5, 3, P.leaf);
  rect(ctx, x + 1 + sway, y - 19, 5, 3, P.leaf);
  rect(ctx, x - 4 + sway, y - 23, 4, 4, P.leaf);
  rect(ctx, x + 2 + sway, y - 13, 4, 3, P.leafDark);
}
