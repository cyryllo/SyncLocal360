# -*- coding: utf-8 -*-
"""Generuje ikony przycisków (chmura ze strzałką) bez zewnętrznych bibliotek.

Użycie:
    python3 tools/make_icons.py resources/icons/backup        # strzałka w dół
    python3 tools/make_icons.py resources/icons/restore --up  # strzałka w górę
    python3 tools/make_icons.py resources/icons/gallery --grid # siatka miniatur (galeria)
"""
import zlib, struct, sys, os
def png(path, w, h, px):
    raw = b''.join(b'\x00' + bytes(sum((px[y*w+x] for x in range(w)), ())) for y in range(h))
    def chunk(t, d): return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t+d) & 0xffffffff)
    open(path,'wb').write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))
def inside_cloud(u, v):
    circles = [(0.33,0.52,0.17),(0.52,0.40,0.22),(0.72,0.55,0.15)]
    if any((u-cx)**2+(v-cy)**2 <= r*r for cx,cy,r in circles): return True
    return 0.18 <= u <= 0.86 and 0.52 <= v <= 0.70 and not (u < 0.18 or u > 0.86)
UP = "--up" in sys.argv
GRID = "--grid" in sys.argv

def inside_grid(u, v):
    """Cztery zaokrąglone kwadraty 2x2."""
    for x0 in (0.08, 0.54):
        for y0 in (0.08, 0.54):
            x1, y1, r = x0 + 0.38, y0 + 0.38, 0.08
            if x0 <= u <= x1 and y0 <= v <= y1:
                cx = min(max(u, x0 + r), x1 - r); cy = min(max(v, y0 + r), y1 - r)
                if (u - cx) ** 2 + (v - cy) ** 2 <= r * r:
                    return True
    return False

def inside_arrow(u, v):
    if UP:
        v = 1.24 - v
    if 0.455 <= u <= 0.545 and 0.36 <= v <= 0.72: return True
    return 0.62 <= v <= 0.88 and abs(u-0.5) <= (0.88 - v) * 0.85
def render(n):
    ss = 4; out = []
    cloud = (44, 123, 229); arrow = (255, 255, 255)
    for y in range(n):
        for x in range(n):
            c = a = 0
            for sy in range(ss):
                for sx in range(ss):
                    u = (x + (sx+.5)/ss)/n; v = (y + (sy+.5)/ss)/n
                    if GRID:
                        if inside_grid(u, v): c += 1
                    elif inside_arrow(u, v): a += 1
                    elif inside_cloud(u, v): c += 1
            tot = ss*ss; cov = (c+a)/tot
            if cov == 0: out.append((0,0,0,0)); continue
            col = tuple(round((cloud[i]*c + arrow[i]*a)/(c+a)) for i in range(3))
            out.append(col + (round(255*cov),))
    return out
d = sys.argv[1]; os.makedirs(d, exist_ok=True)
for n in (16, 32): png(os.path.join(d, f'{n}x{n}.png'), n, n, render(n))
