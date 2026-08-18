#!/usr/bin/env python3
"""ALFG type-3 encoder v1 — turns RGBA art into native material format + palette rows.
v1 scheme: flat colors (no shading ramps yet):
  - quantized art -> unique colors -> one palette row each (cols 0-2 identical)
  - material pixel: R = row index (band 0), G = 255 (edge 0), B = 204 (shade mid)
  - transparent: (0, 255, 0, 0)
Synthesizes full frame geometry (any image size) per the format validators.
"""
import struct, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_ROWS = 62          # rows 1..62 usable (row 0 + G=255 collides with transparency)

def build_palette_and_material(rgba, w, h, alpha_cut=128):
    colors = {}
    material = bytearray(w * h * 4)
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i:i+4]
        if a < alpha_cut:
            material[i:i+4] = b"\x00\xff\x00\x00"
            continue
        key = (r, g, b)
        row = colors.get(key)
        if row is None:
            if len(colors) >= MAX_ROWS:
                raise ValueError(f"more than {MAX_ROWS} colors; quantize harder")
            row = len(colors) + 1                     # rows start at 1
            colors[key] = row
        material[i] = row          # R: band 0, row selector
        material[i+1] = 255        # G: no edge
        material[i+2] = 204        # B: shade -> middle column
        material[i+3] = 255
    # palette table: 64 rows x 4 cols x RGBA
    table = bytearray(1024)
    for (r, g, b), row in colors.items():
        for col in range(3):
            p = row*16 + col*4
            table[p:p+4] = bytes((r, g, b, 255))
        table[row*16+12:row*16+16] = bytes((0, 0, 0, 255))   # metadata col
    return bytes(material), bytes(table), len(colors)

def palette_list(table):
    return [
        [tuple(table[i*16 + c*4:i*16 + c*4 + 4]) for c in range(4)]
        for i in range(64)
    ]

def lz4_literal(data):
    out = bytearray()
    n = len(data)
    if n < 15:
        out.append(n << 4)
    else:
        out.append(0xF0)
        rest = n - 15
        while rest >= 255:
            out.append(255); rest -= 255
        out.append(rest)
    out.extend(data)
    return bytes(out)

def synth_type3(material, w, h, source_name="!src/sprites/custom/custom.png",
                template_prefix=None):
    """Build a complete type-3 munged file for a w*h material image."""
    tc = (w + 31) // 32                # tile columns
    tr = (h + 31) // 32                # tile rows
    tile_count = tc * tr
    atlas_cols, atlas_rows = tc, tr    # store every tile
    aw, ah = atlas_cols * 34, atlas_rows * 34
    atlas = bytearray(b"\x00\xff\x00\x00" * (aw * ah))
    for tile in range(tile_count):
        tx, ty = tile % tc, tile // tc
        cw = min(32, w - tx*32)
        ch = min(32, h - ty*32)
        cx, cy = tile % atlas_cols, tile // atlas_cols
        for y in range(-1, ch+1):
            sy = min(max(ty*32 + y, ty*32), ty*32 + ch - 1)
            row = material[(sy*w + tx*32)*4:(sy*w + tx*32 + cw)*4]
            cell_row = row[0:4] + row + row[-4:]
            dst = ((cy*34 + y + 1) * aw + cx*34) * 4
            atlas[dst:dst+len(cell_row)] = cell_row
    occupancy = bytearray((tile_count + 7)//8)
    for t in range(tile_count):
        occupancy[t >> 3] |= 1 << (t & 7)
    # header
    if template_prefix:
        # reuse flags/asset key from a real file's first 27 bytes
        head27 = bytearray(template_prefix[:27])
    else:
        head27 = bytearray(struct.pack("<I", 24) + bytes(10) + b"00000000" + bytes(4) + b"\x03")
    head27[26:27] = b"\x03"
    src = b"!" + source_name.lstrip("!").encode()
    header = bytes(head27) + struct.pack("<I", len(src)) + src
    geo = struct.pack("<15I",
        0, 0, w, h,          # crop x,y,w,h
        w, h,                # canvas w,h
        aw, ah,              # atlas w,h
        1,                   # pixel_scale_code (type3: exponent 1-1=0 -> 1x)
        tc, tr,              # tile cols/rows
        atlas_cols, atlas_rows,
        tile_count,          # stored tiles (all)
        tile_count)          # tile count
    pad = bytes(60 - len(geo))
    stream = lz4_literal(bytes(atlas))
    out = bytearray(header + geo + pad)
    out += occupancy
    out += struct.pack("<I", 1)
    out += struct.pack("<3I", len(stream)+8, len(atlas), len(stream))
    out += stream
    return bytes(out)

if __name__ == "__main__":
    from munged_extract import read_munged, decompress_lz4, unpack_tiles, apply_palette, write_png
    raw_path, w, h, out_munged, out_png = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
    rgba = Path(raw_path).read_bytes()
    material, table, ncolors = build_palette_and_material(rgba, w, h)
    print(f"palette rows used: {ncolors}")
    tmpl = None
    tpath = Path('aang/srcdata/munged/frames/sprites~aang~idle~stand~frames~stand_idle01.munged')
    if tpath.exists():
        tmpl = tpath.read_bytes()
    blob = synth_type3(material, w, h, template_prefix=tmpl)
    Path(out_munged).write_bytes(blob)
    # graduation exam: decode with the REFERENCE decoder + our palette
    info, tiles, chunks = read_munged(Path(out_munged))
    atlas = b"".join(decompress_lz4(d, s) for s, d in chunks)
    ww, hh, img = unpack_tiles(info, tiles, atlas)
    final = apply_palette(img, palette_list(table), alpha_curve=False)
    write_png(Path(out_png), ww, hh, final)
    print(f"decoded {ww}x{hh} -> {out_png} | validators passed: {info['image_type_name']}")
    Path(out_munged + '.palette').write_bytes(table)
