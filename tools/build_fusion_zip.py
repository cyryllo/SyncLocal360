# -*- coding: utf-8 -*-
"""Buduje paczkę dodatku dla Fusion: dist/SyncLocal360_<wersja>.zip

W środku jest folder SyncLocal360/ (Fusion wymaga, żeby nazwa folderu była taka
sama jak id w manifeście). Trafiają tam tylko pliki potrzebne w Fusion: bez
linux/ (aplikacja Tauri), tools/, .vscode/, CLAUDE.md i __pycache__.

synclocal_settings.json jest celowo pomijany: po rozpakowaniu na istniejący
dodatek zostaną ustawienia z Windowsa, a w nowej instalacji dodatek użyje domyślnych.

Użycie:
    python3 tools/build_fusion_zip.py
"""
import os
import sys
import json
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDIN_ID = 'SyncLocal360'

# Pliki i katalogi z głównego folderu, które wchodzą do paczki.
INCLUDE_FILES = ['SyncLocal360.manifest', 'NOTICE.md', 'README.md']
INCLUDE_GLOBS_PY = True          # wszystkie *.py z głównego folderu
INCLUDE_DIRS = ['resources']
SKIP_NAMES = {'__pycache__', '.DS_Store', 'Thumbs.db'}
SKIP_EXT = {'.pyc', '.tmp'}


def collect():
    files = [f for f in INCLUDE_FILES if os.path.isfile(os.path.join(ROOT, f))]
    if INCLUDE_GLOBS_PY:
        files += sorted(f for f in os.listdir(ROOT)
                        if f.endswith('.py') and os.path.isfile(os.path.join(ROOT, f)))
    for d in INCLUDE_DIRS:
        for base, dirs, names in os.walk(os.path.join(ROOT, d)):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_NAMES)
            for n in sorted(names):
                if n in SKIP_NAMES or os.path.splitext(n)[1] in SKIP_EXT:
                    continue
                files.append(os.path.relpath(os.path.join(base, n), ROOT))
    return files


def main():
    with open(os.path.join(ROOT, f'{ADDIN_ID}.manifest'), encoding='utf-8') as f:
        version = json.load(f).get('version', '0')

    missing = [f for f in ('SyncLocal360.py', 'resources/gallery/index.html')
               if not os.path.isfile(os.path.join(ROOT, f))]
    if missing:
        print('Brak plików:', ', '.join(missing))
        return 1

    files = collect()
    out_dir = os.path.join(ROOT, 'dist')
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f'{ADDIN_ID}_{version}.zip')
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(os.path.join(ROOT, rel), f'{ADDIN_ID}/' + rel.replace(os.sep, '/'))

    size = os.path.getsize(out)
    print(f'Zbudowano: {out}  ({len(files)} plików, {size / 1024:.0f} KB)')
    for rel in files:
        print('  ', rel.replace(os.sep, '/'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
