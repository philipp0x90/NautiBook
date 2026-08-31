#!/bin/bash
# Met NautiBook à jour depuis la branche production et redémarre le service,
# uniquement si origin/production a bougé. Lancé par nautibook-update.timer,
# jamais à la main (mais rien n'empêche de l'appeler pour forcer un contrôle).
#
# Pas de « set -e » : au large il n'y a pas de réseau, et un fetch qui échoue
# est le cas normal, pas une panne. Chaque étape décide elle-même de sa suite.
set -u

REPO=/home/pi/NautiBook
BRANCH=production
VENV=$REPO/.venv
# Empêche la boucle de rechargement : un commit qui ne démarre pas est noté
# ici, et ignoré aux tours suivants jusqu'à ce qu'un nouveau commit arrive.
FAILED=$REPO/.deploy-failed

cd "$REPO" || exit 1

# stderr jeté : hors couverture, git crache un « fatal: could not read from
# remote » que le message ci-dessous dit mieux, et qui remplirait le journal
# toutes les cinq minutes pendant une traversée.
if ! git fetch --quiet origin "$BRANCH" 2>/dev/null; then
    echo "Pas de réseau — nouvel essai au prochain passage du timer"
    exit 0
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "Déjà à jour sur ${LOCAL:0:7}"
    exit 0
fi

if [ -f "$FAILED" ] && [ "$(cat "$FAILED")" = "$REMOTE" ]; then
    echo "${REMOTE:0:7} a déjà échoué au démarrage — ignoré"
    exit 0
fi

echo "Mise à jour : ${LOCAL:0:7} → ${REMOTE:0:7}"

# --hard, et *jamais* « git clean -x » : logbook.db, config.yaml et le contenu
# de IMG/ sont gitignorés, donc invisibles pour reset — mais -x les effacerait,
# c'est-à-dire tout le journal du bord et toutes les photos.
if ! git reset --hard --quiet "$REMOTE"; then
    echo "Échec du reset, on ne touche pas au service"
    exit 1
fi

# On ne réinstalle que si la liste des dépendances a bougé : pip prend une
# minute sur un Pi, inutile à chaque commit de template.
if ! git diff --quiet "$LOCAL" "$REMOTE" -- requirements.txt; then
    echo "requirements.txt a changé — installation des dépendances"
    "$VENV/bin/pip" install --quiet -r requirements.txt
fi

# Le schéma se met à jour tout seul : init_db() et _migrate() tournent à chaque
# démarrage. Rien à faire ici, mais c'est bien ce redémarrage qui les déclenche.
sudo systemctl restart nautibook

# Filet de sécurité : plutôt que de laisser le bord sans journal, on revient à
# la version qui marchait. Le commit fautif est noté pour ne pas être retenté.
sleep 5
if ! systemctl is-active --quiet nautibook; then
    echo "Le service ne démarre pas — retour à ${LOCAL:0:7}"
    echo "$REMOTE" > "$FAILED"
    git reset --hard --quiet "$LOCAL"
    sudo systemctl restart nautibook
    exit 1
fi

rm -f "$FAILED"
echo "À jour sur ${REMOTE:0:7}"
