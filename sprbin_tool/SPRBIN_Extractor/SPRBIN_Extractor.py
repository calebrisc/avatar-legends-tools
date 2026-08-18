from __future__ import annotations

import argparse
import csv
import io
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Iterator

class SprbinError(Exception):
    pass

class Reader:
    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.offset = offset

    def read(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise SprbinError(f"unexpected end of file at 0x{self.offset:x}")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def u8(self) -> int:
        if self.offset >= len(self.data):
            raise SprbinError(f"unexpected end of file at 0x{self.offset:x}")
        value = self.data[self.offset]
        self.offset += 1
        return value

    def boolean(self) -> bool:
        offset = self.offset
        value = self.u8()
        if value > 1:
            raise SprbinError(f"invalid Boolean {value} at 0x{offset:x}")
        return bool(value)

    def u16(self) -> int:
        if self.offset + 2 > len(self.data):
            raise SprbinError(f"unexpected end of file at 0x{self.offset:x}")
        value = struct.unpack_from("<H", self.data, self.offset)[0]
        self.offset += 2
        return value

    def u32(self) -> int:
        if self.offset + 4 > len(self.data):
            raise SprbinError(f"unexpected end of file at 0x{self.offset:x}")
        value = struct.unpack_from("<I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def i32(self) -> int:
        if self.offset + 4 > len(self.data):
            raise SprbinError(f"unexpected end of file at 0x{self.offset:x}")
        value = struct.unpack_from("<i", self.data, self.offset)[0]
        self.offset += 4
        return value

    def string(self, maximum: int = 1_000_000) -> str:
        offset = self.offset
        length = self.u32()
        if length > maximum:
            raise SprbinError(f"invalid string length {length} at 0x{offset:x}")
        try:
            value = self.read(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SprbinError(f"invalid UTF-8 string at 0x{offset + 4:x}") from exc
        if any(ord(char) < 32 and char not in "\t\r\n" for char in value):
            raise SprbinError(f"control character in string at 0x{offset + 4:x}")
        return value

def read_count(reader: Reader, maximum: int = 100_000) -> int:
    offset = reader.offset
    count = reader.u32()
    if count > maximum:
        raise SprbinError(f"invalid count {count} at 0x{offset:x}")
    return count

def parse_symbol_table(data: bytes, offset: int) -> tuple[list[dict], int]:
    reader = Reader(data, offset)
    count = read_count(reader, 10_000)
    if not count:
        raise SprbinError("empty symbol table")
    symbols = []
    for _ in range(count):
        entry_offset = reader.offset
        value = reader.u32()
        name = reader.string(2048)
        symbols.append({"offset": entry_offset, "id": value, "name": name})
    if sum(bool(item["name"]) for item in symbols) < min(2, count):
        raise SprbinError("implausible symbol table")
    return symbols, reader.offset

SYMBOL_LENGTH_PATTERN = re.compile(
    rb"(?=(?:[\x01-\xff][\x00-\x07]|\x00[\x01-\x08])\x00\x00)"
)

def symbol_candidates(
    data: bytes,
    exhaustive: bool = False,
) -> list[tuple[int, int, list[dict]]]:
    end = min(len(data) - 12, 2 * 1024 * 1024)
    candidates = []
    if exhaustive:
        offsets = range(8, max(8, end))
    else:
        offsets = (
            match.start() - 8
            for match in SYMBOL_LENGTH_PATTERN.finditer(data, 16, max(16, end + 8))
        )
    for offset in offsets:
        if offset < 8 or offset >= end:
            continue
        count = struct.unpack_from("<I", data, offset)[0]
        if not 1 <= count <= 10_000:
            continue
        first_length = struct.unpack_from("<I", data, offset + 8)[0]
        if first_length > 2048 or offset + 12 + first_length > len(data):
            continue
        try:
            symbols, table_end = parse_symbol_table(data, offset)
        except SprbinError:
            continue
        candidates.append((offset, table_end, symbols))
    candidates.sort(key=lambda item: (-len(item[2]), item[0]))
    return candidates

def parse_frame(reader: Reader) -> dict:
    offset = reader.offset
    source = reader.string(5000)
    if not source.startswith("!src/"):
        raise SprbinError(f"invalid frame source at 0x{offset:x}")
    flags_before = [reader.boolean() for _ in range(3)]
    extra_count = read_count(reader, 10_000)
    extra = [list(struct.unpack("<7I", reader.read(28))) for _ in range(extra_count)]
    placement_count = read_count(reader, 10_000)
    placements = []
    for _ in range(placement_count):
        placements.append(
            {
                "id": reader.u32(),
                "x": reader.i32(),
                "y": reader.i32(),
                "z": reader.i32(),
                "flag": reader.boolean(),
            }
        )
    flags_after = [reader.boolean() for _ in range(6)]
    frame_type = reader.u32()
    if frame_type > 255:
        raise SprbinError(f"invalid frame type {frame_type} at 0x{reader.offset - 4:x}")
    return {
        "offset": offset,
        "source": source,
        "flags_before": flags_before,
        "extra_records": extra,
        "placements": placements,
        "flags_after": flags_after,
        "tail_value": frame_type,
    }

def condition_vector(reader: Reader) -> int:
    count = read_count(reader)
    reader.read(count * 44)
    return count

def hash_string(reader: Reader) -> dict:
    offset = reader.offset
    return {"offset": offset, "id": reader.u32(), "value": reader.string()}

def script_node(reader: Reader, stats: dict) -> dict:
    item_count = read_count(reader)
    reader.read(item_count * 30)
    reader.read(30)
    reader.boolean()
    reader.boolean()
    reader.u16()
    reader.u8()
    value = hash_string(reader)
    stats["script_items"] += item_count
    return value

def script_node_vector(reader: Reader, stats: dict) -> int:
    count = read_count(reader, 10_000)
    for _ in range(count):
        script_node(reader, stats)
    return count

def small_group_vector(reader: Reader, stats: dict) -> int:
    count = read_count(reader)
    for _ in range(count):
        reader.u32()
        stats["condition_records"] += condition_vector(reader)
        reader.u32()
    return count

def script_object(reader: Reader, stats: dict) -> dict:
    item_count = read_count(reader)
    reader.read(item_count * 30)
    reader.read(30)
    flags = [reader.boolean(), reader.boolean()]
    value16 = reader.u16()
    value8 = reader.u8()
    name = hash_string(reader)
    stats["script_items"] += item_count
    return {"flags": flags, "value16": value16, "value8": value8, "name": name}

def script_object_vector(reader: Reader, stats: dict) -> int:
    count = read_count(reader, 10_000)
    for _ in range(count):
        script_object(reader, stats)
    return count

def parse_state(reader: Reader) -> dict:
    offset = reader.offset
    name = reader.string(1000)
    if not name:
        raise SprbinError(f"empty state name at 0x{offset:x}")
    stats = {
        "primary_groups": 0,
        "condition_vectors": 0,
        "condition_records": 0,
        "ten_byte_records": 0,
        "small_groups": 0,
        "script_groups": 0,
        "script_items": 0,
    }

    primary_count = read_count(reader, 10_000)
    stats["primary_groups"] = primary_count
    for _ in range(primary_count):
        reader.read(12)
        stats["condition_vectors"] += 1
        stats["condition_records"] += condition_vector(reader)
        reader.boolean()
        reader.read(12)

    for _ in range(4):
        stats["condition_vectors"] += 1
        stats["condition_records"] += condition_vector(reader)

    key_count = read_count(reader)
    stats["ten_byte_records"] = key_count
    reader.read(key_count * 10)
    value0 = reader.u32()
    flag0 = reader.boolean()
    value1 = reader.u32()
    flag1 = reader.boolean()
    source = reader.string(5000)

    for _ in range(2):
        stats["small_groups"] += small_group_vector(reader, stats)
    for _ in range(6):
        stats["condition_vectors"] += 1
        stats["condition_records"] += condition_vector(reader)

    value2 = reader.u32()
    reserved_value = reader.u32()
    reserved_data = reader.read(128)
    value3 = reader.u32()
    value4 = reader.u32()
    flag2 = reader.boolean()

    script_group_count = read_count(reader, 10_000)
    stats["script_groups"] = script_group_count
    embedded_strings = []
    for _ in range(script_group_count):
        reader.boolean()
        stats["condition_vectors"] += 1
        stats["condition_records"] += condition_vector(reader)
        embedded_strings.append(hash_string(reader))
        embedded_strings.append(hash_string(reader))
        embedded_strings.append(script_node(reader, stats))
        script_node_vector(reader, stats)

    return {
        "offset": offset,
        "size": reader.offset - offset,
        "name": name,
        "source": source,
        "values": [value0, value1, value2, value3, value4],
        "flags": [flag0, flag1, flag2],
        "reserved_value": reserved_value,
        "reserved_data": reserved_data.hex(),
        "embedded_strings": embedded_strings,
        "compiled": stats,
    }

def possible_layout_start(data: bytes, offset: int) -> bool:
    if offset + 16 > len(data):
        return False
    frame_count = struct.unpack_from("<I", data, offset)[0]
    if frame_count > 10_000:
        return False
    if frame_count:
        length = struct.unpack_from("<I", data, offset + 4)[0]
        return 5 <= length <= 5000 and data[offset + 8:offset + 13] == b"!src/"
    pair_count = struct.unpack_from("<I", data, offset + 4)[0]
    if pair_count > 10_000:
        return False
    state_count_offset = offset + 8 + pair_count * 8
    if state_count_offset + 8 > len(data):
        return False
    state_count = struct.unpack_from("<I", data, state_count_offset)[0]
    name_length = struct.unpack_from("<I", data, state_count_offset + 4)[0]
    return 1 <= state_count <= 10_000 and 1 <= name_length <= 1000

def parse_layout(data: bytes, offset: int) -> dict:
    reader = Reader(data, offset)
    frame_count = read_count(reader, 10_000)
    frames = [parse_frame(reader) for _ in range(frame_count)]
    pair_count = read_count(reader, 10_000)
    pairs = [list(struct.unpack("<2I", reader.read(8))) for _ in range(pair_count)]
    state_count = read_count(reader, 10_000)
    if not state_count:
        raise SprbinError("layout has no states")
    states = [parse_state(reader) for _ in range(state_count)]
    return {
        "offset": offset,
        "end": reader.offset,
        "frames": frames,
        "pairs": pairs,
        "states": states,
    }

def locate_layout(data: bytes, start: int) -> dict:
    tried = set()
    for match in re.finditer(rb"!src/", data[start:]):
        offset = start + match.start() - 8
        if offset < start or offset in tried or not possible_layout_start(data, offset):
            continue
        tried.add(offset)
        try:
            return parse_layout(data, offset)
        except SprbinError:
            pass

    end = len(data) - 16
    search_end = end + 3 if end > start else start
    offset = data.find(b"\0\0\0\0", start, search_end)
    while offset >= 0:
        if not possible_layout_start(data, offset):
            offset = data.find(b"\0\0\0\0", offset + 1, search_end)
            continue
        try:
            return parse_layout(data, offset)
        except SprbinError:
            pass
        offset = data.find(b"\0\0\0\0", offset + 1, search_end)
    raise SprbinError("could not locate the frame/state table")

def palette_record(data: bytes, marker: int) -> dict | None:
    candidates = []
    for extra in range(257):
        start = marker - 1029 - extra
        if start < 0 or data[start + 1024] not in (0, 1):
            continue
        access_flag = struct.unpack_from("<I", data, start + 1025)[0]
        end = start + 1029
        access = ""
        if access_flag:
            if access_flag != 1 or end + 4 > marker:
                continue
            length = struct.unpack_from("<I", data, end)[0]
            if length > 252 or end + 4 + length != marker:
                continue
            try:
                access = data[end + 4:marker].decode("utf-8")
            except UnicodeDecodeError:
                continue
            end = marker
        if end == marker:
            candidates.append((start, access_flag, access))
    if len(candidates) != 1:
        return None
    start, access_flag, access = candidates[0]
    return {
        "offset": start,
        "property": data[start + 1024],
        "access_flag": access_flag,
        "access": access,
    }

def find_palettes(data: bytes) -> list[dict]:
    palettes = []
    seen = set()
    for match in re.finditer(rb"\.png", data, re.I):
        end = match.end()
        marker = end - 4
        while marker and end - marker <= 512 and 32 <= data[marker - 1] <= 126:
            marker -= 1
        named = data.rfind(b"!src/", marker, end)
        if named >= 0:
            marker = named
        record = palette_record(data, marker)
        if record is None or record["offset"] in seen:
            continue
        seen.add(record["offset"])
        record["source"] = data[marker:end].decode("utf-8", "replace")
        record["label_offset"] = marker
        record["label_size"] = end - marker
        record["table_size"] = 1024
        palettes.append(record)
    return palettes

LOCALIZATION_HEADER = (
    "Flag,Reference,FileShortname,Tag,EN,FR,IT,DE,ES,KR,JP,CN,SC,BR,LA,RU"
)

def find_localizations(data: bytes) -> list[dict]:
    header = LOCALIZATION_HEADER.encode("ascii")
    records = []
    seen = set()
    position = 0
    while True:
        position = data.find(header, position)
        if position < 0:
            break
        starts = [position]
        if position >= 3 and data[position - 3:position] == b"\xef\xbb\xbf":
            starts.insert(0, position - 3)
        for start in starts:
            if start < 4:
                continue
            size = struct.unpack_from("<I", data, start - 4)[0]
            end = start + size
            if not size or end > len(data) or start in seen:
                continue
            raw = data[start:end]
            try:
                text = raw.decode("utf-8-sig")
                rows = list(csv.reader(io.StringIO(text)))
            except (UnicodeDecodeError, csv.Error):
                continue
            if not rows or rows[0] != LOCALIZATION_HEADER.split(","):
                continue
            seen.add(start)
            language_counts = {}
            for index, language in enumerate(rows[0][4:], 4):
                language_counts[language] = sum(
                    len(row) > index and bool(row[index].strip()) for row in rows[1:]
                )
            records.append(
                {
                    "prefix_offset": start - 4,
                    "offset": start,
                    "size": size,
                    "end": end,
                    "bom": raw.startswith(b"\xef\xbb\xbf"),
                    "rows": len(rows),
                    "language_counts": language_counts,
                }
            )
            break
        position += len(header)
    return records

def find_dependencies(data: bytes) -> list[str]:
    values = []
    seen = set()
    for match in re.finditer(rb"!src/", data):
        marker = match.start()
        if marker >= 4:
            length = struct.unpack_from("<I", data, marker - 4)[0]
            if 5 <= length <= 5000 and marker + length <= len(data):
                raw = data[marker:marker + length]
                try:
                    value = raw.decode("utf-8")
                except UnicodeDecodeError:
                    value = ""
                if value and all(ord(char) >= 32 for char in value):
                    if value not in seen:
                        seen.add(value)
                        values.append(value)
                    continue
        tail = data[marker:marker + 1024]
        path_match = re.match(rb"!src/[^\x00-\x1f]{1,1000}", tail)
        if path_match:
            value = path_match.group().decode("utf-8", "replace")
            if value not in seen:
                seen.add(value)
                values.append(value)
    return values

def find_audio_references(data: bytes) -> list[str]:
    references = []
    for match in re.finditer(rb"!src/", data):
        offset = match.start()
        if offset < 4:
            continue
        size = struct.unpack_from("<I", data, offset - 4)[0]
        if size < 5 or size > 5000 or offset + size > len(data):
            continue
        try:
            value = data[offset:offset + size].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if value.lower().endswith(".ogg") and all(ord(char) >= 32 for char in value):
            references.append(value)
    return references

def fsb_name(data: bytes, offset: int, header_size: int, name_size: int) -> str:
    start = offset + 60 + header_size
    table = data[start:start + name_size]
    if len(table) < 5:
        raise SprbinError(f"invalid FSB5 name table at 0x{offset:x}")
    name_offset = struct.unpack_from("<I", table)[0]
    if name_offset < 4 or name_offset >= len(table):
        raise SprbinError(f"invalid FSB5 name offset at 0x{offset:x}")
    end = table.find(b"\0", name_offset)
    if end < 0:
        raise SprbinError(f"unterminated FSB5 name at 0x{offset:x}")
    try:
        return table[name_offset:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SprbinError(f"invalid FSB5 name at 0x{offset:x}") from exc

def find_audio(data: bytes, source: str) -> list[dict]:
    banks = []
    position = 0
    while True:
        offset = data.find(b"FSB5", position)
        if offset < 0:
            break
        position = offset + 4
        if offset < 4 or offset + 60 > len(data):
            continue
        version, samples, headers, names, payload, mode, flags = struct.unpack_from(
            "<7I", data, offset + 4
        )
        size = 60 + headers + names + payload
        declared = struct.unpack_from("<I", data, offset - 4)[0]
        if (
            version != 1
            or samples != 1
            or mode not in (15, 16)
            or size != declared
            or size <= 60
            or offset + size > len(data)
        ):
            continue
        name = fsb_name(data, offset, headers, names)
        banks.append(
            {
                "offset": offset,
                "end": offset + size,
                "size": size,
                "mode": mode,
                "codec": "vorbis" if mode == 15 else "fmod_adpcm",
                "name": name,
                "flags": flags,
            }
        )
        position = offset + size

    references = find_audio_references(data)
    if len(references) != len(banks):
        raise SprbinError(
            f"{source}: found {len(references)} OGG references but {len(banks)} FSB5 banks"
        )
    for reference, bank in zip(references, banks):
        expected = Path(reference.replace("\\", "/")).stem
        if bank["name"].casefold() != expected.casefold():
            raise SprbinError(
                f"{source}: FSB5 name {bank['name']!r} does not match {reference!r}"
            )
        bank["source"] = reference
    return banks

def parse_sprbin(data: bytes, source: str = "<memory>") -> dict:
    if len(data) < 16:
        raise SprbinError(f"{source}: file is too small")
    versions = struct.unpack_from("<4H", data)
    selected = None
    for exhaustive in (False, True):
        candidates = symbol_candidates(data, exhaustive)
        for symbol_offset, symbol_end, symbols in candidates[:64]:
            try:
                layout = locate_layout(data, symbol_end)
            except SprbinError:
                continue
            selected = symbol_offset, symbol_end, symbols, layout
            break
        if selected is not None:
            break
    if selected is None:
        raise SprbinError(f"{source}: could not match symbol, frame and state tables")

    symbol_offset, symbol_end, symbols, layout = selected
    return {
        "source": source,
        "size": len(data),
        "versions": {
            "operation_count": versions[0],
            "variable_count": versions[1],
            "manual_version": versions[2],
            "keyframe_version": versions[3],
        },
        "symbol_table": {
            "offset": symbol_offset,
            "end": symbol_end,
            "count": len(symbols),
            "entries": symbols,
        },
        "frame_state_table": layout,
        "palettes": find_palettes(data[:symbol_offset]),
        "localizations": find_localizations(data),
        "audio": find_audio(data, source),
        "dependencies": find_dependencies(data),
        "unparsed_prefix_bytes": symbol_offset - 8,
        "unparsed_suffix_bytes": len(data) - layout["end"],
    }

def data_from_input(path: Path) -> Iterator[tuple[str, bytes]]:
    if path.is_file():
        if path.suffix.lower() != ".sprbin":
            raise SprbinError(f"input file is not .sprbin: {path}")
        yield str(path), path.read_bytes()
        return
    if path.is_dir():
        files = sorted(item for item in path.rglob("*.sprbin") if item.is_file())
        for item in files:
            yield str(item), item.read_bytes()
        return
    raise SprbinError(f"input does not exist: {path}")

def safe_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).rstrip(" .")
    return value or "unnamed"

def source_asset_path(value: str) -> Path:
    normalized = value.replace("\\", "/")
    if not normalized.lower().startswith("!src/"):
        raise SprbinError(f"invalid source asset path {value!r}")
    parts = normalized[5:].split("/")
    if not parts or any(part in ("", ".", "..") or ":" in part for part in parts):
        raise SprbinError(f"unsafe source asset path {value!r}")
    return Path(*(safe_component(part) for part in parts))

def find_vgmstream() -> Path | None:
    folder = Path(__file__).resolve().parent
    candidates = (
        folder / "vgmstream-cli.exe",
        folder / "vgmstream-cli",
        folder / "vgmstream-cli" / "vgmstream-cli.exe",
        folder / "vgmstream-cli" / "vgmstream-cli",
        folder / "vgmstream" / "vgmstream-cli.exe",
        folder / "vgmstream" / "vgmstream-cli",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    executable = shutil.which("vgmstream-cli")
    if executable:
        return Path(executable)
    return None

def run_vgmstream(
    executable: Path,
    source: Path,
    target: Path,
    display_source: Path,
) -> None:
    result = subprocess.run(
        [str(executable), "-i", "-o", str(target), str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    if result.returncode:
        message = result.stdout.strip() or "unknown decoder error"
        raise SprbinError(f"audio extraction failed for {display_source}: {message}")
    if not target.is_file() or target.stat().st_size < 44:
        raise SprbinError(f"audio decoder did not create a valid WAV for {display_source}")

def decode_fsb(executable: Path, source: Path, target: Path) -> None:
    executable = executable.resolve()
    source_path = source.resolve()
    target_path = target.resolve()
    if sys.platform == "win32" and max(len(str(source_path)), len(str(target_path))) >= 260:
        with tempfile.TemporaryDirectory(prefix="sprbin_audio_") as temporary:
            temporary_path = Path(temporary)
            temporary_source = temporary_path / "input.fsb"
            temporary_target = temporary_path / "output.wav"
            shutil.copyfile(source_path, temporary_source)
            run_vgmstream(
                executable,
                temporary_source,
                temporary_target,
                source,
            )
            shutil.copyfile(temporary_target, target_path)
    else:
        run_vgmstream(executable, source, target, source)
    if not target_path.is_file() or target_path.stat().st_size < 44:
        raise SprbinError(f"audio decoder did not create a valid WAV for {source}")

def export_relative(source: str, input_path: Path) -> Path:
    path = Path(source)
    if input_path.is_dir():
        try:
            relative = path.resolve().relative_to(input_path.resolve())
        except ValueError:
            relative = Path(path.name)
        parts = [safe_component(part) for part in relative.parts]
        parts[-1] = safe_component(Path(parts[-1]).stem)
        return Path(*parts)
    return Path(safe_component(path.stem))

def csv_data(rows: list[list[object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")

def png_chunk(name: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + name
        + payload
        + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
    )

def palette_png(table: bytes) -> bytes:
    scale = 8
    scanlines = bytearray()
    for row in range(64):
        line = bytearray([0])
        for column in range(4):
            offset = row * 16 + column * 4
            pixel = table[offset:offset + 3] + b"\xff"
            line.extend(pixel * scale)
        scanlines.extend(line * scale)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">2I5B", 4 * scale, 64 * scale, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(bytes(scanlines), 9))
        + png_chunk(b"IEND", b"")
    )

def write_artifact(
    root: Path,
    relative: Path,
    payload: bytes,
) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

def export_sprbin(data: bytes, result: dict, target: Path) -> dict:
    decoder = find_vgmstream() if result["audio"] else None
    if target.exists():
        raise SprbinError(f"export target already exists: {target}")
    target.mkdir(parents=True)

    symbols = [["index", "offset", "id", "name"]]
    for index, item in enumerate(result["symbol_table"]["entries"]):
        symbols.append([index, item["offset"], item["id"], item["name"]])
    write_artifact(target, Path("tables/symbols.csv"), csv_data(symbols))

    frames = [[
        "index",
        "offset",
        "source",
        "flag_before_0",
        "flag_before_1",
        "flag_before_2",
        "extra_records",
        "placements",
        "flag_after_0",
        "flag_after_1",
        "flag_after_2",
        "flag_after_3",
        "flag_after_4",
        "flag_after_5",
        "tail_value",
    ]]
    frame_extras = [[
        "frame_index",
        "record_index",
        "value0",
        "value1",
        "value2",
        "value3",
        "value4",
        "value5",
        "value6",
    ]]
    frame_placements = [[
        "frame_index",
        "placement_index",
        "id",
        "x",
        "y",
        "z",
        "flag",
    ]]
    for index, item in enumerate(result["frame_state_table"]["frames"]):
        frames.append(
            [
                index,
                item["offset"],
                item["source"],
                *(int(value) for value in item["flags_before"]),
                len(item["extra_records"]),
                len(item["placements"]),
                *(int(value) for value in item["flags_after"]),
                item["tail_value"],
            ]
        )
        for record_index, record in enumerate(item["extra_records"]):
            frame_extras.append([index, record_index, *record])
        for placement_index, placement in enumerate(item["placements"]):
            frame_placements.append(
                [
                    index,
                    placement_index,
                    placement["id"],
                    placement["x"],
                    placement["y"],
                    placement["z"],
                    int(placement["flag"]),
                ]
            )
    write_artifact(target, Path("tables/frames.csv"), csv_data(frames))
    write_artifact(
        target,
        Path("tables/frame_extras.csv"),
        csv_data(frame_extras),
    )
    write_artifact(
        target,
        Path("tables/frame_placements.csv"),
        csv_data(frame_placements),
    )

    states = [[
        "index",
        "offset",
        "size",
        "name",
        "source",
        "value0",
        "value1",
        "value2",
        "value3",
        "value4",
        "flag0",
        "flag1",
        "flag2",
        "reserved_value",
        "reserved_data",
        "primary_groups",
        "condition_vectors",
        "condition_records",
        "ten_byte_records",
        "small_groups",
        "script_groups",
        "script_items",
    ]]
    state_strings = [["state_index", "string_index", "offset", "id", "value"]]
    for index, item in enumerate(result["frame_state_table"]["states"]):
        compiled = item["compiled"]
        states.append(
            [
                index,
                item["offset"],
                item["size"],
                item["name"],
                item["source"],
                *item["values"],
                *(int(value) for value in item["flags"]),
                item["reserved_value"],
                item["reserved_data"],
                compiled["primary_groups"],
                compiled["condition_vectors"],
                compiled["condition_records"],
                compiled["ten_byte_records"],
                compiled["small_groups"],
                compiled["script_groups"],
                compiled["script_items"],
            ]
        )
        for string_index, value in enumerate(item["embedded_strings"]):
            state_strings.append(
                [index, string_index, value["offset"], value["id"], value["value"]]
            )
    write_artifact(target, Path("tables/states.csv"), csv_data(states))
    write_artifact(
        target,
        Path("tables/state_strings.csv"),
        csv_data(state_strings),
    )

    pairs = [["index", "value0", "value1"]]
    for index, pair in enumerate(result["frame_state_table"]["pairs"]):
        pairs.append([index, pair[0], pair[1]])
    write_artifact(target, Path("tables/pairs.csv"), csv_data(pairs))

    dependencies = "".join(f"{value}\n" for value in result["dependencies"]).encode("utf-8")
    write_artifact(
        target,
        Path("tables/dependencies.txt"),
        dependencies,
    )

    palette_records = [[
        "index",
        "offset",
        "table_size",
        "property",
        "access_flag",
        "access",
        "source",
    ]]
    for index, palette in enumerate(result["palettes"]):
        table = data[palette["offset"]:palette["offset"] + 1024]
        label = safe_component(Path(palette["source"].replace("\\", "/")).stem)
        base = f"{index:04d}_{palette['offset']:08x}_{label}"
        palette_records.append(
            [
                index,
                palette["offset"],
                palette["table_size"],
                palette["property"],
                palette["access_flag"],
                palette["access"],
                palette["source"],
            ]
        )
        write_artifact(
            target,
            Path(f"palettes/{base}.rgba"),
            table,
        )
        rows = [["row", "column", "r", "g", "b", "x"]]
        for row in range(64):
            for column in range(4):
                offset = row * 16 + column * 4
                rows.append([row, column, *table[offset:offset + 4]])
        write_artifact(
            target,
            Path(f"palettes/{base}.csv"),
            csv_data(rows),
        )
        write_artifact(
            target,
            Path(f"palettes/{base}.png"),
            palette_png(table),
        )
    write_artifact(
        target,
        Path("tables/palette_records.csv"),
        csv_data(palette_records),
    )

    for index, record in enumerate(result["localizations"]):
        payload = data[record["offset"]:record["end"]]
        write_artifact(
            target,
            Path(f"localization/{index:02d}_{record['offset']:08x}.csv"),
            payload,
        )

    audio_paths = set()
    for record in result["audio"]:
        source_path = source_asset_path(record["source"])
        fsb_relative = Path("audio") / source_path.with_suffix(".fsb")
        key = fsb_relative.as_posix().casefold()
        if key in audio_paths:
            raise SprbinError(f"duplicate audio output path {fsb_relative}")
        audio_paths.add(key)
        fsb_target = target / fsb_relative
        payload = data[record["offset"]:record["end"]]
        write_artifact(target, fsb_relative, payload)
        if decoder is not None:
            decode_fsb(decoder, fsb_target, fsb_target.with_suffix(".wav"))

    bounds = [
        ("00_header", 0, 8),
        ("01_prefix", 8, result["symbol_table"]["offset"]),
        (
            "02_symbols",
            result["symbol_table"]["offset"],
            result["symbol_table"]["end"],
        ),
        (
            "03_between",
            result["symbol_table"]["end"],
            result["frame_state_table"]["offset"],
        ),
        (
            "04_frame_states",
            result["frame_state_table"]["offset"],
            result["frame_state_table"]["end"],
        ),
        ("05_suffix", result["frame_state_table"]["end"], len(data)),
    ]
    segments = []
    for name, start, end in bounds:
        if end < start:
            raise SprbinError(f"invalid export segment {name}: 0x{start:x}-0x{end:x}")
        if start == end:
            continue
        relative = Path(f"raw/{name}_{start:08x}_{end:08x}.bin")
        payload = data[start:end]
        write_artifact(
            target,
            relative,
            payload,
        )
        segments.append({"path": relative.as_posix(), "offset": start, "end": end})
    if not segments or segments[0]["offset"] != 0 or segments[-1]["end"] != len(data):
        raise SprbinError("raw export does not cover the complete SPRBIN")
    for left, right in zip(segments, segments[1:]):
        if left["end"] != right["offset"]:
            raise SprbinError("raw export segments are not contiguous")

    return {
        "source": result["source"],
        "output": str(target),
        "size": len(data),
        "audio_banks": len(result["audio"]),
        "audio_wavs": len(result["audio"]) if decoder is not None else 0,
    }

def main() -> int:
    parser = argparse.ArgumentParser(description="Extract assets from Avatar Legends .sprbin files")
    parser.add_argument("input", type=Path)
    parser.add_argument("export", type=Path, metavar="OUTPUT")
    args = parser.parse_args()

    try:
        exports = []
        for source, data in data_from_input(args.input):
            parsed = parse_sprbin(data, source)
            relative = export_relative(source, args.input)
            exports.append(export_sprbin(data, parsed, args.export / relative))
        if not exports:
            raise SprbinError(f"no .sprbin data found in {args.input}")
    except (OSError, SprbinError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    total = sum(item["size"] for item in exports)
    print(f"exported {len(exports)} SPRBIN files ({total} bytes) to {args.export}")
    audio_banks = sum(item["audio_banks"] for item in exports)
    audio_wavs = sum(item["audio_wavs"] for item in exports)
    if audio_banks and audio_wavs != audio_banks:
        print(
            f"warning: extracted {audio_banks} FSB5 banks without WAV conversion; "
            "put the complete vgmstream release in a vgmstream-cli folder beside "
            "sprbin_tool.py or add vgmstream-cli to PATH",
            file=sys.stderr,
        )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
