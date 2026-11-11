# -*- coding: utf-8 -*-
"""Ikona aplikacji SyncLocal360 Galeria (niebieski kafelek z siatką 2x2), czysty Python.

Użycie:  python3 linux/tools/make_app_icon.py
Zapisuje src-tauri/icons/{32x32,128x128,128x128@2x,icon}.png
"""
import os, zlib, struct

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src-tauri', 'icons')
BG, FG = (44, 123, 229), (255, 255, 255)


def png(path, n, px):
    raw = b''.join(b'\x00' + bytes(sum((px[y * n + x] for x in range(n)), ())) for y in range(n))
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', n, n, 8, 6, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def in_rrect(u, v, x0, y0, x1, y1, r):
    if not (x0 <= u <= x1 and y0 <= v <= y1):
        return False
    cx = min(max(u, x0 + r), x1 - r)
    cy = min(max(v, y0 + r), y1 - r)
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


def shade(u, v):
    if not in_rrect(u, v, 0.04, 0.04, 0.96, 0.96, 0.2):
        return None
    for x0 in (0.22, 0.53):
        for y0 in (0.22, 0.53):
            if in_rrect(u, v, x0, y0, x0 + 0.25, y0 + 0.25, 0.05):
                return FG if (x0 == y0) else (196, 220, 250)
    return BG


def render(n, ss=3):
    out = []
    for y in range(n):
        for x in range(n):
            acc = [0, 0, 0]; cov = 0
            for sy in range(ss):
                for sx in range(ss):
                    c = shade((x + (sx + .5) / ss) / n, (y + (sy + .5) / ss) / n)
                    if c:
                        cov += 1
                        for i in range(3): acc[i] += c[i]
            out.append((0, 0, 0, 0) if not cov else
                       tuple(round(a / cov) for a in acc) + (round(255 * cov / ss / ss),))
    return out


os.makedirs(OUT, exist_ok=True)
for name, n in (('32x32', 32), ('128x128', 128), ('128x128@2x', 256), ('icon', 512)):
    png(os.path.join(OUT, name + '.png'), n, render(n, 3 if n <= 256 else 2))
print('Ikony zapisane w', os.path.normpath(OUT))
