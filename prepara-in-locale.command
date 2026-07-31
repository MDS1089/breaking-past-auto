#!/bin/bash
# Doppio clic su questo file dal Finder.
# Popola episodi/ dalle edizioni gia' approvate in 07_Output e genera i JPEG.
# Non tocca nulla di 07_Output: si limita a copiarne il contenuto.

set -e
cd "$(dirname "$0")"
echo "=== Breaking Past — preparazione locale ==="

# Cerca 07_Output risalendo di un paio di livelli.
SRC=""
for c in "../../07_Output" "../07_Output" "../../../07_Output"; do
  [[ -d "$c" ]] && SRC="$c" && break
done
if [[ -z "$SRC" ]]; then
  echo "Non trovo 07_Output. Copia a mano le cartelle Episodio_* dentro episodi/ e rilancia."
  read -n 1 -s -r -p "Premi un tasto per chiudere."; exit 1
fi
echo "Sorgente: $(cd "$SRC" && pwd)"

mkdir -p episodi
rsync -a --exclude '.DS_Store' "$SRC"/Episodio_*/ /dev/null 2>/dev/null || true
for d in "$SRC"/Episodio_*; do
  [[ -d "$d" ]] || continue
  rsync -a --exclude '.DS_Store' "$d" episodi/
done
echo "Copiate $(ls -d episodi/Episodio_* 2>/dev/null | wc -l | tr -d ' ') edizioni."

PY=$(command -v python3 || true)
if [[ -z "$PY" ]]; then
  echo "python3 non trovato. Installa Python 3 da python.org e rilancia."
  read -n 1 -s -r -p "Premi un tasto per chiudere."; exit 1
fi
"$PY" -c "import PIL" 2>/dev/null || "$PY" -m pip install --user Pillow

"$PY" bp.py prepare
"$PY" bp.py status

echo
echo "Fatto. Adesso:  gh repo create breaking-past-auto --public --source . --push"
read -n 1 -s -r -p "Premi un tasto per chiudere."
