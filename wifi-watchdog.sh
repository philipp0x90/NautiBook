#!/bin/bash
# Chien de garde du wifi du Pi. Lancé toutes les deux minutes par
# wifi-watchdog.timer, en root (nmcli et reboot l'exigent).
#
# Le Pi joint-il le routeur du bord ? (la passerelle par défaut, à défaut
# l'iKommunicate n'étant pas toujours sur la même adresse). Oui : on remet le
# compteur d'échecs à zéro. Non : on relance le wifi ; au 3e échec de suite
# (6 minutes sans réseau), on redémarre le Pi.
#
# Garde-fous contre une boucle de redémarrages — routeur éteint au port, bateau
# hors de portée de tout wifi connu : pas de redémarrage dans la première heure
# après un démarrage, ni plus d'un toutes les six heures. Entre deux, le script
# se contente de relancer le wifi.
set -u

ETAT=/run/wifi-watchdog.echecs          # effacé à chaque démarrage : c'est voulu
DERNIER_REBOOT=/var/lib/wifi-watchdog.reboot
INTERFACE=wlan0

passerelle=$(ip route show default dev "$INTERFACE" 2>/dev/null | awk '/default/ {print $3; exit}')

# Joint : il répond au ping, ou — certains routeurs ignorent le ping — il a
# répondu à la requête ARP que ce ping vient de provoquer (voisin REACHABLE).
if [ -n "$passerelle" ]; then
    if ping -c 2 -W 3 "$passerelle" >/dev/null 2>&1 \
       || ip neigh show "$passerelle" dev "$INTERFACE" 2>/dev/null | grep -q REACHABLE; then
        rm -f "$ETAT"
        exit 0
    fi
fi

echecs=$(( $(cat "$ETAT" 2>/dev/null || echo 0) + 1 ))
echo "$echecs" > "$ETAT"
echo "Routeur injoignable (${passerelle:-pas de route par défaut}) — échec n° $echecs"

if [ "$echecs" -lt 3 ]; then
    echo "Relance du wifi"
    nmcli radio wifi off; sleep 3; nmcli radio wifi on
    exit 0
fi

uptime_s=$(cut -d. -f1 /proc/uptime)
maintenant=$(date +%s)
dernier=$(cat "$DERNIER_REBOOT" 2>/dev/null || echo 0)
if [ "$uptime_s" -lt 3600 ] || [ $(( maintenant - dernier )) -lt 21600 ]; then
    echo "Redémarrage retenu (démarré il y a ${uptime_s} s, dernier redémarrage du chien de garde il y a $(( maintenant - dernier )) s) — relance du wifi"
    nmcli radio wifi off; sleep 3; nmcli radio wifi on
    exit 0
fi

echo "Toujours injoignable après $echecs essais : redémarrage du Pi"
echo "$maintenant" > "$DERNIER_REBOOT"
systemctl reboot
