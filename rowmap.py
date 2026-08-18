import sys, math, colorsys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from munged_extract import read_palette_records_data, read_munged, decompress_lz4, unpack_tiles

if len(sys.argv) < 3:
    sys.exit("usage: rowmap.py <sprbin> <munged_frame> [palette_name]\n(run with a bogus palette_name to see the available names)")
sprbin, frame = sys.argv[1:3]
pal_name = sys.argv[3] if len(sys.argv) > 3 else None
data = Path(sprbin).read_bytes()
records = list(read_palette_records_data(data))
if pal_name is None:
    pal_name = records[0]['name']
matches = [r for r in records if r['name'] == pal_name]
if not matches:
    sys.exit(f"palette '{pal_name}' not found; available: {', '.join(r['name'] for r in records)}")
rec = matches[0]
pal = rec['palette']
info, tiles, chunks = read_munged(Path(frame))
atlas = b"".join(decompress_lz4(d,s) for s,d in chunks)
w, h, img = unpack_tiles(info, tiles, atlas)
# material pixels: R=row selector, G=edge(255=transparent-ish), B=shade
stats = {}
for i in range(0, len(img), 4):
    r, g, b, _ = img[i:i+4]
    if r == 0 and g == 255:  # transparent
        continue
    if g > 200:  # mostly edge/anti-alias
        continue
    scaled = r * 4.0 / 255.0
    row = min(63, int((scaled - math.floor(scaled)) * 64.0))
    p = i // 4
    x, y = (p % w) / w, (p // w) / h
    s = stats.setdefault(row, [0, 0.0, 0.0])
    s[0] += 1; s[1] += x; s[2] += y
print(f"crop {w}x{h}, rows in use: {len(stats)}")
print(f"{'row':>3} {'count':>7} {'avgX':>5} {'avgY':>5}  col0/col1/col2 (rgb) hueSatVal(col1)")
for row in sorted(stats, key=lambda r: -stats[r][0]):
    n, sx, sy = stats[row]
    if n < 200: continue
    cols = [tuple(pal[row][c][:3]) for c in range(3)]
    r1,g1,b1 = cols[1]
    hh,ss,vv = colorsys.rgb_to_hsv(r1/255,g1/255,b1/255)
    print(f"{row:>3} {n:>7} {sx/n:>5.2f} {sy/n:>5.2f}  {cols[0]} {cols[1]} {cols[2]}  h{hh*360:>5.0f} s{ss:.2f} v{vv:.2f}")
