#!/usr/bin/env python3
"""Recolor an ALFG character palette inside a sprbin (in-memory), render preview.
Usage: recolor.py <sprbin> <munged_frame> <palette_name> <out_png> [hue_shift_deg]
Strategy: hue-rotate only saturated warm colors (outfit), keep skin/neutrals.
"""
import sys, colorsys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from munged_extract import (read_palette_records_data, palette_table,
                            read_munged, decompress_lz4, unpack_tiles,
                            make_canvas, apply_palette, write_png)

def shift_color(r, g, b, deg):
    h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
    hue_deg = h * 360
    # outfit colors: saturated warms (reds/oranges/yellows). skin ~ lower sat.
    if s < 0.45 or not (0 <= hue_deg <= 70 or hue_deg >= 340):
        return r, g, b
    h = ((hue_deg + deg) % 360) / 360
    r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
    return round(r2*255), round(g2*255), round(b2*255)

def main():
    sprbin, frame, pal_name, out_png = sys.argv[1:5]
    deg = float(sys.argv[5]) if len(sys.argv) > 5 else 175.0
    data = bytearray(Path(sprbin).read_bytes())
    recs = read_palette_records_data(bytes(data))
    matches = [r for r in recs if r['name'].lower() == pal_name.lower()]
    if not matches:
        sys.exit(f"palette {pal_name} not found")
    # dedupe identical tables, patch every occurrence of this name
    offsets = sorted({r['offset'] for r in matches})
    print(f"patching {len(offsets)} table(s) named {pal_name}")
    changed = 0
    for off in offsets:
        for row in range(64):
            for col in range(3):          # cols 0-2 = colors; col 3 = metadata
                p = off + row*16 + col*4
                r, g, b, a = data[p:p+4]
                r2, g2, b2 = shift_color(r, g, b, deg)
                if (r2, g2, b2) != (r, g, b):
                    data[p:p+3] = bytes((r2, g2, b2))
                    changed += 1
    print(f"shifted {changed} palette colors")
    patched = bytes(data)
    Path(sprbin + '.patched').write_bytes(patched)
    # render with patched palette
    pal_recs = read_palette_records_data(patched)
    rec = [r for r in pal_recs if r['name'].lower() == pal_name.lower()][0]
    pal = rec['palette']
    info, tiles, chunks = read_munged(Path(frame))
    atlas = b"".join(decompress_lz4(d, s) for s, d in chunks)
    w, h, img = unpack_tiles(info, tiles, atlas)
    img = apply_palette(img, pal, alpha_curve=True)
    w, h, img = make_canvas(info, img)
    write_png(Path(out_png), w, h, img)
    print(f"preview -> {out_png}")

if __name__ == '__main__':
    main()
