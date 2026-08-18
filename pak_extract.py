import argparse
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

HEADER = struct.Struct("<4sII")
ENTRY = struct.Struct("<248sII")
COPY_SIZE = 1024 * 1024

class PakError(Exception):
    pass

@dataclass(frozen=True)
class PakEntry:
    path: str
    offset: int
    size: int

    @property
    def end(self):
        return self.offset + self.size

def safe_parts(value):
    if "\\" in value:
        raise PakError(f"unsafe archive path {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise PakError(f"unsafe archive path {value!r}")
    if any(part in ("", ".", "..") or (os.name == "nt" and ":" in part) for part in path.parts):
        raise PakError(f"unsafe archive path {value!r}")
    return path.parts

class PakArchive:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.entries = []
        self._read()

    def _read(self):
        size = self.path.stat().st_size
        if size < HEADER.size:
            raise PakError(f"{self.path}: file is smaller than the header")
        with self.path.open("rb") as stream:
            magic, directory_offset, directory_size = HEADER.unpack(stream.read(HEADER.size))
            if magic != b"PACK":
                raise PakError(f"{self.path}: invalid PACK signature")
            if directory_size % ENTRY.size:
                raise PakError(f"{self.path}: invalid directory size")
            if directory_offset < HEADER.size or directory_offset + directory_size != size:
                raise PakError(f"{self.path}: invalid directory range")
            stream.seek(directory_offset)
            seen = set()
            for index in range(directory_size // ENTRY.size):
                record = stream.read(ENTRY.size)
                if len(record) != ENTRY.size:
                    raise PakError(f"{self.path}: truncated directory entry {index}")
                raw_name, offset, length = ENTRY.unpack(record)
                raw_name = raw_name.split(b"\0", 1)[0]
                try:
                    name = raw_name.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise PakError(f"{self.path}: invalid UTF-8 path at entry {index}") from error
                safe_parts(name)
                folded = name.casefold()
                if not name or folded in seen:
                    raise PakError(f"{self.path}: empty or duplicate path {name!r}")
                if offset < HEADER.size or offset + length > directory_offset:
                    raise PakError(f"{self.path}: payload outside the data area for {name!r}")
                seen.add(folded)
                self.entries.append(PakEntry(name, offset, length))
        populated = sorted((entry for entry in self.entries if entry.size), key=lambda x: x.offset)
        for previous, current in zip(populated, populated[1:]):
            if previous.end > current.offset:
                raise PakError(f"{self.path}: overlapping payloads")

    def extract(self, output):
        root = (Path(output).resolve() / self.path.stem).resolve()
        root.mkdir(parents=True, exist_ok=True)
        total = 0
        with self.path.open("rb") as source:
            for entry in self.entries:
                target = root.joinpath(*safe_parts(entry.path)).resolve()
                try:
                    target.relative_to(root)
                except ValueError as error:
                    raise PakError(f"{self.path}: path escapes output directory") from error
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    destination = target.open("xb")
                except FileExistsError as error:
                    raise PakError(f"refusing to overwrite {target}") from error
                with destination:
                    source.seek(entry.offset)
                    remaining = entry.size
                    while remaining:
                        block = source.read(min(remaining, COPY_SIZE))
                        if not block:
                            raise PakError(f"{self.path}: unexpected end of file")
                        destination.write(block)
                        remaining -= len(block)
                total += entry.size
        return len(self.entries), total

def size_text(value):
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024

def archives(source):
    source = Path(source)
    if not source.is_dir():
        raise PakError(f"source folder does not exist: {source}")
    paths = sorted(
        (path for path in source.rglob("*") if path.is_file() and path.suffix.lower() == ".pak"),
        key=lambda path: str(path).casefold(),
    )
    if not paths:
        raise PakError(f"No PAK files found below {source}")
    return paths

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        paths = archives(args.source)
        args.destination.mkdir(parents=True, exist_ok=True)
        file_count = 0
        byte_count = 0
        for path in paths:
            count, size = PakArchive(path).extract(args.destination)
            file_count += count
            byte_count += size
            print(f"{path.name}: {count} files, {size_text(size)}")
        print(f"Done: {file_count} files, {size_text(byte_count)}")
    except (OSError, PakError) as error:
        parser.exit(1, f"error: {error}\n")

if __name__ == "__main__":
    main()