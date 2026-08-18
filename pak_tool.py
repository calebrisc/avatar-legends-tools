#!/usr/bin/env python3
"""ALFG (ABARE engine) PAK tool — list & extract.
Format (reverse-engineered 2026-08-05):
  Header: b'PACK' | uint32 LE dir_offset | uint32 LE dir_size | zero padding
  Directory: N x 256-byte records: name[248] (null-padded) | uint32 LE offset | uint32 LE size
"""
import struct, sys, os

RECORD = 256
NAME_LEN = 248

def read_dir(path):
    with open(path, 'rb') as f:
        magic = f.read(4)
        if magic != b'PACK':
            raise ValueError(f"{path}: bad magic {magic!r}")
        dir_off, dir_size = struct.unpack('<II', f.read(8))
        if dir_size % RECORD:
            raise ValueError(f"{path}: dir_size {dir_size} not multiple of {RECORD}")
        f.seek(dir_off)
        raw = f.read(dir_size)
    entries = []
    for i in range(dir_size // RECORD):
        rec = raw[i*RECORD:(i+1)*RECORD]
        name = rec[:NAME_LEN].split(b'\0', 1)[0].decode('utf-8', 'replace')
        off, size = struct.unpack('<II', rec[NAME_LEN:NAME_LEN+8])
        entries.append((name, off, size))
    return entries

def cmd_list(pak):
    for name, off, size in read_dir(pak):
        print(f"{size:>12}  {name}")

def cmd_extract(pak, outdir, pattern=None):
    n = 0
    with open(pak, 'rb') as f:
        for name, off, size in read_dir(pak):
            if pattern and pattern.lower() not in name.lower():
                continue
            safe = name.replace('..', '__')
            dest = os.path.join(outdir, safe)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            f.seek(off)
            with open(dest, 'wb') as o:
                o.write(f.read(size))
            n += 1
    print(f"extracted {n} files -> {outdir}")

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("usage: pak_tool.py list <pak> | extract <pak> <outdir> [filter]"); sys.exit(1)
    if sys.argv[1] == 'list':
        cmd_list(sys.argv[2])
    elif sys.argv[1] == 'extract':
        cmd_extract(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
