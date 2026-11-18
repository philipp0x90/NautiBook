"""Sauvegarde du journal de bord (la base) et des photos (IMG/) sur le disque
externe branché au Pi — un Samsung T7, monté sur ~/Desktop/T7_Pi.

Lancé par le bouton « Sauvegarder maintenant » des Réglages, ou à la main :
    .venv/bin/python backup.py
    GARDER=60 .venv/bin/python backup.py     # garder 60 copies de la base

La base et IMG/ sont gitignorés : GitHub ne les transporte pas, ce script est
le seul filet sous les données du bord. Python seul (bibliothèque standard) :
le Pi n'a pas forcément les commandes sqlite3 ni rsync.

Trois choix délibérés, faciles à défaire par mégarde :
- La base est copiée par l'API de sauvegarde en ligne de SQLite, jamais par une
  copie de fichier : c'est la seule copie sûre pendant que l'app écrit. Chaque
  copie passe integrity_check, et est supprimée si elle échoue.
- Les photos vont dans un miroir unique et cumulatif, sans suppression : les
  noms produits par _save_upload sont horodatés, jamais réutilisés ; une photo
  effacée dans l'app reste dans le miroir, ce qu'il faut pour restaurer une
  base ancienne qui la désigne encore. Seules les copies de la base sont
  élaguées.
- Le disque doit être monté : un dossier T7_Pi vide sur la carte SD (disque
  débranché) passerait sinon pour la destination, et la « sauvegarde »
  remplirait la carte SD à côté des données qu'elle prétend protéger.

Le compte rendu sort sur la sortie standard : les lignes « base : » et
« photos : » sont le résumé que les Réglages affichent, une ligne commençant
par ERREUR le motif d'un échec (code de sortie 1).
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import config

REPO = Path(__file__).resolve().parent
DB = Path(os.environ.get("NAUTIBOOK_DB", "logbook.db"))
if not DB.is_absolute():
    DB = REPO / DB
PHOTOS = REPO / "IMG"
# Le disque, réglable dans config.yaml (backup_dir) ; les sauvegardes vont
# dans un sous-dossier, pour ne pas se mêler au reste du disque.
# Chemin complet : le service tourne sous le compte pi, le disque est monté
# dans le dossier de bmansion.
DISQUE_DEFAUT = "/home/bmansion/Desktop/T7_Pi"
SOUS_DOSSIER = Path("NautiBook") / "Sauvegardes"


def disque() -> Path:
    return Path(config.load_config().get("backup_dir") or DISQUE_DEFAUT).expanduser()


def destination() -> Path:
    return disque() / SOUS_DOSSIER


def erreur(message: str):
    print(f"ERREUR : {message}")
    sys.exit(1)


def main():
    garder = int(os.environ.get("GARDER") or 30)
    print(f"NautiBook — sauvegarde du {datetime.now():%d/%m/%Y à %H:%M}")

    if not DB.is_file():
        erreur(f"{DB} est introuvable — rien à sauvegarder.")

    racine = disque()
    if not racine.is_dir():
        erreur(f"le disque de sauvegarde ({racine}) est introuvable — est-il branché ?")
    # Monté = sur un autre périphérique que le dossier personnel. Un dossier
    # resté vide sur la carte SD est sur le même.
    if racine.resolve().stat().st_dev == Path.home().stat().st_dev:
        erreur(f"le disque de sauvegarde n'est pas monté sur {racine} — est-il branché ?")

    dest = destination()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        erreur(f"impossible de créer {dest} ({exc.strerror}).")

    # ── La base ──
    horodatage = datetime.now().strftime("%Y%m%d-%H%M%S")
    cible = dest / f"logbook.db.backup-{horodatage}"
    n = 1
    while cible.exists():   # deux lancements dans la même seconde
        cible = dest / f"logbook.db.backup-{horodatage}-{n}"
        n += 1
    try:
        with sqlite3.connect(DB) as src, sqlite3.connect(cible) as dst:
            src.backup(dst)
        with sqlite3.connect(cible) as copie:
            etat = copie.execute("PRAGMA integrity_check").fetchone()[0]
            lignes = copie.execute("SELECT count(*) FROM logbook_lines").fetchone()[0]
    except sqlite3.Error as exc:
        cible.unlink(missing_ok=True)
        erreur(f"la sauvegarde de la base a échoué ({exc}).")
    if etat != "ok":
        cible.unlink(missing_ok=True)
        erreur("la copie de la base ne passe pas integrity_check — supprimée.")
    poids = cible.stat().st_size / 1048576
    print(f"base : {cible.name} ({poids:.1f} Mo, {lignes} lignes de journal) — intègre")

    # ── Les photos ──
    if PHOTOS.is_dir():
        miroir = dest / "IMG"
        nouvelles = total = 0
        try:
            for f in PHOTOS.rglob("*"):
                if not f.is_file() or f.name.startswith("."):
                    continue
                total += 1
                copie = miroir / f.relative_to(PHOTOS)
                if copie.exists() and copie.stat().st_size == f.stat().st_size:
                    continue
                copie.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, copie)
                nouvelles += 1
        except OSError as exc:
            erreur(f"la copie des photos a échoué ({exc.strerror} : {exc.filename}).")
        print(f"photos : {nouvelles} nouvelle(s) copiée(s), {total} au total")
    else:
        print("photos : dossier IMG/ absent, ignoré")

    # ── Élagage ── l'horodatage se trie comme la chronologie.
    copies = sorted(dest.glob("logbook.db.backup-*"))
    for vieille in copies[:-garder] if len(copies) > garder else []:
        vieille.unlink()
    print(f"gardé : {min(len(copies), garder)} copie(s) de la base sur {garder}")
    print(f"Terminé — {dest}")


if __name__ == "__main__":
    main()
