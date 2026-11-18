"""Sauvegarde du journal de bord (la base) et des photos (IMG/) sur le disque
externe branché au Pi — un Samsung T7, monté sur /mnt/t7.

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
import re
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
# Le point de montage lui-même (ext4, /mnt/t7), pas le raccourci
# ~bmansion/Desktop/T7_Pi qui y mène : le service tourne sous le compte pi,
# qui ne peut pas entrer dans le dossier de bmansion (drwx------).
DISQUE_DEFAUT = "/mnt/t7"
SOUS_DOSSIER = Path("NautiBook") / "Sauvegardes"


def disque() -> Path:
    # NAUTIBOOK_BACKUP_DIR : un autre disque pour un essai, comme NAUTIBOOK_DB
    # une autre base.
    return Path(os.environ.get("NAUTIBOOK_BACKUP_DIR") or config.load_config().get("backup_dir")
                or DISQUE_DEFAUT).expanduser()


def destination() -> Path:
    return disque() / SOUS_DOSSIER


NOM_COPIE = re.compile(r"logbook\.db\.backup-[0-9A-Za-z-]+")


class Echec(Exception):
    """Une sauvegarde ou une restauration qui s'arrête : le message dit
    pourquoi, en français, pour les Réglages comme pour le terminal."""


def _dossier_pret() -> Path:
    """Le dossier des sauvegardes, sur le disque monté, créé au besoin."""
    racine = disque()
    if not racine.is_dir():
        raise Echec(f"le disque de sauvegarde ({racine}) est introuvable — est-il branché ?")
    # Monté = sur un autre périphérique que le dossier personnel. Un dossier
    # resté vide sur la carte SD est sur le même.
    if racine.resolve().stat().st_dev == Path.home().stat().st_dev:
        raise Echec(f"le disque de sauvegarde n'est pas monté sur {racine} — est-il branché ?")
    dest = destination()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Echec(f"impossible de créer {dest} ({exc.strerror}).")
    return dest


def _copier_base(dest: Path, suffixe: str = "") -> str:
    """Copie la base en service dans dest, vérifiée ; renvoie la ligne
    « base : … » du compte rendu."""
    if not DB.is_file():
        raise Echec(f"{DB} est introuvable — rien à sauvegarder.")
    horodatage = datetime.now().strftime("%Y%m%d-%H%M%S") + suffixe
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
        raise Echec(f"la sauvegarde de la base a échoué ({exc}).")
    if etat != "ok":
        cible.unlink(missing_ok=True)
        raise Echec("la copie de la base ne passe pas integrity_check — supprimée.")
    poids = cible.stat().st_size / 1048576
    return f"base : {cible.name} ({poids:.1f} Mo, {lignes} lignes de journal) — intègre"


def _copier_fichiers(source: Path, cible: Path) -> tuple:
    """Copie de source vers cible les fichiers absents (ou d'une autre
    taille) ; ne supprime rien. Renvoie (copiés, total)."""
    copies = total = 0
    try:
        for f in source.rglob("*"):
            if not f.is_file() or f.name.startswith("."):
                continue
            total += 1
            dst = cible / f.relative_to(source)
            if dst.exists() and dst.stat().st_size == f.stat().st_size:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            copies += 1
    except OSError as exc:
        raise Echec(f"la copie des photos a échoué ({exc.strerror} : {exc.filename}).")
    return copies, total


def sauvegarder(garder: int = 30) -> list:
    """Une sauvegarde complète ; renvoie les lignes du compte rendu."""
    dest = _dossier_pret()
    lignes = [_copier_base(dest)]
    if PHOTOS.is_dir():
        nouvelles, total = _copier_fichiers(PHOTOS, dest / "IMG")
        lignes.append(f"photos : {nouvelles} nouvelle(s) copiée(s), {total} au total")
    else:
        lignes.append("photos : dossier IMG/ absent, ignoré")
    # Élagage : l'horodatage se trie comme la chronologie.
    copies = sorted(dest.glob("logbook.db.backup-*"))
    for vieille in copies[:-garder] if len(copies) > garder else []:
        vieille.unlink()
    lignes.append(f"gardé : {min(len(copies), garder)} copie(s) de la base sur {garder}")
    lignes.append(f"Terminé — {dest}")
    return lignes


def _compter(chemin: Path) -> dict:
    """Ce que contient une copie (lue sans y toucher) : navires, croisières,
    routes, lignes de journal ; None pour ce qui ne se lit pas."""
    res = {}
    try:
        with sqlite3.connect(f"file:{chemin}?mode=ro", uri=True) as db:
            for cle, table in (("navires", "ship_info"), ("croisieres", "cruises"),
                               ("routes", "routes"), ("lignes", "logbook_lines")):
                try:
                    res[cle] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    res[cle] = None
    except sqlite3.Error:
        pass
    return res


def liste() -> list:
    """Les copies présentes sur le disque, la plus récente d'abord : nom,
    date, taille en Mo, contenu, et si c'est la copie de sécurité faite
    avant une restauration. Lève Echec si le disque n'est pas là."""
    dest = _dossier_pret()
    copies = []
    for f in sorted(dest.glob("logbook.db.backup-*"), reverse=True):
        m = re.match(r"logbook\.db\.backup-(\d{8})-(\d{6})", f.name)
        quand = datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S") if m else None
        copies.append({"nom": f.name, "date": quand, "mo": f.stat().st_size / 1048576,
                       "avant_restauration": "avant-restauration" in f.name, **_compter(f)})
    return copies


def restaurer(nom: str) -> list:
    """Remet la copie `nom` à la place de la base en service. Avant tout, la
    base actuelle est sauvegardée (suffixe « avant-restauration ») : on peut
    revenir en arrière. Puis les photos du miroir absentes d'IMG/ y sont
    remises. Le schéma d'une copie ancienne est mis à jour ensuite par
    l'app (init_db). Renvoie les lignes du compte rendu."""
    dest = _dossier_pret()
    copie = dest / nom
    if not NOM_COPIE.fullmatch(nom) or not copie.is_file():
        raise Echec("cette copie est introuvable sur le disque.")
    try:
        with sqlite3.connect(f"file:{copie}?mode=ro", uri=True) as db:
            etat = db.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as exc:
        raise Echec(f"la copie choisie ne se lit pas ({exc}).")
    if etat != "ok":
        raise Echec("la copie choisie ne passe pas integrity_check — rien n'a été changé.")
    lignes = ["sécurité : " + _copier_base(dest, "-avant-restauration").split(" : ", 1)[1]]
    try:
        with sqlite3.connect(f"file:{copie}?mode=ro", uri=True) as src, sqlite3.connect(DB) as dst:
            src.backup(dst)
    except sqlite3.Error as exc:
        raise Echec(f"la restauration a échoué ({exc}) — la base d'avant est sur le disque.")
    lignes.append(f"base : {nom} restaurée")
    miroir = dest / "IMG"
    if miroir.is_dir():
        remises, _ = _copier_fichiers(miroir, PHOTOS)
        lignes.append(f"photos : {remises} remise(s) depuis le disque")
    return lignes


def supprimer(nom: str) -> str:
    """Efface une copie de la base du disque ; le miroir des photos n'est
    jamais touché. Renvoie la ligne du compte rendu."""
    dest = _dossier_pret()
    copie = dest / nom
    if not NOM_COPIE.fullmatch(nom) or not copie.is_file():
        raise Echec("cette copie est introuvable sur le disque.")
    try:
        copie.unlink()
    except OSError as exc:
        raise Echec(f"impossible d'effacer {nom} ({exc.strerror}).")
    return f"copie effacée : {nom}"


def main():
    print(f"NautiBook — sauvegarde du {datetime.now():%d/%m/%Y à %H:%M}")
    try:
        for ligne in sauvegarder(int(os.environ.get("GARDER") or 30)):
            print(ligne)
    except Echec as exc:
        print(f"ERREUR : {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
