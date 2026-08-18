import argparse
import binascii
import json
import math
import re
import struct
import sys
import zlib
from pathlib import Path, PurePosixPath

IMAGE_TYPES = {
    0: "UNINITIALIZED",
    1: "BASIC_PALETTIZED",
    2: "BLOCK_PALETTIZED",
    3: "RLE_AND_BLOCK_PALETTIZED",
    4: "BASIC_TRUECOLOR",
    5: "BLOCK_TRUECOLOR",
    6: "RLE_AND_BLOCK_TRUECOLOR",
}

def read_munged(path):
    data = path.read_bytes()
    if len(data) < 91 or struct.unpack_from("<I", data)[0] != 24:
        raise ValueError("not an Abare munged image")

    flags = list(data[4:14])
    if any(flag > 1 for flag in flags):
        raise ValueError("invalid Boolean header flags")
    try:
        asset_key = data[14:22].decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("invalid asset key") from error
    if not re.fullmatch(r"[0-9A-Fa-f]{8}", asset_key):
        raise ValueError("invalid asset key")
    image_type = data[26]
    if image_type not in (3, 6):
        name = IMAGE_TYPES.get(image_type, "unknown")
        raise ValueError(f"unsupported image layout {image_type} ({name})")

    path_size = struct.unpack_from("<I", data, 27)[0]
    if path_size < 1:
        raise ValueError("invalid source record length")
    name_start = 31
    name_end = name_start + path_size
    if name_end + 60 > len(data):
        raise ValueError("truncated header")
    if data[name_start] != 0x21:
        raise ValueError("invalid source marker")
    try:
        source = data[name_start + 1:name_end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("invalid source path") from error
    if "\0" in source:
        raise ValueError("invalid source path")

    values = struct.unpack_from("<15I", data, name_end)
    keys = (
        "crop_x", "crop_y", "crop_width", "crop_height",
        "canvas_width", "canvas_height", "atlas_width", "atlas_height",
        "pixel_scale_code", "tile_columns", "tile_rows", "atlas_columns",
        "atlas_rows", "stored_tiles", "tile_count"
    )
    info = dict(zip(keys, values))
    expected_columns = (info["crop_width"] + 31) // 32
    expected_rows = (info["crop_height"] + 31) // 32
    if info["tile_columns"] != expected_columns or info["tile_rows"] != expected_rows:
        raise ValueError("logical tile dimensions do not match the crop")
    if info["tile_count"] != info["tile_columns"] * info["tile_rows"]:
        raise ValueError("invalid logical tile count")
    if info["atlas_height"] != info["atlas_rows"] * 34:
        raise ValueError("invalid atlas height")
    empty_atlas = (
        info["stored_tiles"] == 0 and info["atlas_columns"] == 0
        and info["atlas_width"] == 1
    )
    if not empty_atlas and info["atlas_width"] != info["atlas_columns"] * 34:
        raise ValueError("invalid atlas width")
    if info["stored_tiles"] > info["atlas_columns"] * info["atlas_rows"]:
        raise ValueError("stored tiles exceed atlas capacity")

    tile_bytes = (values[14] + 7) // 8
    tile_start = name_end + 60
    stream_header = tile_start + tile_bytes
    if stream_header + 4 > len(data):
        raise ValueError("truncated stream header")
    tiles = data[tile_start:stream_header]
    if info["tile_count"] % 8 and tiles:
        used = (1 << (info["tile_count"] % 8)) - 1
        if tiles[-1] & ~used:
            raise ValueError("nonzero occupancy padding bits")
    if sum(byte.bit_count() for byte in tiles) != info["stored_tiles"]:
        raise ValueError("occupancy mask does not match stored tile count")

    chunk_count = struct.unpack_from("<I", data, stream_header)[0]
    if chunk_count == 0:
        raise ValueError("image has no pixel chunks")
    position = stream_header + 4
    chunks = []
    chunk_info = []
    for _ in range(chunk_count):
        if position + 12 > len(data):
            raise ValueError("truncated chunk header")
        section_size, raw_size, stream_size = struct.unpack_from("<3I", data, position)
        position += 12
        end = position + stream_size
        if end > len(data):
            raise ValueError("truncated pixel stream")
        if section_size != stream_size + 8:
            raise ValueError("invalid stream section size")
        chunks.append((raw_size, data[position:end]))
        chunk_info.append({"raw_size": raw_size, "stream_size": stream_size})
        position = end

    info.update(
        source=source,
        flags=flags,
        has_mask=bool(flags[4]),
        asset_key=asset_key,
        reserved=struct.unpack_from("<I", data, 22)[0],
        image_type=image_type,
        image_type_name=IMAGE_TYPES[image_type],
        chunk_count=chunk_count,
        chunks=chunk_info,
        raw_size=sum(x[0] for x in chunks),
        stream_size=sum(len(x[1]) for x in chunks),
        trailing_size=len(data) - position,
    )
    if info["raw_size"] != values[6] * values[7] * 4:
        raise ValueError("invalid RGBA atlas size")
    if position != len(data):
        raise ValueError("unexpected trailing data")

    return info, data[tile_start:stream_header], chunks

def decompress_lz4(data, size):
    if size < 0:
        raise ValueError("invalid LZ4 output size")
    if not data:
        raise ValueError("empty LZ4 block")
    src = 0
    out = bytearray()
    final_literals = False
    while src < len(data):
        token = data[src]
        src += 1
        literal_size = token >> 4
        if literal_size == 15:
            while True:
                if src >= len(data):
                    raise ValueError("truncated LZ4 literal length")
                value = data[src]
                src += 1
                literal_size += value
                if value != 255:
                    break
        if src + literal_size > len(data):
            raise ValueError("invalid LZ4 literals")
        if len(out) + literal_size > size:
            raise ValueError("LZ4 output is too large")
        out.extend(data[src:src + literal_size])
        src += literal_size
        if src == len(data):
            final_literals = True
            break
        if src + 2 > len(data):
            raise ValueError("invalid LZ4 offset")
        offset = data[src] | data[src + 1] << 8
        src += 2
        if offset == 0 or offset > len(out):
            raise ValueError("invalid LZ4 match")
        match_size = (token & 15) + 4
        if (token & 15) == 15:
            while True:
                if src >= len(data):
                    raise ValueError("truncated LZ4 match length")
                value = data[src]
                src += 1
                match_size += value
                if value != 255:
                    break
        if len(out) + match_size > size:
            raise ValueError("LZ4 output is too large")
        block = out[-offset:]
        repeats, remainder = divmod(match_size, offset)
        out.extend(block * repeats + block[:remainder])
    if not final_literals:
        raise ValueError("LZ4 block does not end with literals")
    if len(out) != size:
        raise ValueError(f"LZ4 output is {len(out)} bytes, expected {size}")
    return bytes(out)

def unpack_tiles(info, tiles, atlas):
    width = info["crop_width"]
    height = info["crop_height"]
    atlas_width = info["atlas_width"]
    atlas_columns = info["atlas_columns"]
    if len(tiles) != (info["tile_count"] + 7) // 8:
        raise ValueError("invalid occupancy mask size")
    if len(atlas) != atlas_width * info["atlas_height"] * 4:
        raise ValueError("invalid atlas buffer size")
    if info["image_type"] == 3:
        image = bytearray(b"\0\xff\0\0" * (width * height))
    else:
        image = bytearray(width * height * 4)
    stored = 0
    for tile in range(info["tile_count"]):
        if not tiles[tile >> 3] & (1 << (tile & 7)):
            continue
        cell_x = stored % atlas_columns
        cell_y = stored // atlas_columns
        tile_x = tile % info["tile_columns"]
        tile_y = tile // info["tile_columns"]
        copy_width = min(32, width - tile_x * 32)
        copy_height = min(32, height - tile_y * 32)
        for y in range(copy_height):
            source = ((cell_y * 34 + y + 1) * atlas_width + cell_x * 34 + 1) * 4
            target = ((tile_y * 32 + y) * width + tile_x * 32) * 4
            if source + copy_width * 4 > len(atlas):
                raise ValueError("atlas cell is outside the pixel buffer")
            image[target:target + copy_width * 4] = atlas[source:source + copy_width * 4]
        stored += 1
    if stored != info["stored_tiles"]:
        raise ValueError("tile mask does not match stored tile count")
    return width, height, bytes(image)

def make_canvas(info, image):
    width = info["canvas_width"]
    height = info["canvas_height"]
    crop_width = info["crop_width"]
    crop_height = info["crop_height"]
    exponent = info["pixel_scale_code"] if info["pixel_scale_code"] in (0, 1, 2) else 0
    if info["image_type"] in (1, 2, 3):
        exponent -= 1
    if exponent < 0:
        raise ValueError("fractional canvas pixel scale is not supported")
    scale = 1 << exponent
    scaled_width = crop_width * scale
    scaled_height = crop_height * scale
    if info["crop_x"] + scaled_width > width or info["crop_y"] + scaled_height > height:
        raise ValueError("scaled crop does not fit the canvas")
    canvas = bytearray(width * height * 4)
    for y in range(crop_height):
        source = y * crop_width * 4
        row = image[source:source + crop_width * 4]
        if scale > 1:
            row = b"".join(row[x:x + 4] * scale for x in range(0, len(row), 4))
        for repeat in range(scale):
            target_y = info["crop_y"] + y * scale + repeat
            target = (target_y * width + info["crop_x"]) * 4
            canvas[target:target + scaled_width * 4] = row
    return width, height, bytes(canvas)

def palette_record_start(data, marker):
    candidates = []
    for extra in range(257):
        start = marker - 1029 - extra
        if start < 0 or data[start + 1024] not in (0, 1):
            continue
        access = struct.unpack_from("<I", data, start + 1025)[0]
        end = start + 1029
        if access:
            if access != 1 or end + 4 > marker:
                continue
            length = struct.unpack_from("<I", data, end)[0]
            end += 4 + length
        if end == marker:
            candidates.append(start)
    if len(candidates) != 1:
        raise ValueError("unsupported palette record")
    return candidates[0]

def palette_table(data, start):
    return [
        [tuple(data[start + i * 16 + c * 4:start + i * 16 + c * 4 + 4]) for c in range(4)]
        for i in range(64)
    ]

def is_costume_palette(source):
    value = source.replace("/", "\\").lower()
    return is_selection_palette(source) and "\\sprites\\" in value

def is_selection_palette(source):
    value = source.replace("/", "\\")
    base = value.rsplit("\\", 1)[-1].lower()
    if "\\palettes\\" not in value.lower() or "colormap" in base:
        return False
    return bool(re.search(r"\d+\.png$", base) or base.endswith("_gold.png"))

def read_palette_records_data(data, costume_only=False):
    records = []
    seen = set()
    for match in re.finditer(rb"\.png", data, re.I):
        end = match.end()
        marker = end - 4
        while marker and end - marker <= 512 and 32 <= data[marker - 1] <= 126:
            marker -= 1
        named = data.rfind(b"!src/", marker, end)
        if named >= 0:
            marker = named
        try:
            start = palette_record_start(data, marker)
        except ValueError:
            continue
        if start in seen:
            continue
        seen.add(start)
        source = data[marker:end].decode("utf-8", "replace")
        if costume_only and not is_costume_palette(source):
            continue
        property_value = data[start + 1024]
        access_flag = struct.unpack_from("<I", data, start + 1025)[0]
        access = ""
        if access_flag == 1:
            position = start + 1029
            length = struct.unpack_from("<I", data, position)[0]
            access = data[position + 4:position + 4 + length].decode("utf-8")
        records.append(
            {
                "offset": start,
                "source": source,
                "name": re.split(r"[\\/]", source)[-1][:-4],
                "property": property_value,
                "access_flag": access_flag,
                "access": access,
                "bytes": data[start:start + 1024],
                "palette": palette_table(data, start),
            }
        )
    return records

def read_palette_records(path, costume_only=False):
    return read_palette_records_data(path.read_bytes(), costume_only)

def find_palette_record(records, name):
    key = name.lower()
    if not key.endswith(".png"):
        key += ".png"
    matches = [record for record in records if key in record["source"].lower()]
    if not matches and name == "_color1":
        matches = [
            record for record in records
            if re.search(
                r"(?:color|palette)\D*1\.png$",
                record["source"].lower().rsplit("/", 1)[-1]
            )
            and "colormap" not in record["source"].lower().rsplit("/", 1)[-1]
        ]
    if not matches:
        raise ValueError(f"palette {name!r} was not found")
    if len({record["bytes"] for record in matches}) != 1:
        raise ValueError(f"palette name {name!r} is ambiguous")
    return matches[0]

def read_palette(path, name):
    return find_palette_record(read_palette_records(path), name)["palette"]

def normalized_parts(source):
    value = source.replace("\\", "/").lstrip("!/")
    return tuple(part.lower() for part in value.split("/") if part)

def common_prefix(left, right):
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count

def palette_catalog(paths):
    catalog = []
    for path in paths:
        data = path.read_bytes()
        records = [
            record for record in read_palette_records_data(data)
            if is_selection_palette(record["source"])
        ]
        if records:
            catalog.append(
                {"path": path, "records": records, "references": sprbin_references(data)}
            )
    return catalog

def source_key(source):
    value = "/".join(normalized_parts(source))
    return value[:-4] if value.endswith(".png") else value

def sprbin_references(data):
    references = set()
    for match in re.finditer(rb"!src/", data):
        if match.start() < 4:
            continue
        length = struct.unpack_from("<I", data, match.start() - 4)[0]
        end = match.start() + length
        if not 5 <= length <= 5000 or end > len(data):
            continue
        try:
            value = data[match.start():end].decode("utf-8")
        except UnicodeDecodeError:
            continue
        references.add(source_key(value))
    return references

def sprbin_owner(path):
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    parts = stem.lower().split("~")
    return parts[1] if len(parts) >= 3 and parts[0] == "sprites" else ""

def reference_palette_matches(source, catalog):
    key = source_key(source)
    return [item for item in catalog if key in item.get("references", ())]

def owner_palette_records(matches):
    records = []
    for item in matches:
        owner = sprbin_owner(item["path"]) or item["path"].stem
        for record in item["records"]:
            records.append(
                {
                    **record,
                    "owner": owner,
                    "sprbin": str(item["path"]),
                }
            )
    return records

def associate_palette(source, catalog):
    source_parts = normalized_parts(source)
    reference_matches = reference_palette_matches(source, catalog)
    if len(reference_matches) == 1:
        return reference_matches[0], None
    candidates = reference_matches or catalog
    scored = []
    for item in candidates:
        prefix = max(
            common_prefix(source_parts, normalized_parts(record["source"]))
            for record in item["records"]
        )
        owner = sprbin_owner(item["path"])
        owner_match = int(
            len(source_parts) >= 3
            and source_parts[1] == "sprites"
            and source_parts[2] == owner
        )
        score = (prefix, owner_match)
        if prefix >= 3:
            scored.append((score, item))
    if not scored:
        if reference_matches:
            names = ", ".join(str(item["path"]) for item in reference_matches[:4])
            return None, f"frame is shared by {names}"
        return None, "no palette source has a specific path match"
    best = max(score for score, _ in scored)
    matches = [item for score, item in scored if score == best]
    if len(matches) != 1:
        names = ", ".join(str(item["path"]) for item in matches[:4])
        return None, f"palette association is ambiguous between {names}"
    return matches[0], None

def default_palette_record(source, records):
    candidates = [
        record for record in records
        if re.search(r"(?:color|palette)\D*1\.png$", record["source"], re.I)
        and "colormap" not in record["source"].lower()
    ]
    if not candidates:
        return records[0] if records else None
    source_parts = normalized_parts(source)
    best = max(common_prefix(source_parts, normalized_parts(x["source"])) for x in candidates)
    matches = [
        record for record in candidates
        if common_prefix(source_parts, normalized_parts(record["source"])) == best
    ]
    tables = {record["bytes"] for record in matches}
    return matches[0] if len(tables) == 1 else None

def unique_palette_records(records):
    output = []
    seen = {}
    for record in records:
        key = (
            record.get("owner", "").lower(),
            record["name"].lower(),
            record["access"].lower(),
        )
        previous = seen.get(key)
        if previous is not None:
            if previous["bytes"] != record["bytes"]:
                raise ValueError(f"palette label {record['name']!r} has different tables")
            continue
        seen[key] = record
        output.append(record)
    return output

def apply_palette(pixels, palette, alpha_curve=False):
    image = bytearray(len(pixels))
    cache = {}
    for position in range(0, len(pixels), 4):
        material = pixels[position:position + 4]
        cached = cache.get(material)
        if cached is not None:
            image[position:position + 4] = cached
            continue
        red, green, blue, _ = material
        if red == 0 and green == 255:
            cache[material] = b"\0\0\0\0"
            continue

        scaled = red * 4.0 / 255.0
        band = math.floor(scaled)
        row = min(63, int((scaled - band) * 64.0))
        blend = band / 7.0
        metadata = shader_rgb(palette[row][3])
        blend_row = max(0, min(63, int(metadata[1])))
        shade = blue / 255.0
        if shade <= 0.8:
            shade = (shade - 0.5) / 0.6
        else:
            shade = 0.5 + (shade - 0.8) / 0.3
        shade = max(0.0, min(1.0, shade))
        column = 0 if shade <= 0.5 else 1
        amount = shade * 2.0 - column

        colors = []
        for palette_row in (row, blend_row):
            first = shader_rgb(palette[palette_row][column])
            second = shader_rgb(palette[palette_row][column + 1])
            colors.append([
                first[i] + (second[i] - first[i]) * amount for i in range(3)
            ])
        color = [colors[0][i] + (colors[1][i] - colors[0][i]) * blend for i in range(3)]

        edge = 1.0 - green / 255.0
        alpha = min(1.0, blue * 2.0 / 255.0 + edge)
        if alpha_curve:
            alpha = 1.0 - (1.0 - alpha) ** 2
            color = [value * alpha for value in color]
        output = bytes(
            [round(max(0.0, min(255.0, value * (1.0 - edge)))) for value in color]
            + [round(alpha * 255.0)]
        )
        cache[material] = output
        image[position:position + 4] = output
    return bytes(image)

def shader_rgb(entry):
    packed = (entry[0] << 16 | entry[1] << 8 | entry[2]) / 16777216.0
    return [(packed * factor % 1.0) * 255.0 for factor in (1.0, 256.0, 65536.0)]

def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(
        ">I", binascii.crc32(kind + data) & 0xffffffff
    )

def write_png(path, width, height, pixels):
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        rows.append(0)
        rows.extend(pixels[y * stride:(y + 1) * stride])
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(rows, 9))
        + png_chunk(b"IEND", b"")
    )

def files(path):
    if path.is_dir():
        yield from sorted(path.rglob("*.munged"))
    else:
        yield path

def is_mask(path):
    return path.stem.lower().endswith("_mask")

def mask_path(path, lookup):
    candidate = path.with_name(f"{path.stem}_mask{path.suffix}")
    return lookup.get(str(candidate).lower())

def decode_image(path, palette, alpha_curve):
    info, tiles, chunks = read_munged(path)
    atlas = b"".join(decompress_lz4(data, size) for size, data in chunks)
    width, height, image = unpack_tiles(info, tiles, atlas)
    if palette and info["image_type"] == 3:
        image = apply_palette(image, palette, alpha_curve)
    return info, tiles, chunks, atlas, width, height, image

def colorize(decoded, palette, alpha_curve):
    info, tiles, chunks, atlas, width, height, image = decoded
    if palette and info["image_type"] == 3:
        image = apply_palette(image, palette, alpha_curve)
    return info, tiles, chunks, atlas, width, height, image

def write_image(target, name, decoded, args, auxiliary=True):
    info, tiles, chunks, atlas, width, height, image = decoded
    if args.canvas:
        width, height, image = make_canvas(info, image)
    write_png(target / f"{name}.png", width, height, image)
    if auxiliary and args.atlas:
        write_png(
            target / f"{name}.atlas.png",
            info["atlas_width"], info["atlas_height"], atlas
        )
    if auxiliary and args.raw:
        (target / f"{name}.json").write_text(
            json.dumps(info, indent=2), encoding="utf-8"
        )
        (target / f"{name}.tiles").write_bytes(tiles)
        for index, (_, data) in enumerate(chunks):
            suffix = "" if len(chunks) == 1 else f".{index:03d}"
            (target / f"{name}.pixelstream{suffix}.lz4").write_bytes(data)
        (target / f"{name}.atlas.rgba").write_bytes(atlas)

def safe_source_path(source):
    value = source.replace("\\", "/").lstrip("!/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") or ":" in part for part in path.parts)
    ):
        raise ValueError(f"unsafe embedded source path {source!r}")
    return Path(*path.parts)

def output_location(path, relative, info, args):
    if not args.source_paths:
        return relative.parent, relative.stem
    source = safe_source_path(info["source"])
    name = source.stem
    if is_mask(path):
        name += ".mask"
    return source.parent, name

def reserve_name(target, name, source, claimed):
    candidate = name
    index = 2
    while True:
        key = str((target / f"{candidate}.png").resolve()).lower()
        previous = claimed.get(key)
        if previous is None or previous == source:
            claimed[key] = source
            return candidate
        candidate = f"{name}.duplicate_{index}"
        index += 1

def palette_slug(record):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", record["name"]).strip("._")
    if record["access"]:
        access = re.sub(r"[^A-Za-z0-9._-]+", "_", record["access"]).strip("._")
        value += f".{access}"
    if record.get("owner"):
        owner = re.sub(r"[^A-Za-z0-9._-]+", "_", record["owner"]).strip("._")
        value = f"{owner}.{value}"
    return value or "palette"

def write_variants(target, name, decoded, records, args):
    info = decoded[0]
    if not args.all_palettes or info["image_type"] != 3 or not records:
        palette = records[0]["palette"] if records else None
        write_image(target, name, colorize(decoded, palette, args.alpha_curve), args)
        return

    for index, record in enumerate(unique_palette_records(records)):
        variant = f"{name}.{palette_slug(record)}"
        write_image(
            target,
            variant,
            colorize(decoded, record["palette"], args.alpha_curve),
            args,
            auxiliary=index == 0,
        )

def palette_search_paths(input_path, roots):
    if roots:
        candidates = []
        for root in roots:
            if root.is_file():
                candidates.append(root)
            elif root.is_dir():
                candidates.extend(root.rglob("*.sprbin"))
            else:
                raise ValueError(f"palette root does not exist: {root}")
        return sorted(set(path.resolve() for path in candidates))

    start = input_path if input_path.is_dir() else input_path.parent
    direct = sorted(start.rglob("*.sprbin"))
    if direct:
        return direct
    for parent in start.parents:
        scripts = parent / "scripts"
        if parent.name.lower() == "munged" and scripts.is_dir():
            return sorted(scripts.rglob("*.sprbin"))
        if parent.name.lower() in ("srcdata", "extracted"):
            break
    return []

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--atlas", action="store_true")
    parser.add_argument("--canvas", action="store_true")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--palette", type=Path)
    parser.add_argument("--palette-name", default="_color1")
    parser.add_argument("--auto-palette", action="store_true")
    parser.add_argument("--palette-root", type=Path, action="append", default=[])
    parser.add_argument("--all-palettes", action="store_true")
    parser.add_argument("--source-paths", action="store_true")
    parser.add_argument("--alpha-curve", action="store_true")
    parser.add_argument("--mask", action="store_true")
    args = parser.parse_args()

    if args.palette and args.auto_palette:
        parser.error("--palette and --auto-palette cannot be used together")
    if args.palette_root and args.palette:
        parser.error("--palette-root is only used with automatic palette association")

    items = list(files(args.input))
    lookup = {str(path).lower(): path for path in items}
    if not args.input.is_dir():
        sibling = args.input.with_name(f"{args.input.stem}_mask{args.input.suffix}")
        if sibling.is_file():
            lookup[str(sibling).lower()] = sibling
    args.output.mkdir(parents=True, exist_ok=True)

    manual_records = None
    catalog = []
    if args.palette:
        all_records = read_palette_records(args.palette)
        manual_records = (
            unique_palette_records([x for x in all_records if is_selection_palette(x["source"])])
            if args.all_palettes
            else [find_palette_record(all_records, args.palette_name)]
        )
        if args.all_palettes and not manual_records:
            parser.error(f"no selection palettes were found in {args.palette}")
    elif args.auto_palette or args.all_palettes:
        palette_paths = palette_search_paths(args.input, args.palette_root)
        if not palette_paths:
            parser.error("no .sprbin files were found; use --palette-root")
        catalog = palette_catalog(palette_paths)
        if not catalog:
            parser.error("the discovered .sprbin files contain no selection palettes")

    claimed = {}
    warned = set()

    for path in items:
        if args.mask and is_mask(path):
            base = path.with_name(f"{path.stem[:-5]}{path.suffix}")
            if str(base).lower() in lookup:
                continue
        relative = path.relative_to(args.input) if args.input.is_dir() else Path(path.name)
        decoded = decode_image(path, None, args.alpha_curve)
        info = decoded[0]
        output_parent, output_name = output_location(path, relative, info, args)
        target = args.output / output_parent
        target.mkdir(parents=True, exist_ok=True)
        name = reserve_name(target, output_name, str(path), claimed)

        records = manual_records or []
        if info["image_type"] == 3 and catalog:
            associated, reason = associate_palette(info["source"], catalog)
            if associated is None:
                shared = reference_palette_matches(info["source"], catalog)
                if args.all_palettes and len(shared) > 1:
                    records = owner_palette_records(shared)
                    key = ("shared", tuple(str(item["path"]) for item in shared))
                    if key not in warned:
                        print(
                            f"notice: shared frame; writing palettes from "
                            f"{len(shared)} SPRBIN owners",
                            file=sys.stderr,
                        )
                        warned.add(key)
                else:
                    key = ("association", reason)
                    if key not in warned:
                        print(
                            f"warning: {reason}; unmatched type-3 images remain raw",
                            file=sys.stderr,
                        )
                        warned.add(key)
                    records = []
            elif args.all_palettes:
                records = associated["records"]
            else:
                if args.palette_name == "_color1":
                    default = default_palette_record(info["source"], associated["records"])
                else:
                    try:
                        default = find_palette_record(associated["records"], args.palette_name)
                    except ValueError:
                        default = None
                if default is None:
                    message = (
                        f"no unambiguous {args.palette_name} palette in "
                        f"{associated['path']}"
                    )
                    if message not in warned:
                        print(f"warning: {message}; affected images remain raw", file=sys.stderr)
                        warned.add(message)
                    records = []
                else:
                    records = [default]

        write_variants(target, name, decoded, records, args)
        if not args.mask or not info["has_mask"] or is_mask(path):
            continue
        companion = mask_path(path, lookup)
        if companion is None:
            raise ValueError(f"companion mask was not found for {path}")
        mask = decode_image(companion, None, args.alpha_curve)
        mask_info = mask[0]
        if (
            mask_info["source"] != info["source"]
            or mask_info["flags"] != info["flags"]
            or mask_info["canvas_width"] != info["canvas_width"]
            or mask_info["canvas_height"] != info["canvas_height"]
        ):
            raise ValueError(f"companion mask metadata does not match {path}")
        write_variants(target, f"{name}.mask", mask, records, args)

if __name__ == "__main__":
    main()
