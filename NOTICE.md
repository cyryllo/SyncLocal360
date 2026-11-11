# SyncLocal360 – autorzy i licencja

## Autorzy

- **Cyryl Sochacki** – SyncLocal360: kopia z chmury, przywracanie do chmury, galeria,
  aplikacja na Linuksa.
- **Kamil Sokalski** – autor oryginalnego dodatku **NoCloud2** (https://modelowanie3d.org),
  na którym SyncLocal360 jest oparty.

## Pochodzenie kodu

SyncLocal360 powstał jako fork dodatku NoCloud2, udostępnionego przez Kamila Sokalskiego
na licencji otwartej z wymogiem uznania autorstwa. Zgodnie z tym wymogiem informacja
o autorze oryginału pozostaje w nagłówkach plików, w manifeście dodatku, w README,
na stronie projektu i w samej galerii.

Z NoCloud2 pochodzą (z modyfikacjami):

- `synclocal_core.py` – zapis aktywnego projektu do lokalnego folderu
  (`save_current_document_to_vault`), ustawienia (`load_settings`, `save_settings`,
  `get_vault_dir`), identyfikator projektu (`get_or_create_doc_id`), `sanitize_filename`,
  `resolve_unique_folder`, `import_from_vault` oraz mechanizm autozapisu,
- `SyncLocal360.py` – przyciski i okna „Szybki Zapis” oraz „Ustawienia”, dodawanie przycisków
  do panelu,
- układ folderu kopii i format `config.json` (pola `folder`, `doc_id`, `displayname`, `file`,
  `thumbnail`, `version`, `tag` …), grupa atrybutu `NoCloud`/`doc_id`,
- ikony przycisków „Szybki Zapis” i „Ustawienia”.

Nowe w SyncLocal360:

- **Kopia z chmury** (`synclocal_backup.py`) – wsadowy zapis projektów i plików z chmury Fusion,
  eksport do STEP, miniatury z chmury, wznawianie, log na bieżąco,
- **Przywróć do chmury** (`synclocal_restore.py`) – wgrywanie kopii do chmury z odtworzeniem
  projektów i folderów,
- **Galeria** (`synclocal_gallery.py`, `resources/gallery/`) – kategorie, etykiety, notatki,
  otwieranie plików; napisana od nowa (nie jest to galeria z NoCloud2),
- **Aplikacja na Linuksa** (`linux/`) – samodzielna galeria jako AppImage,
- własny panel na pasku narzędzi, naprawiony autozapis, narzędzia w `tools/`.

Usunięte z oryginału: galeria i eksporter R2P (zamknięta aplikacja `nocloud-app.exe`),
`migrator.py`, `ready2print.py`, mechanizm plików `.flag`, nieużywane funkcje odczytu
folderu (`scan_vault`, `list_versions`) i konfiguracja edytora `.vscode/` z oryginału.

## Znaki towarowe

SyncLocal360 nie jest związany z Autodesk, Inc. „Autodesk Fusion” i „Fusion 360”
są znakami towarowymi Autodesk, Inc.
