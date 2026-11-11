# -*- coding: utf-8 -*-
"""
SyncLocal360 - galeria lokalnych kopii w przeglądarce.

Mały serwer HTTP (tylko biblioteka standardowa) dostępny wyłącznie
z tego komputera (127.0.0.1). Pokazuje projekty z folderu kopii
i pozwala przypisywać im kategorie, etykiety, notatki i gwiazdki.

Dane:
  * <projekt>/config.json:
      tag       - kategoria (pole z NoCloud2; "unassigned" = brak kategorii)
      tags      - lista etykiet
      note      - notatka
      favorite  - ulubiony (bool)
  * <folder kopii>/synclocal_gallery.json - lista kategorii (nazwa, kolor, kolejność)

Uruchamianie:
  * w Fusion: przycisk „Galeria” (start_in_fusion); wtedy działa też
    „Otwórz w Fusion” i „Wstaw do aktywnego projektu”,
  * bez Fusion:  python3 synclocal_gallery.py [folder_kopii] [--port 8360] [--no-browser]

Moduł nie importuje adsk na poziomie modułu, żeby działał także poza Fusion.
"""

import os
import sys
import json
import secrets
import threading
import datetime
import subprocess
import webbrowser
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ADDIN_PATH = os.path.dirname(os.path.realpath(__file__))
HTML_PATH = os.path.join(ADDIN_PATH, 'resources', 'gallery', 'index.html')
SETTINGS_PATH = os.path.join(ADDIN_PATH, 'synclocal_settings.json')
CATEGORIES_FILE = 'synclocal_gallery.json'
NO_CATEGORY = 'unassigned'
DEFAULT_PORT = 8360
FUSION_EVENT_ID = 'SyncLocal360_GalleryEvent'

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# DANE
# ---------------------------------------------------------------------------
def _read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def _write_json(path, data):
    """Zapis przez plik tymczasowy, żeby przerwany zapis nie zepsuł config.json."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _project_dir(vault, folder):
    """Bezpieczna ścieżka do folderu projektu (tylko bezpośrednie podfoldery)."""
    if not isinstance(folder, str) or not folder or folder in ('.', '..') \
            or folder != os.path.basename(folder) or '/' in folder or '\\' in folder:
        raise ValueError('Nieprawidłowa nazwa folderu.')
    path = os.path.join(vault, folder)
    if not os.path.isfile(os.path.join(path, 'config.json')):
        raise ValueError(f'Nie ma projektu „{folder}”.')
    return path


def _category_of(meta):
    tag = str(meta.get('tag') or '').strip()
    return '' if tag == NO_CATEGORY else tag


def _clean_tags(tags):
    out = []
    for t in tags or []:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return out


def _file_info(path):
    try:
        st = os.stat(path)
        return {'size': st.st_size, 'mtime': int(st.st_mtime)}
    except OSError:
        return None


def _project_item(vault, folder, meta):
    path = os.path.join(vault, folder)
    files = {}
    for kind, name in (('f3d', meta.get('file') or f'{folder}.f3d'),
                       ('step', meta.get('step_file') or f'{folder}.step'),
                       ('png', meta.get('thumbnail') or f'{folder}.png')):
        info = _file_info(os.path.join(path, os.path.basename(name)))
        if info:
            files[kind] = dict(info, name=os.path.basename(name))
    versions_dir = os.path.join(path, 'versions')
    versions = 0
    if os.path.isdir(versions_dir):
        versions = sum(1 for n in os.listdir(versions_dir) if n.lower().endswith('.f3d'))
    return {
        'folder': folder,
        'name': meta.get('displayname') or folder,
        'category': _category_of(meta),
        'tags': _clean_tags(meta.get('tags')),
        'note': str(meta.get('note') or ''),
        'favorite': bool(meta.get('favorite')),
        'cloud_project': meta.get('cloud_project') or '',
        'cloud_folder': meta.get('cloud_folder') or '',
        'version': meta.get('cloud_version') or meta.get('version') or '',
        'last_saved': meta.get('last_saved') or '',
        'reason': meta.get('last_save_reason') or '',
        'versions': versions,
        'files': files,
    }


def scan_projects(vault):
    items = []
    try:
        entries = sorted(os.listdir(vault), key=str.lower)
    except OSError:
        return items
    for entry in entries:
        config_path = os.path.join(vault, entry, 'config.json')
        if not os.path.isfile(config_path):
            continue
        meta = _read_json(config_path, default=None)
        if not isinstance(meta, dict):
            continue
        items.append(_project_item(vault, entry, meta))
    return items


def load_categories(vault):
    data = _read_json(os.path.join(vault, CATEGORIES_FILE))
    result = []
    for c in data.get('categories') or []:
        name = str(c.get('name') or '').strip() if isinstance(c, dict) else ''
        if name and name != NO_CATEGORY and all(r['name'] != name for r in result):
            result.append({'name': name, 'color': str(c.get('color') or '')})
    return result


def save_categories(vault, categories):
    clean = []
    for c in categories or []:
        name = str(c.get('name') or '').strip()
        if name and name != NO_CATEGORY and all(r['name'] != name for r in clean):
            clean.append({'name': name, 'color': str(c.get('color') or '')})
    path = os.path.join(vault, CATEGORIES_FILE)
    data = _read_json(path)
    data['categories'] = clean
    data['updated'] = datetime.datetime.now().isoformat(timespec='seconds')
    _write_json(path, data)
    return clean


def update_projects(vault, folders, changes):
    """changes: category, tags, note, favorite (podmiana) oraz add_tags, remove_tags."""
    if not isinstance(folders, list):
        raise ValueError('Nieprawidłowa lista projektów.')
    paths = [_project_dir(vault, folder) for folder in folders]   # najpierw sprawdź wszystkie
    updated = []
    for folder, path in zip(folders, paths):
        config_path = os.path.join(path, 'config.json')
        meta = _read_json(config_path, default=None)
        if not isinstance(meta, dict):
            raise ValueError(f'Nie da się odczytać config.json w „{folder}”.')
        if 'category' in changes:
            meta['tag'] = str(changes['category'] or '').strip() or NO_CATEGORY
        if 'tags' in changes:
            meta['tags'] = _clean_tags(changes['tags'])
        if changes.get('add_tags') or changes.get('remove_tags'):
            remove = set(_clean_tags(changes.get('remove_tags')))
            tags = [t for t in _clean_tags(meta.get('tags')) if t not in remove]
            meta['tags'] = _clean_tags(tags + _clean_tags(changes.get('add_tags')))
        if 'note' in changes:
            meta['note'] = str(changes['note'] or '')
        if 'favorite' in changes:
            meta['favorite'] = bool(changes['favorite'])
        _write_json(config_path, meta)
        updated.append(_project_item(vault, folder, meta))
    return updated


def rename_category(vault, old, new):
    """Zmienia nazwę kategorii we wszystkich projektach; new == '' usuwa kategorię."""
    old = str(old or '').strip()
    new = str(new or '').strip()
    if not old:
        raise ValueError('Brak nazwy kategorii.')
    changed = 0
    for entry in os.listdir(vault):
        config_path = os.path.join(vault, entry, 'config.json')
        if not os.path.isfile(config_path):
            continue
        meta = _read_json(config_path, default=None)
        if isinstance(meta, dict) and _category_of(meta) == old:
            meta['tag'] = new or NO_CATEGORY
            _write_json(config_path, meta)
            changed += 1
    cats = load_categories(vault)
    if new and any(c['name'] == new for c in cats):
        cats = [c for c in cats if c['name'] != old]          # scalenie z istniejącą
    else:
        for c in cats:
            if c['name'] == old:
                c['name'] = new
        cats = [c for c in cats if c['name']]
    save_categories(vault, cats)
    return changed


def open_project_file(vault, folder, name):
    """Otwiera plik projektu (.step, .f3d) w programie domyślnym systemu."""
    name = str(name or '')
    if not name or name != os.path.basename(name) or name in ('.', '..'):
        raise ValueError('Nieprawidłowa nazwa pliku.')
    path = os.path.join(_project_dir(vault, folder), name)
    if not os.path.isfile(path):
        raise ValueError('Plik nie istnieje.')
    _open_path(path)


def open_in_file_manager(vault, folder):
    _open_path(_project_dir(vault, folder))


def _open_path(path):
    if sys.platform.startswith('win'):
        os.startfile(path)  # noqa - tylko Windows
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


# ---------------------------------------------------------------------------
# SERWER
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = 'SyncLocal360'

    # konfiguracja ustawiana w _GalleryServer
    get_vault = None
    token = ''
    fusion_action = None   # callable(action, folder) albo None

    def log_message(self, fmt, *args):   # bez spamowania konsoli / Fusion
        pass

    # --- pomocnicze ---
    def _host_ok(self):
        # Ochrona przed DNS rebinding: tylko adresy lokalne.
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]').lower()
        return host in ('127.0.0.1', 'localhost', '::1')

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _error(self, code, msg):
        self._send(code, {'ok': False, 'error': msg})

    # --- GET ---
    def do_GET(self):
        if not self._host_ok():
            return self._error(403, 'Niedozwolony host.')
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        try:
            if path in ('/', '/index.html'):
                with open(HTML_PATH, 'r', encoding='utf-8') as f:
                    html = f.read().replace('__SYNCLOCAL_TOKEN__', self.token)
                return self._send(200, html, 'text/html; charset=utf-8',
                                  {'Cache-Control': 'no-store'})
            if path == '/api/state':
                vault = self.get_vault()
                return self._send(200, {
                    'ok': True,
                    'vault': vault,
                    'fusion': self.fusion_action is not None,
                    'categories': load_categories(vault),
                    'projects': scan_projects(vault),
                }, extra={'Cache-Control': 'no-store'})
            if path.startswith('/files/'):
                return self._serve_file(path[len('/files/'):])
            return self._error(404, 'Nie ma takiej strony.')
        except Exception as e:
            return self._error(500, str(e))

    def _serve_file(self, rel):
        parts = rel.split('/')
        if len(parts) != 2:
            return self._error(404, 'Nie ma takiego pliku.')
        vault = self.get_vault()
        try:
            folder_path = _project_dir(vault, parts[0])
        except ValueError as e:
            return self._error(404, str(e))
        name = parts[1]
        if name != os.path.basename(name) or name in ('', '.', '..'):
            return self._error(404, 'Nie ma takiego pliku.')
        file_path = os.path.join(folder_path, name)
        if not os.path.isfile(file_path):
            return self._error(404, 'Nie ma takiego pliku.')
        ext = os.path.splitext(name)[1].lower()
        ctype = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}.get(ext)
        extra = {'Cache-Control': 'max-age=3600'}
        if not ctype:
            ctype = 'application/octet-stream'
            extra['Content-Disposition'] = "attachment; filename*=UTF-8''" + urllib.parse.quote(name)
        with open(file_path, 'rb') as f:
            data = f.read()
        return self._send(200, data, ctype, extra)

    # --- POST ---
    def do_POST(self):
        if not self._host_ok():
            return self._error(403, 'Niedozwolony host.')
        # Własny nagłówek z tokenem: inne strony w przeglądarce nie mogą go wysłać.
        if not secrets.compare_digest(self.headers.get('X-SyncLocal-Token', ''), self.token):
            return self._error(403, 'Brak tokenu - odśwież stronę galerii.')
        try:
            length = int(self.headers.get('Content-Length') or 0)
            data = json.loads(self.rfile.read(length) or b'{}') if length else {}
        except Exception:
            return self._error(400, 'Nieprawidłowe dane.')
        path = urllib.parse.urlsplit(self.path).path
        try:
            vault = self.get_vault()
            if path == '/api/projects':
                folders = data.get('folders') or []
                changes = {k: data[k] for k in ('category', 'tags', 'note', 'favorite',
                                                 'add_tags', 'remove_tags') if k in data}
                cat = str(changes.get('category') or '').strip()
                with _write_lock:
                    items = update_projects(vault, folders, changes)
                    if cat and all(c['name'] != cat for c in load_categories(vault)):
                        save_categories(vault, load_categories(vault) + [{'name': cat, 'color': ''}])
                return self._send(200, {'ok': True, 'projects': items,
                                        'categories': load_categories(vault)})
            if path == '/api/categories':
                with _write_lock:
                    cats = save_categories(vault, data.get('categories'))
                return self._send(200, {'ok': True, 'categories': cats})
            if path == '/api/categories/rename':
                with _write_lock:
                    n = rename_category(vault, data.get('old'), data.get('new'))
                return self._send(200, {'ok': True, 'changed': n,
                                        'categories': load_categories(vault)})
            if path == '/api/open-folder':
                open_in_file_manager(vault, data.get('folder'))
                return self._send(200, {'ok': True})
            if path == '/api/open-file':
                open_project_file(vault, data.get('folder'), data.get('file'))
                return self._send(200, {'ok': True})
            if path == '/api/fusion':
                if self.fusion_action is None:
                    return self._error(400, 'Galeria nie jest uruchomiona z Fusion.')
                action = data.get('action')
                if action not in ('open', 'insert'):
                    return self._error(400, 'Nieznana akcja.')
                _project_dir(vault, data.get('folder'))
                self.fusion_action(action, data.get('folder'))
                return self._send(200, {'ok': True})
            return self._error(404, 'Nie ma takiego adresu.')
        except ValueError as e:
            return self._error(400, str(e))
        except Exception as e:
            return self._error(500, str(e))


class _GalleryServer:
    def __init__(self, get_vault, fusion_action=None, port=DEFAULT_PORT):
        handler = type('GalleryHandler', (_Handler,), {
            'get_vault': staticmethod(get_vault),
            'token': secrets.token_urlsafe(24),
            'fusion_action': staticmethod(fusion_action) if fusion_action else None,
        })
        try:
            self.httpd = ThreadingHTTPServer(('127.0.0.1', port), handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)   # port zajęty
        self.httpd.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/'
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name='SyncLocal360Gallery', daemon=True)
        self.thread.start()

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# INTEGRACJA Z FUSION
# ---------------------------------------------------------------------------
_server = None
_fusion_event = None
_fusion_handlers = []


def _fusion_do(action, folder):
    """Wywoływane w wątku głównym Fusion (custom event)."""
    import adsk.core
    import synclocal_core
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        if action == 'insert':
            ok, msg = synclocal_core.import_from_vault(folder)
        else:
            ok, msg = synclocal_core.open_from_vault(folder)
        if not ok:
            ui.messageBox(f'SyncLocal360 - galeria:\n{msg}')
    except Exception as e:
        ui.messageBox(f'SyncLocal360 - galeria:\n{e}')


def start_in_fusion():
    """Uruchamia serwer (jeśli jeszcze nie działa) i otwiera galerię w przeglądarce."""
    global _server, _fusion_event
    import adsk.core
    import synclocal_core
    app = adsk.core.Application.get()

    if _fusion_event is None:
        class _GalleryEventHandler(adsk.core.CustomEventHandler):
            def notify(self, args):
                try:
                    info = json.loads(args.additionalInfo or '{}')
                    _fusion_do(info.get('action'), info.get('folder'))
                except Exception:
                    pass
        try:
            app.unregisterCustomEvent(FUSION_EVENT_ID)
        except Exception:
            pass
        _fusion_event = app.registerCustomEvent(FUSION_EVENT_ID)
        handler = _GalleryEventHandler()
        _fusion_event.add(handler)
        _fusion_handlers.append(handler)

    def fire(action, folder):
        # Wątek serwera nie może dotykać API Fusion - przekazujemy zadanie do wątku głównego.
        app.fireCustomEvent(FUSION_EVENT_ID, json.dumps({'action': action, 'folder': folder}))

    if _server is None:
        _server = _GalleryServer(synclocal_core.get_vault_dir, fusion_action=fire)
        _server.start()
    webbrowser.open(_server.url)
    return _server.url


def stop():
    global _server, _fusion_event
    if _server is not None:
        _server.stop()
        _server = None
    if _fusion_event is not None:
        try:
            import adsk.core
            app = adsk.core.Application.get()
            for h in _fusion_handlers:
                _fusion_event.remove(h)
            app.unregisterCustomEvent(FUSION_EVENT_ID)
        except Exception:
            pass
        _fusion_event = None
        _fusion_handlers.clear()


# ---------------------------------------------------------------------------
# URUCHOMIENIE BEZ FUSION
# ---------------------------------------------------------------------------
def main(argv):
    import argparse
    parser = argparse.ArgumentParser(description='SyncLocal360 - galeria lokalnych kopii')
    parser.add_argument('vault', nargs='?', help='folder kopii (domyślnie z synclocal_settings.json)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)

    vault = args.vault or _read_json(SETTINGS_PATH).get('vault_dir') or ''
    vault = os.path.abspath(os.path.expanduser(vault))
    if not os.path.isdir(vault):
        print(f'Nie ma folderu kopii: {vault}')
        return 1
    server = _GalleryServer(lambda: vault, port=args.port)
    print(f'Galeria SyncLocal360: {server.url}  (folder: {vault})')
    print('Zatrzymanie: Ctrl+C')
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(server.url,)).start()
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
