#!/usr/bin/env python3
"""ALFG (ABARE engine) PAK builder — inverse of pak_tool.py.
Layout mirrors shipped paks: 4KB-aligned data blocks starting at 0x1000,
directory appended after the last file's data.
"""
import struct, sys, os

RECORD = 256
NAME_LEN = 248
DATA_START = 0x1000
ALIGN = 0x1000

def build(srcdir, out_pak, order=None):
    files = []
    for root, _, names in os.walk(srcdir):
        for n in names:
            full = os.path.join(root, n)
            rel = os.path.relpath(full, srcdir).replace(os.sep, '/')
            files.append(rel)
    files.sort()
    if order:  # preserve original ordering if given (list of names)
        files.sort(key=lambda f: order.index(f) if f in order else 10**9)
    entries = []
    with open(out_pak, 'wb') as o:
        o.write(b'PACK')
        o.write(b'\0' * (DATA_START - 4))  # placeholder header + padding
        pos = DATA_START
        for rel in files:
            if len(rel.encode()) > NAME_LEN - 1:
                raise ValueError(f"name too long: {rel}")
            with open(os.path.join(srcdir, rel), 'rb') as f:
                data = f.read()
            o.seek(pos)
            o.write(data)
            entries.append((rel, pos, len(data)))
            pos += len(data)
            pad = (-pos) % ALIGN
            o.write(b'\0' * pad)
            pos += pad
        # directory goes right after the LAST file's data, unaligned (matches shipped paks)
        dir_off = entries[-1][1] + entries[-1][2] if entries else DATA_START
        o.seek(dir_off)
        o.truncate()
        o.seek(dir_off)
        for rel, off, size in entries:
            rec = rel.encode().ljust(NAME_LEN, b'\0') + struct.pack('<II', off, size)
            o.write(rec)
        o.seek(4)
        o.write(struct.pack('<II', dir_off, len(entries) * RECORD))
    print(f"packed {len(entries)} files -> {out_pak} (dir at {dir_off:#x})")

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("usage: pak_pack.py <srcdir> <out.pak>"); sys.exit(1)
    build(sys.argv[1], sys.argv[2])
