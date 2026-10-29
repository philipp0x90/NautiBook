#!/bin/bash
# Rapatrie sur ce Mac la base (logbook.db) et les photos (IMG/) du Pi du bord.
#
#   ./copy_db.sh               remplace logbook.db (l'ancien est mis de côté)
#   ./copy_db.sh pi.db         écrit dans pi.db, sans toucher à logbook.db —
#                              à ouvrir avec  NAUTIBOOK_DB=pi.db uvicorn main:app --port 8001
#   PI=pi@192.168.1.20 ./copy_db.sh    si nautibook.local ne répond pas
#
# Pas de « set -e » mais chaque étape vérifie son résultat, comme backup.sh.
set -u

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PI=${PI:-pi@nautibook.local}
DISTANT=/home/pi/NautiBook
CIBLE="$REPO/${1:-logbook.db}"
case "${1:-}" in /*) CIBLE=$1 ;; esac

HORODATAGE=$(date +%Y%m%d-%H%M%S)
TMP_PI=/tmp/nautibook-copie-$HORODATAGE.db
TMP_MAC=$(mktemp -t nautibook-copie) || exit 1
trap 'rm -f "$TMP_MAC"' EXIT

echo "NautiBook — copie depuis $PI"

# ── La base ─────────────────────────────────────────────────────────────────

# Pas de scp direct de logbook.db : l'app tourne sur le Pi et peut écrire
# pendant la copie, ce qui donne un fichier corrompu (voir backup.sh). On fait
# d'abord une copie cohérente sur le Pi avec l'API de sauvegarde de SQLite —
# par Python, toujours présent sur le Pi, là où la commande sqlite3 peut ne
# pas être installée — puis c'est cette copie qu'on rapatrie.
if ! ssh "$PI" "python3 -c 'import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close()' $DISTANT/logbook.db $TMP_PI"; then
    echo "ERREUR : impossible de copier la base sur le Pi (Pi joignable ?)."
    exit 1
fi

scp -q "$PI:$TMP_PI" "$TMP_MAC"
OK=$?
ssh "$PI" "rm -f $TMP_PI"
if [ $OK -ne 0 ]; then
    echo "ERREUR : le transfert de la base a échoué."
    exit 1
fi

if [ "$(sqlite3 "$TMP_MAC" 'pragma integrity_check;')" != "ok" ]; then
    echo "ERREUR : la copie reçue ne passe pas integrity_check — rien n'a été remplacé."
    exit 1
fi

# L'ancienne base du Mac n'est jamais écrasée sans trace.
if [ -f "$CIBLE" ]; then
    ANCIENNE="$CIBLE.avant-copie-$HORODATAGE"
    if ! mv "$CIBLE" "$ANCIENNE"; then
        echo "ERREUR : impossible de mettre de côté $CIBLE"
        exit 1
    fi
    echo "  ancienne : $(basename "$ANCIENNE")"
fi

if ! mv "$TMP_MAC" "$CIBLE"; then
    echo "ERREUR : impossible d'écrire $CIBLE"
    exit 1
fi

LIGNES=$(sqlite3 "$CIBLE" 'select count(*) from logbook_lines;')
echo "  base     : $(basename "$CIBLE") ($(du -h "$CIBLE" | cut -f1), $LIGNES lignes de journal) — intègre"

# ── Les photos ──────────────────────────────────────────────────────────────

# rsync plutôt que « scp -r » : ne recopie que les nouvelles photos. Sans
# --delete, pour la même raison que backup.sh : une photo absente du Pi reste
# utile à une base plus ancienne.
compte_photos() { find "$1" -type f ! -name '.*' 2>/dev/null | wc -l | tr -d ' '; }
AVANT=$(compte_photos "$REPO/IMG")
if ! rsync -a "$PI:$DISTANT/IMG/" "$REPO/IMG/"; then
    echo "ERREUR : la copie des photos a échoué (la base, elle, est copiée)."
    exit 1
fi
APRES=$(compte_photos "$REPO/IMG")
echo "  photos   : $((APRES - AVANT)) nouvelle(s), $APRES au total"

echo "Terminé."
