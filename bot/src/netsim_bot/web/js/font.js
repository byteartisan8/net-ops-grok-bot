// A 3x5 pixel font (drawn for this project) for everything written inside the NOC room canvas.
// Lowercase is drawn as uppercase; unknown characters render as a small box.

const RAW = {
  A: '.#.|#.#|###|#.#|#.#', B: '##.|#.#|##.|#.#|##.', C: '.##|#..|#..|#..|.##', D: '##.|#.#|#.#|#.#|##.',
  E: '###|#..|##.|#..|###', F: '###|#..|##.|#..|#..', G: '.##|#..|#.#|#.#|.##', H: '#.#|#.#|###|#.#|#.#',
  I: '###|.#.|.#.|.#.|###', J: '..#|..#|..#|#.#|.#.', K: '#.#|#.#|##.|#.#|#.#', L: '#..|#..|#..|#..|###',
  M: '#.#|###|###|#.#|#.#', N: '##.|#.#|#.#|#.#|#.#', O: '.#.|#.#|#.#|#.#|.#.', P: '##.|#.#|##.|#..|#..',
  Q: '.#.|#.#|#.#|##.|.##', R: '##.|#.#|##.|#.#|#.#', S: '.##|#..|.#.|..#|##.', T: '###|.#.|.#.|.#.|.#.',
  U: '#.#|#.#|#.#|#.#|###', V: '#.#|#.#|#.#|#.#|.#.', W: '#.#|#.#|###|###|#.#', X: '#.#|#.#|.#.|#.#|#.#',
  Y: '#.#|#.#|.#.|.#.|.#.', Z: '###|..#|.#.|#..|###',
  0: '###|#.#|#.#|#.#|###', 1: '.#.|##.|.#.|.#.|###', 2: '##.|..#|.#.|#..|###', 3: '##.|..#|.#.|..#|##.',
  4: '#.#|#.#|###|..#|..#', 5: '###|#..|##.|..#|##.', 6: '.##|#..|###|#.#|###', 7: '###|..#|.#.|.#.|.#.',
  8: '###|#.#|###|#.#|###', 9: '###|#.#|###|..#|##.',
  ' ': '...|...|...|...|...', '.': '...|...|...|...|.#.', ',': '...|...|...|.#.|#..', ':': '...|.#.|...|.#.|...',
  ';': '...|.#.|...|.#.|#..', '!': '.#.|.#.|.#.|...|.#.', '?': '##.|..#|.#.|...|.#.', '-': '...|...|###|...|...',
  '+': '...|.#.|###|.#.|...', '/': '..#|..#|.#.|#..|#..', '\\': '#..|#..|.#.|..#|..#', '(': '.#.|#..|#..|#..|.#.',
  ')': '.#.|..#|..#|..#|.#.', '[': '##.|#..|#..|#..|##.', ']': '.##|..#|..#|..#|.##', '%': '#.#|..#|.#.|#..|#.#',
  '#': '#.#|###|#.#|###|#.#', "'": '.#.|.#.|...|...|...', '"': '#.#|#.#|...|...|...', '<': '..#|.#.|#..|.#.|..#',
  '>': '#..|.#.|..#|.#.|#..', '=': '...|###|...|###|...', _: '...|...|...|...|###', '*': '...|#.#|.#.|#.#|...',
  '&': '.#.|#.#|.#.|#.#|.##', '@': '###|#.#|###|#..|.##', $: '.##|##.|.#.|.##|##.', '|': '.#.|.#.|.#.|.#.|.#.',
  '^': '.#.|#.#|...|...|...', '~': '...|...|.##|##.|...', '`': '#..|.#.|...|...|...', '{': '.##|.#.|##.|.#.|.##',
  '}': '##.|.#.|.##|.#.|##.', '•': '...|.#.|###|.#.|...', '✓': '...|..#|#.#|.#.|...', '×': '...|#.#|.#.|#.#|...',
  '★': '.#.|###|.#.|#.#|...', '♥': '#.#|###|###|.#.|...', '↑': '.#.|###|.#.|.#.|.#.', '↓': '.#.|.#.|.#.|###|.#.',
  '…': '...|...|...|...|#.#', '·': '...|...|.#.|...|...', '°': '###|#.#|###|...|...',
};
const ALIAS = { '→': '>', '←': '<', '—': '-', '–': '-', '✗': '×', '’': "'", '‘': "'", '“': '"', '”': '"', 'é': 'E' };
const BOX = '###|#.#|#.#|#.#|###';

export const ADV = 4; // advance per glyph (3 px + 1 px gap)
export const LINE = 7; // line height
export const GLYPH_H = 5;

const glyphs = new Map();
function glyph(ch) {
  let key = ch.toUpperCase();
  if (ALIAS[ch]) key = ALIAS[ch];
  if (!glyphs.has(key)) {
    const rows = (RAW[key] || BOX).split('|');
    const px = [];
    rows.forEach((row, y) => [...row].forEach((c, x) => { if (c === '#') px.push([x, y]); }));
    glyphs.set(key, px);
  }
  return glyphs.get(key);
}

const cache = new Map();
function rendered(text, color) {
  const key = color + '\u0000' + text;
  let c = cache.get(key);
  if (c) return c;
  if (cache.size > 600) cache.clear();
  const chars = [...text];
  c = document.createElement('canvas');
  c.width = Math.max(1, chars.length * ADV);
  c.height = GLYPH_H;
  const g = c.getContext('2d');
  g.fillStyle = color;
  chars.forEach((ch, i) => { for (const [x, y] of glyph(ch)) g.fillRect(i * ADV + x, y, 1, 1); });
  cache.set(key, c);
  return c;
}

export function textWidth(text) {
  const n = [...String(text)].length;
  return n ? n * ADV - 1 : 0;
}

// Draw `text` with its top-left at (x, y). align: 'left' | 'center' | 'right'.
export function drawText(ctx, text, x, y, color = '#e8ecf2', align = 'left') {
  const s = String(text);
  if (!s) return;
  const w = textWidth(s);
  const dx = align === 'center' ? Math.round(x - w / 2) : align === 'right' ? Math.round(x - w) : Math.round(x);
  ctx.drawImage(rendered(s, color), dx, Math.round(y));
}

// Word-wrap into lines of at most `max` characters (long words are cut).
export function wrap(text, max, maxLines = 3) {
  const words = String(text).replace(/\s+/g, ' ').trim().split(' ');
  const lines = [];
  let cur = '';
  for (let w of words) {
    while (w.length > max) {
      if (cur) { lines.push(cur); cur = ''; }
      lines.push(w.slice(0, max));
      w = w.slice(max);
    }
    if (!cur) cur = w;
    else if (cur.length + 1 + w.length <= max) cur += ' ' + w;
    else { lines.push(cur); cur = w; }
  }
  if (cur) lines.push(cur);
  if (lines.length > maxLines) {
    const kept = lines.slice(0, maxLines);
    kept[maxLines - 1] = kept[maxLines - 1].slice(0, Math.max(0, max - 1)) + '…';
    return kept;
  }
  return lines;
}

export function truncate(text, max) {
  const s = String(text);
  return s.length > max ? s.slice(0, Math.max(0, max - 1)) + '…' : s;
}
