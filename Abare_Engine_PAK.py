import argparse
import struct
from pathlib import Path, PurePosixPath

HEADER = struct.Struct("<4sII")
ENTRY = struct.Struct("<248sII")

def unpack(pak: Path, output: Path) -> None:
    with pak.open("rb") as source:
        magic, index_offset, index_size = HEADER.unpack(source.read(HEADER.size))
        if magic != b"PACK" or index_size % ENTRY.size:
            raise ValueError(f"Not a supported Abare PAK: {pak}")

        source.seek(index_offset)
        entries = []
        for _ in range(index_size // ENTRY.size):
            raw_name, offset, size = ENTRY.unpack(source.read(ENTRY.size))
            name = raw_name.split(b"\0", 1)[0].decode("utf-8")
            entries.append((name, offset, size))

        root = (output / pak.stem).resolve()
        for name, offset, size in entries:
            virtual_path = PurePosixPath(name)
            target = root.joinpath(*virtual_path.parts).resolve()
            if virtual_path.is_absolute() or root not in target.parents:
                raise ValueError(f"Unsafe archive path: {name}")

            target.parent.mkdir(parents=True, exist_ok=True)
            source.seek(offset)
            remaining = size
            with target.open("wb") as destination:
                while remaining:
                    chunk = source.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise EOFError(f"Truncated payload: {name}")
                    destination.write(chunk)
                    remaining -= len(chunk)

    print(f"Extracted {len(entries)} files from {pak.name} to {root}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pak", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("extracted"))
    args = parser.parse_args()
    unpack(args.pak, args.output)