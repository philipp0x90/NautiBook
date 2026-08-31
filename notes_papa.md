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
