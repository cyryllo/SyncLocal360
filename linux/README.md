# SyncLocal360 Galeria – aplikacja na Linuksa

Samodzielna aplikacja (AppImage) do przeglądania i porządkowania lokalnych kopii
projektów zrobionych dodatkiem SyncLocal360. Nie potrzebuje Fusion.

- wybór folderu z kopiami (pamięta ostatnio używane),
- kategorie, etykiety, notatki, ulubione – zapisywane w tych samych plikach co galeria
  w Fusion (`config.json` projektu, `synclocal_gallery.json`), więc obie strony widzą to samo
  przez Nextcloud,
- otwieranie `.step` / `.f3d` w wybranych programach (np. FreeCAD) albo w programie
  domyślnym systemu; podwójne kliknięcie miniatury otwiera `.step` w pierwszym pasującym programie.

Interfejs to ten sam plik co w dodatku: `../resources/gallery/index.html`.

Autor: Cyryl Sochacki. Projekt SyncLocal360 jest oparty na dodatku NoCloud2 autorstwa
Kamila Sokalskiego (https://modelowanie3d.org) – zob. [NOTICE.md](../NOTICE.md).
Kod aplikacji: `src-tauri/src/lib.rs` (Tauri 2, Rust).

## Budowanie

Jednorazowo (biblioteki systemowe, Debian/Ubuntu):

```
sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev \
  libayatana-appindicator3-dev patchelf build-essential file
```

Potem w tym katalogu:

```
npm install            # narzędzie tauri (raz)
npm run dev            # uruchomienie na próbę, bez pakowania
npm run build          # AppImage
```

Gotowy plik: `src-tauri/target/release/bundle/appimage/SyncLocal360 Galeria_1.0.0_amd64.AppImage`
(w Releases na GitHubie ten sam plik nazywa się `SyncLocal360-Galeria-1.0.0-x86_64.AppImage`).
Pierwsze budowanie trwa kilka minut (kompilacja zależności); kolejne są szybkie.

Ikony aplikacji: `python3 tools/make_app_icon.py` (czysty Python, bez PIL).

## Uruchamianie

```
chmod +x "SyncLocal360-Galeria-1.0.0-x86_64.AppImage"
./"SyncLocal360-Galeria-1.0.0-x86_64.AppImage"                 # folder z ustawień
./"SyncLocal360-Galeria-1.0.0-x86_64.AppImage" ~/Nextcloud/NoCloud   # konkretny folder
```

Ustawienia aplikacji: `~/.config/org.synclocal360.galeria/settings.json`
(folder, ostatnie foldery, programy do otwierania plików).

## Programy do otwierania plików

Ustawienia (ikona zębatki) → „Programy do otwierania plików”. Każdy program ma:

- nazwę (np. `FreeCAD`),
- polecenie, w którym `%f` to ścieżka pliku: `freecad %f`,
  `flatpak run org.freecad.FreeCAD %f`, `~/Aplikacje/FreeCAD.AppImage %f`,
- rozszerzenia (np. `step, stp`); puste = wszystkie pliki.

Kolumna **Dwuklik** wybiera program, którym projekt otwiera się po dwukliku na miniaturze
(otwierany jest `.step`, a gdy go brak – `.f3d`). Można też wybrać „Program domyślny systemu”.
Jeśli wybrany program nie obsługuje danego pliku, użyty zostanie pierwszy pasujący z listy.
W panelu projektu przycisk programu od dwukliku jest niebieski.

**FreeCAD:** jeśli polecenie zawiera „freecad”, a plik to STEP/IGES/BREP, aplikacja nie podaje
FreeCAD-owi samego pliku, tylko jednorazowe makro (`~/.cache/synclocal360/open_*.py`, usuwa się samo).
Makro otwiera plik, nazywa dokument jak plik i ustawia widok izometryczny z „dopasuj wszystko”.
Bez tego FreeCAD zostawia kamerę w punkcie zero z kadrem ok. 4 mm i modele z Fusion są
niewidoczne albo mocno przybliżone.

Przy pierwszym uruchomieniu aplikacja sama dopisuje FreeCAD, jeśli go znajdzie
(w `PATH` albo jako Flatpak).

Pliki `.f3d` otworzy tylko Fusion – na Linuksie przydają się kopie `.step`
(opcja „Dodatkowo eksportuj do STEP” w kopii z chmury). STEP to sama geometria,
bez historii i parametrów z Fusion.
