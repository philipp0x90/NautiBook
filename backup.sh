#!/bin/bash
# Sauvegarde le journal de bord (logbook.db) et les photos (IMG/) vers iCloud.
# À lancer à la main : ./backup.sh
#
# logbook.db et IMG/ sont gitignorés, donc GitHub ne les transporte pas : ce
# script est le seul filet sous les données du bord. Le code, lui, n'a pas
# besoin d'être sauvegardé ici, il est sur GitHub.
#
# POUR LE MAC UNIQUEMENT : la destination est iCloud Drive, qui n'existe pas
# sur le Raspberry Pi du bord. Le script s'y arrêterait proprement avec une
# erreur, mais il n'a rien à y faire — le Pi demande une autre destination
# (clé USB, ou copie vers le Mac par le réseau du bateau).
#
# Pas de « set -e » mais chaque étape vérifie son résultat : une sauvegarde qui
# échoue à moitié en silence est pire que pas de sauvegarde du tout.
set -u

# Le script marche depuis n'importe quel dossier : il se repère lui-même.
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DB="$REPO/logbook.db"
PHOTOS="$REPO/IMG"

DEST="$HOME/Library/Mobile Documents/com~apple~CloudDocs/NautiBook/Sauvegardes"
# Nombre d'instantanés de la base conservés. Les photos ne sont jamais élaguées
# (voir plus bas), donc ce chiffre ne concerne que les .db. Réglable au
# lancement sans toucher au fichier :  GARDER=30 ./backup.sh
GARDER=${GARDER:-10}

HORODATAGE=$(date +%Y%m%d-%H%M%S)

echo "NautiBook — sauvegarde du $(date '+%d/%m/%Y à %H:%M')"

# ── Contrôles préalables ────────────────────────────────────────────────────

if [ ! -f "$DB" ]; then
    echo "ERREUR : $DB est introuvable — rien à sauvegarder."
    exit 1
fi

# Le dossier iCloud n'existe pas au premier lancement ; en revanche si iCloud
# Drive lui-même est absent, mieux vaut s'arrêter que créer l'arborescence
# ailleurs et croire les données sauvegardées.
if [ ! -d "$HOME/Library/Mobile Documents/com~apple~CloudDocs" ]; then
    echo "ERREUR : iCloud Drive est introuvable sur cette machine."
    exit 1
fi

if ! mkdir -p "$DEST"; then
    echo "ERREUR : impossible de créer $DEST"
    exit 1
fi

# ── La base ─────────────────────────────────────────────────────────────────

# « .backup » et non « cp » : c'est l'API de sauvegarde en ligne de SQLite, la
# seule sûre si l'app tourne pendant la copie. Un cp d'une base ouverte en
# cours d'écriture donne un fichier corrompu, et on ne s'en aperçoit que le
# jour où on veut le restaurer.
CIBLE_DB="$DEST/logbook.db.backup-$HORODATAGE"
# Deux lancements dans la même seconde donneraient le même nom, et le second
# écraserait le premier sans rien dire. On suffixe -1, -2… comme le fait
# _save_photo dans main.py pour les photos homonymes.
n=1
while [ -e "$CIBLE_DB" ]; do
    CIBLE_DB="$DEST/logbook.db.backup-$HORODATAGE-$n"
    n=$((n + 1))
done

if ! sqlite3 "$DB" ".backup '$CIBLE_DB'"; then
    echo "ERREUR : la sauvegarde de la base a échoué."
    exit 1
fi

# Une sauvegarde non vérifiée n'est qu'une supposition.
if [ "$(sqlite3 "$CIBLE_DB" 'pragma integrity_check;')" != "ok" ]; then
    echo "ERREUR : le fichier produit ne passe pas integrity_check — supprimé."
    rm -f "$CIBLE_DB"
    exit 1
fi

LIGNES=$(sqlite3 "$CIBLE_DB" 'select count(*) from logbook_lines;')
POIDS=$(du -h "$CIBLE_DB" | cut -f1)
echo "  base   : $(basename "$CIBLE_DB") ($POIDS, $LIGNES lignes de journal) — intègre"

# ── Les photos ──────────────────────────────────────────────────────────────

# Un miroir unique et cumulatif, pas un instantané par sauvegarde : les noms
# de fichiers produits par _save_photo sont horodatés donc jamais réutilisés,
# et rien dans l'app ne supprime un fichier d'IMG/. Les photos ne changent donc
# jamais, seules de nouvelles s'ajoutent — rsync ne recopie que celles-là.
#
# Volontairement sans « --delete » : le miroir conserve toute photo effacée
# localement, ce qui est justement ce qu'il faut pour restaurer une base
# ancienne dont les chemins pointent vers des fichiers depuis disparus.
compte_photos() { find "$1" -type f ! -name '.*' 2>/dev/null | wc -l | tr -d ' '; }

if [ -d "$PHOTOS" ]; then
    AVANT=$(compte_photos "$DEST/IMG")
    if ! rsync -a "$PHOTOS/" "$DEST/IMG/" > /tmp/nautibook-rsync.log 2>&1; then
        echo "ERREUR : la copie des photos a échoué (voir /tmp/nautibook-rsync.log)"
        exit 1
    fi
    # Le nombre de nouvelles photos est calculé ici plutôt que lu dans la sortie
    # de « rsync --stats » : macOS livre openrsync, qui écrit « Number of files
    # transferred », là où GNU rsync 3 écrit « Number of regular files
    # transferred ». Compter les fichiers ne dépend d'aucune de ces variantes.
    APRES=$(compte_photos "$DEST/IMG")
    echo "  photos : $((APRES - AVANT)) nouvelle(s), $APRES au total dans le miroir ($(du -sh "$DEST/IMG" | cut -f1))"
else
    echo "  photos : dossier IMG/ absent, ignoré"
fi

# ── Élagage ─────────────────────────────────────────────────────────────────

# L'horodatage YYYYMMDD-HHMMSS se trie par ordre alphabétique comme par ordre
# chronologique : le glob suffit, pas besoin de « ls -t » (dont la sortie se
# prête mal aux chemins contenant des espaces, et il y en a dans celui d'iCloud).
shopt -s nullglob
INSTANTANES=("$DEST"/logbook.db.backup-*)
NB=${#INSTANTANES[@]}

if [ "$NB" -gt "$GARDER" ]; then
    for ((i = 0; i < NB - GARDER; i++)); do
        rm -f "${INSTANTANES[i]}"
        echo "  élagué : $(basename "${INSTANTANES[i]}")"
    done
fi

echo "  gardé  : $((NB > GARDER ? GARDER : NB)) instantané(s) de la base sur $GARDER"
echo "Terminé — $DEST"
