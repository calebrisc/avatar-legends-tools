#!/usr/bin/env python3
"""ALFG munged encoder v1 — writes type-6 (RGBA) munged files.
Strategy: reuse the source file's geometry (tiling, occupancy, atlas dims),
replace pixel content, emit literal-only LZ4 (valid, uncompressed) chunks.
Can convert a type-3 source to type-6 (pixel_scale_code adjusted to keep scale).
"""
import struct, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from munged_extract import read_munged, decompress_lz4, unpack_tiles

def lz4_literal(data):
    """Encode data as a valid literal-only LZ4 block."""
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

def build_atlas(info, tiles_mask, image, fill=None):
    """Rebuild the padded 34px-cell atlas from a crop-space RGBA image.
    Vacant cell space uses the type's 'empty' value: type-3 material transparent
    is (0,255,0,0); type-6 RGBA transparent is (0,0,0,0)."""
    w, h = info["crop_width"], info["crop_height"]
    aw, ah = info["atlas_width"], info["atlas_height"]
    cols = info["atlas_columns"]
    if fill is None:
        fill = b"\x00\xff\x00\x00" if info["image_type"] == 3 else b"\x00\x00\x00\x00"
    atlas = bytearray(fill * (aw * ah))
    stored = 0
    for tile in range(info["tile_count"]):
        if not tiles_mask[tile >> 3] & (1 << (tile & 7)):
            continue
        cx, cy = stored % cols, stored // cols
        tx, ty = tile % info["tile_columns"], tile // info["tile_columns"]
        cw = min(32, w - tx * 32)
        ch = min(32, h - ty * 32)
        # write 34x34 cell: 1px clamped border around the 32x32 payload
        for y in range(-1, ch + 1):
            sy = min(max(ty * 32 + y, ty * 32), ty * 32 + ch - 1)
            row = image[(sy * w + tx * 32) * 4:(sy * w + tx * 32 + cw) * 4]
            first_px = row[0:4]; last_px = row[-4:]
            cell_row = first_px + row + last_px          # clamp left/right border
            dst = ((cy * 34 + y + 1) * aw + cx * 34) * 4
            atlas[dst:dst + len(cell_row)] = cell_row
        stored += 1
    return bytes(atlas)

def encode(src_path, image, out_path, force_type6=True):
    """image: crop-space RGBA bytes (crop_width*crop_height*4) to store."""
    data = bytearray(Path(src_path).read_bytes())
    info, tiles_mask, chunks = read_munged(Path(src_path))
    atlas = build_atlas(info, tiles_mask, image)
    assert len(atlas) == info["atlas_width"] * info["atlas_height"] * 4

    # header prefix: everything up to the 15-uint geometry block
    name_end = 31 + struct.unpack_from("<I", data, 27)[0]
    header = bytearray(data[:name_end + 60])
    if force_type6 and info["image_type"] == 3:
        header[26] = 6
        # keep effective canvas scale: type3 uses (code-1), type6 uses code
        geo_off = name_end
        code_off = geo_off + 8 * 4          # pixel_scale_code is the 9th uint
        code = struct.unpack_from("<I", header, code_off)[0]
        new_code = max(0, (code if code in (0, 1, 2) else 0) - 1)
        struct.pack_into("<I", header, code_off, new_code)

    # split atlas into the same raw-size chunks as the source, literal-LZ4 each
    out = bytearray(header)
    out += data[name_end + 60:name_end + 60 + len(tiles_mask)]   # occupancy
    out += struct.pack("<I", len(chunks))
    pos = 0
    for raw_size, _ in chunks:
        raw = atlas[pos:pos + raw_size]
        pos += raw_size
        stream = lz4_literal(raw)
        out += struct.pack("<3I", len(stream) + 8, raw_size, len(stream))
        out += stream
    assert pos == len(atlas)
    Path(out_path).write_bytes(bytes(out))
    return info

if __name__ == "__main__":
    # roundtrip self-test: decode source, re-encode its own pixels, re-decode, compare
    src, dst = sys.argv[1], sys.argv[2]
    info, tiles_mask, chunks = read_munged(Path(src))
    atlas = b"".join(decompress_lz4(d, s) for s, d in chunks)
    w, h, image = unpack_tiles(info, tiles_mask, atlas)
    encode(src, image, dst, force_type6=(info["image_type"] == 3))
    info2, tiles2, chunks2 = read_munged(Path(dst))
    atlas2 = b"".join(decompress_lz4(d, s) for s, d in chunks2)
    w2, h2, image2 = unpack_tiles(info2, tiles2, atlas2)
    print("validators passed:", info2["image_type_name"])
    print("dims match:", (w, h) == (w2, h2))
    print("pixels identical:", image == image2)
