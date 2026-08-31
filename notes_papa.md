# Travailler sur NautiBook

## Ouvrir VSCode

VSCode s'ouvre automatiquement avec un terminal ouvert. Dans ce terminal, il est important que le prompt (ligne avec ton nom d'utilisateur) commence par la mention "(.venv)"
Si ce n'est pas le cas, il faut taper la commande suivante:
`source .venv/bin/activate`

Tu as alors activé l'environnement qui te permet de faire tourner le serveur.

Pour lancer le serveur, il suffit de taper la commande `./run.sh` dans le terminal.
Si il y a une erreur, tu la verras dans ce terminal.

Pour ouvrir la version virtuelle: http://localhost:8000

Pour **relancer** le serveur, il faut d'abord l'arreter avec <CTRL-C>, et retaper la commande `./run.sh`
Normalement il n'y a pas besoin de redémarrer le serveur, il integre les modifications a la volée.

# Le Raspberry Pi du bord

Le Pi fait tourner NautiBook en permanence via `nautibook.service`, sur la branche
`production`. Contrairement au serveur de test, il n'a pas `--reload` : il faut le
redémarrer pour qu'il prenne un changement.

## Mise à jour automatique

`nautibook-update.timer` regarde toutes les 5 minutes si la branche `production` a
bougé sur GitHub. Si oui, `deploy.sh` récupère le changement et redémarre le
service ; sinon il ne fait rien. Donc **pousser sur `production` suffit** : dans
les cinq minutes qui suivent, le bateau est à jour.

Sans réseau (au large), le contrôle échoue sans bruit et reprend au retour de la
couverture. Si un changement empêche l'app de démarrer, `deploy.sh` revient tout
seul à la version précédente et ne réessaie pas ce commit-là — le bord n'est
jamais laissé sans journal.

À installer une seule fois, sur le Pi :

```bash
# Le Pi suit production, et non main
cd /home/pi/NautiBook
git fetch origin && git checkout production

# Autoriser l'utilisateur pi à redémarrer ce seul service, sans mot de passe.
# Les deux chemins sont listés parce que sudo compare le chemin exact, et que
# systemctl est en /usr/bin ou en /bin selon la version du système.
echo 'pi ALL=(root) NOPASSWD: /usr/bin/systemctl restart nautibook, /bin/systemctl restart nautibook' \
    | sudo tee /etc/sudoers.d/nautibook
sudo chmod 440 /etc/sudoers.d/nautibook
sudo visudo -c -f /etc/sudoers.d/nautibook   # doit répondre « parsed OK »

# Installer le timer et sa tâche
sudo cp nautibook-update.service nautibook-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nautibook-update.timer
```

Pour voir ce que ça fait :

```bash
systemctl list-timers nautibook-update.timer   # prochain passage
journalctl -u nautibook-update -n 30           # ce qu'il a trouvé
sudo systemctl start nautibook-update.service  # forcer un contrôle tout de suite
```
