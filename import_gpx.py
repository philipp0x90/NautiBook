"""Import des traces GPX d'une croisière — temporaire.

En attendant que NautiBook enregistre lui-même la trace du bateau, les traces
Navionics (réduites pour Google MyMaps) d'une croisière passées une fois pour
toutes dans track_points, la table que l'enregistrement automatique remplira.
Le jour où il marche, on retire la page d'import, rien d'autre.

Un fichier couvre une croisière, une trace (<trk>) par navigation — ou une
seule navigation, partagée depuis Navionics : plusieurs fichiers s'envoient
alors d'un coup. Les noms
des traces ne disent pas à quelle route elles vont (« Port Gradian », «
track04 ») : proposer() les rattache par la date, et l'utilisateur confirme ou
corrige sur l'écran de vérification.

Ce que les fichiers réels ont montré (2022.gpx, octobre 2026) :
- heures en UTC (« 2022-08-07T11:47:55.075Z ») → heure du bord, celle de la
  machine, comme datetime.now() partout dans l'app ;
- quelques points sans heure : ils prennent celle du point précédent ;
- une trace enregistrée à l'envers (heures décroissantes) : l'ordre du
  fichier est gardé tel quel, c'est celui du dessin ;
- une même trace deux fois, dans un fichier ou d'un fichier à l'autre : la
  seconde est écartée.
"""

import xml.etree.ElementTree as ET
from datetime import datetime


class GpxErreur(Exception):
    """Un fichier illisible, expliqué en français pour la page d'import."""


def _local(tag: str) -> str:
    """Nom d'une balise sans son espace de noms : GPX 1.0 et 1.1 n'ont pas le
    même, et quelques logiciels n'en mettent aucun."""
    return tag.rsplit("}", 1)[-1]


def _heure(texte):
    """« 2022-08-07T11:47:55.075Z » → « 2022-08-07T13:47:55 » à l'heure de la
    machine, au format des points enregistrés par l'app. None si illisible."""
    if not texte:
        return None
    try:
        moment = datetime.fromisoformat(texte.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def lire(fichiers: list) -> list:
    """Les traces de fichiers [(nom, contenu)], dans leur ordre, doublons
    repérés : [{"nom", "fichier", "points": [(lat, lon, heure ou None)],
    "debut", "fin", "doublon_de"}]. debut / fin : plus petite et plus grande
    heure, quel que soit l'ordre ; doublon_de : l'indice de la première."""
    traces, vues = [], {}
    for nom_fichier, data in fichiers:
        avant = len(traces)
        _lire_un(nom_fichier, data, traces, vues)
        if len(traces) == avant:
            raise GpxErreur(f"Aucune trace dans « {nom_fichier} » (un GPX de route prévue, sans trace parcourue ?).")
    return traces


def _lire_un(nom_fichier: str, data: bytes, traces: list, vues: dict):
    try:
        racine = ET.fromstring(data)
    except ET.ParseError:
        raise GpxErreur(f"« {nom_fichier} » n'est pas un GPX lisible.")
    if _local(racine.tag) != "gpx":
        raise GpxErreur(f"« {nom_fichier} » n'est pas un GPX.")
    for trk in racine:
        if _local(trk.tag) != "trk":
            continue
        nom = next((e.text.strip() for e in trk if _local(e.tag) == "name" and e.text), "")
        points = []
        for e in trk.iter():
            if _local(e.tag) != "trkpt":
                continue
            try:
                lat, lon = float(e.get("lat")), float(e.get("lon"))
            except (TypeError, ValueError):
                continue
            heure = next((_heure(t.text) for t in e if _local(t.tag) == "time"), None)
            points.append((lat, lon, heure))
        if not points:
            continue
        heures = [p[2] for p in points if p[2]]
        cle = tuple(points)
        traces.append({
            "nom": nom or nom_fichier.rsplit(".", 1)[0],
            "fichier": nom_fichier,
            "points": points,
            "debut": min(heures) if heures else None,
            "fin": max(heures) if heures else None,
            "doublon_de": vues.get(cle),
        })
        vues.setdefault(cle, len(traces) - 1)


def proposer(trace: dict, routes: list):
    """L'id de la route proposée pour cette trace : une route de la croisière
    partie le même jour ; s'il y en a plusieurs, celle dont l'heure de départ
    est la plus proche du début de la trace. None sans heure ou sans route."""
    if not trace["debut"]:
        return None
    jour = trace["debut"][:10]
    candidates = [r for r in routes if (r["start_time"] or "")[:10] == jour]
    if not candidates:
        return None
    debut = datetime.fromisoformat(trace["debut"])

    def ecart(r):
        try:
            return abs((datetime.fromisoformat(r["start_time"].replace(" ", "T")) - debut).total_seconds())
        except ValueError:
            return float("inf")
    return min(candidates, key=ecart)["id"]


def remplir_heures(points: list, defaut: str) -> list:
    """Les points prêts pour track_points (timestamp NOT NULL) : un point sans
    heure prend celle du précédent, ou la première connue, ou defaut."""
    derniere = next((p[2] for p in points if p[2]), defaut)
    sortie = []
    for lat, lon, heure in points:
        derniere = heure or derniere
        sortie.append((derniere, lat, lon))
    return sortie
