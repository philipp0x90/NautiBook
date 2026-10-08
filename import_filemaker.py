"""Import ponctuel d'une croisière de l'ancienne solution FileMaker.

Lit les trois exports CSV d'une croisière (UTF-8, séparateur « ; » ou « , »)
— la croisière, ses routes, ses lignes — et les écrit dans une base NautiBook :
la croisière, ses routes, leurs lignes de journal, les escales et l'équipage.
Chaque fichier est reconnu à ses titres de colonnes, pas à son nom.

Deux portes d'entrée sur la même fonction, importer() :
- l'app, page « Importer croisière (csv) » de la liste des croisières
  (/cruises/import, droit « imports ») ;
- le terminal, avec les fichiers posés dans un dossier :

    python import_filemaker.py import_filemaker --base copie.db --nom "Tremiti 2025"

Tout se fait dans une seule transaction : une erreur et rien n'est écrit. Une
croisière du même navire, au même nom et à la même date de départ, fait
refuser l'import — relancé par erreur, il ne double rien. Toujours essayer
d'abord sur une copie de logbook.db.

Correspondances retenues avec l'utilisateur (octobre 2026) :
- VentDir est l'angle du vent apparent, 0–360° depuis l'étrave → AWA signé
  (au-delà de 180°, bâbord, négatif) ; VentVit → AWS.
- Speed → SOG : le speedo était en panne, la vitesse notée est celle du GPS.
- Cap toujours à 0 ou vide (pas de compas) → vide. P° à 0 → vide (non mesuré).
- Sonde négative dans FileMaker → profondeur positive ; 0 → vide.
- Lat. / Long. en degrés-minutes-secondes → degrés décimaux signés.
- Une ligne datée après l'arrivée de sa route prend la date de cette arrivée
  (ligne 461 : saisie le lendemain, datée du lendemain).
- Le Journal d'une route → notes de la route, tel quel.
- La position visuelle n'est que dans l'export des routes : rattachée à la
  ligne de même route et même heure.
"""
import argparse
import csv
import io
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

VOILES = {"GV 1ris + Gén.": "GV 1 Ris + Génois", "+ Appui mot.": "+ Appui Moteur"}
TYPES_ESCALE = {"Marina": "Port"}
VOILES_APP = {"Moteur", "GV + Génois", "GV 1 Ris + Génois", "GV 2 Ris + Génois", "GV + Moteur",
              "+ Appui Moteur", "Génois seul", "GV seule", "GV + Trinq.", "GV 1 Ris + Trinq.",
              "GV 2 Ris + Trinq.", "Trinq. seule", "À sec"}


class ImportErreur(Exception):
    """Un problème dans les fichiers ou la base, expliqué en français : la
    page d'import l'affiche tel quel, le terminal aussi."""


# Les colonnes dont l'import a besoin, par fichier. Les titres sont ceux des
# rubriques FileMaker ; les deux premiers de chaque liste servent aussi à
# reconnaître le fichier.
COLONNES = {
    "croisière": ["_ID_Croisière", "Croisière_date_debut", "Croisière_date_fin", "Croisière_Origine",
                  "Croisière_Destination", "ta_Equipiers::Nom complet", "ta_Equipiers::Téléphone",
                  "ta_Equipiers::email", "ta_Equipages::Embarquement", "ta_Equipages::Débarquement",
                  "ta_Escales::Localité", "ta_Escales::Date arrivée", "ta_Escales::Date départ",
                  "ta_Escales::Nuitées"],
    "routes": ["_ID_Route", "Journal", "RO_Moment_Depart", "RO_Moment_Arrivee", "RO_Origine",
               "RO_Destination", "RO_HoramètreDépartRoute", "RO_HoramètreArrivéeRoute",
               "ta_Escales::Localité", "ta_Escales::Marina", "ta_Escales::Prix", "ta_Escales::Type",
               "ta_Lignes::Heure", "ta_Lignes::LI_Pos_Visu"],
    "lignes": ["_ID_Ligne", "ext_ID_Route", "Date", "Heure", "Allure", "Cap", "COG", "Lat.", "Long.",
               "Mer", "Odo", "P°", "Sonde", "Speed", "Température de l'eau", "Trip", "VentDir",
               "VentVit", "Visi", "Voiles"],
}


EXPORTS = {"croisière": "l'export de la croisière", "routes": "l'export des routes",
           "lignes": "l'export des lignes"}


def lire_texte(contenu: str) -> list:
    """Le contenu d'un export en liste de dicts ; « ; » ou « , » selon la
    première ligne (FileMaker exporte en « , », un passage par Excel en « ; »)."""
    contenu = contenu.lstrip("\ufeff")
    premiere = contenu.split("\n", 1)[0]
    separateur = ";" if premiere.count(";") >= premiere.count(",") else ","
    return list(csv.DictReader(io.StringIO(contenu, newline=""), delimiter=separateur))


def lire(chemin: Path) -> list:
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        return lire_texte(f.read())


def reconnaitre(fichiers: dict) -> dict:
    """{nom de fichier: lignes} → {"croisière"|"routes"|"lignes": lignes},
    chaque export reconnu à ses titres de colonnes. Lève ImportErreur si l'un
    manque, revient deux fois, ou s'il manque des colonnes."""
    trouves = {}
    for nom, lignes in fichiers.items():
        titres = set(lignes[0].keys()) if lignes else set()
        genre = next((g for g, cols in COLONNES.items() if set(cols[:2]) <= titres), None)
        if genre is None:
            raise ImportErreur(f"« {nom} » n'est ni l'export des croisières, ni celui des routes, "
                               "ni celui des lignes (ou il est vide).")
        if genre in trouves:
            raise ImportErreur(f"Deux fois {EXPORTS[genre]} : « {trouves[genre][0]} » et « {nom} ».")
        manquantes = [c for c in COLONNES[genre] if c not in titres]
        if manquantes:
            raise ImportErreur(f"Dans « {nom} » ({EXPORTS[genre]}), il manque les rubriques : "
                               + ", ".join(manquantes) + ".")
        trouves[genre] = (nom, lignes)
    absents = [EXPORTS[g] for g in COLONNES if g not in trouves]
    if absents:
        raise ImportErreur("Il manque " + " et ".join(absents) + ".")
    return {g: lignes for g, (nom, lignes) in trouves.items()}


def texte(v):
    v = (v or "").replace("﻿", "").strip()
    return v or None


def nombre(v):
    """« 2364,2 », « ,2 », « 176,35 L », « 70,00 € » → float ; vide → None."""
    v = texte(v)
    if v is None:
        return None
    v = re.sub(r"[^\d,.\-]", "", v).replace(",", ".")
    return float(v) if v not in ("", "-", ".") else None


def dms(v):
    """« 41°56'14,14 N » → 41.937261 ; « 15°53'25,28 E » → 15.890356."""
    v = texte(v)
    if v is None:
        return None
    m = re.fullmatch(r"(\d+)°(\d+)'([\d,.]+)\s*([NSEW])", v)
    if not m:
        raise ValueError(f"position illisible : {v}")
    d = int(m[1]) + int(m[2]) / 60 + float(m[3].replace(",", ".")) / 3600
    return round(-d if m[4] in "SW" else d, 6)


def jour(v):
    """« 01/09/2025 » → date."""
    return datetime.strptime(texte(v), "%d/%m/%Y").date()


def moment(v):
    """« 01/09/2025 18:59:32 » → datetime."""
    return datetime.strptime(texte(v), "%d/%m/%Y %H:%M:%S")


def plie(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def importer(base, nom: str, navire: int, exports: dict) -> dict:
    """Écrit la croisière décrite par les exports (sortie de reconnaitre())
    dans la base. Une seule transaction. Renvoie ce qui a été importé, pour
    le compte rendu ; lève ImportErreur sans rien écrire en cas de problème."""
    nom = (nom or "").strip()
    if not nom:
        raise ImportErreur("Donnez un nom à la croisière.")
    croisiere_rows, routes_rows, lignes_rows = exports["croisière"], exports["routes"], exports["lignes"]
    remarques = []
    try:
        return _importer(base, nom, navire, croisiere_rows, routes_rows, lignes_rows, remarques)
    except ImportErreur:
        raise
    except (ValueError, KeyError, IndexError) as e:
        raise ImportErreur(f"Valeur illisible dans les exports : {e}") from e


def _importer(base, nom, navire, croisiere_rows, routes_rows, lignes_rows, remarques):

    # ── Croisière ──
    c = croisiere_rows[0]
    debut, fin = jour(c["Croisière_date_debut"]), jour(c["Croisière_date_fin"])

    # ── Routes, et ce que l'export des routes porte en plus : escale de la
    # route (sur sa première rangée) et positions visuelles (par heure). ──
    routes, visu, route_courante = {}, {}, None
    for r in routes_rows:
        if texte(r["_ID_Route"]):
            route_courante = int(r["_ID_Route"])
            routes[route_courante] = {
                "debut": moment(r["RO_Moment_Depart"]), "fin": moment(r["RO_Moment_Arrivee"]),
                "origine": texte(r["RO_Origine"]), "destination": texte(r["RO_Destination"]),
                "h_debut": nombre(r["RO_HoramètreDépartRoute"]), "h_fin": nombre(r["RO_HoramètreArrivéeRoute"]),
                "journal": texte(r["Journal"]),
                "escale": {"localite": texte(r["ta_Escales::Localité"]), "marina": texte(r["ta_Escales::Marina"]),
                           "prix": nombre(r["ta_Escales::Prix"]), "type": texte(r["ta_Escales::Type"])},
            }
        if route_courante and texte(r["ta_Lignes::Heure"]) and texte(r["ta_Lignes::LI_Pos_Visu"]):
            visu[(route_courante, texte(r["ta_Lignes::Heure"]))] = texte(r["ta_Lignes::LI_Pos_Visu"])
    for rid, r in routes.items():
        if r["h_debut"] is not None and r["h_fin"] is not None and r["h_fin"] < r["h_debut"]:
            remarques.append(f"route {rid} : horamètre {r['h_debut']:g} → {r['h_fin']:g}, à rebours (importé tel quel)")

    # Dates d'escale et nuitées : dans l'export de la croisière, par localité.
    escales_dates = {}
    for r in croisiere_rows:
        loc = texte(r["ta_Escales::Localité"])
        if loc:
            escales_dates[loc] = (jour(r["ta_Escales::Date arrivée"]), jour(r["ta_Escales::Date départ"]),
                                  nombre(r["ta_Escales::Nuitées"]))

    # ── Lignes ──
    lignes = []
    for l in lignes_rows:
        rid = int(l["ext_ID_Route"])
        if rid not in routes:
            raise ImportErreur(f"Ligne {l['_ID_Ligne']} : route {rid} absente de l'export des routes.")
        heure = texte(l["Heure"])
        ts = datetime.strptime(f"{texte(l['Date'])} {heure}", "%d/%m/%Y %H:%M:%S")
        fin_route = routes[rid]["fin"]
        if ts > fin_route + timedelta(hours=1):
            corrige = datetime.combine(fin_route.date(), ts.time())
            remarques.append(f"ligne {l['_ID_Ligne']} : {ts:%d/%m %H:%M} après l'arrivée de sa route, "
                             f"redatée {corrige:%d/%m %H:%M}")
            ts = corrige
        cap, pression, sonde = nombre(l["Cap"]), nombre(l["P°"]), nombre(l["Sonde"])
        vent_dir = nombre(l["VentDir"])
        awa = None
        if vent_dir is not None:
            awa = round(vent_dir - 360 if vent_dir > 180 else vent_dir)
        voiles = texte(l["Voiles"])
        voiles = VOILES.get(voiles, voiles)
        if voiles and voiles not in VOILES_APP:
            remarques.append(f"ligne {l['_ID_Ligne']} : voiles « {voiles} » hors de la liste de l'app (gardé)")
        cog, temp = nombre(l["COG"]), nombre(l["Température de l'eau"])
        lignes.append({
            "route": rid, "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "aws": nombre(l["VentVit"]), "awa": awa,
            "sog": nombre(l["Speed"]),
            "heading": round(cap) if cap else None,
            "cog": round(cog) if cog is not None else None,
            "log": nombre(l["Odo"]), "trip": nombre(l["Trip"]),
            "depth": round(abs(sonde), 1) if sonde else None,
            "pressure": round(pression) if pression else None,
            "water_temp": round(temp, 1) if temp is not None else None,
            "sea_state": texte(l["Mer"]), "visibility": texte(l["Visi"]),
            "sails": voiles, "points_of_sail": texte(l["Allure"]),
            "lat": dms(l["Lat."]), "lon": dms(l["Long."]),
            "visual_pos": visu.get((rid, heure)),
        })

    # ── Écriture ──
    db = sqlite3.connect(base)
    db.execute("PRAGMA foreign_keys = ON")
    try:
        if not db.execute("SELECT 1 FROM ship_info WHERE id = ?", (navire,)).fetchone():
            raise ImportErreur(f"Navire {navire} introuvable.")
        if db.execute("SELECT 1 FROM cruises WHERE ship_id = ? AND name = ? AND start_time = ?",
                      (navire, nom, debut.isoformat())).fetchone():
            raise ImportErreur(f"« {nom} » du {debut:%d/%m/%Y} existe déjà sur ce navire.")
        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = db.execute(
            "INSERT INTO cruises (name, departure, destination, start_time, end_time, ship_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nom, texte(c["Croisière_Origine"]), texte(c["Croisière_Destination"]),
             debut.isoformat(), fin.isoformat(), navire, maintenant))
        cruise_id = cur.lastrowid

        ids_routes, n_escales = {}, 0
        for rid in sorted(routes):   # l'ordre des id NautiBook suit celui de FileMaker
            r = routes[rid]
            cur = db.execute(
                "INSERT INTO routes (cruise_id, start_time, end_time, departure_location, destination_location, "
                "notes, finished, motor_hours_start, motor_hours_end, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (cruise_id, r["debut"].strftime("%Y-%m-%dT%H:%M:%S"), r["fin"].strftime("%Y-%m-%dT%H:%M:%S"),
                 r["origine"], r["destination"], r["journal"], r["h_debut"], r["h_fin"], maintenant))
            ids_routes[rid] = cur.lastrowid
            e = r["escale"]
            if e["localite"]:
                arrivee, depart, nuits = escales_dates.get(e["localite"], (None, None, None))
                prix = e["prix"] or 0
                db.execute(
                    "INSERT INTO stopovers (route_id, locality, name, type, cost, cost_per_night, "
                    "arrival_date, departure_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cur.lastrowid, e["localite"], e["marina"], TYPES_ESCALE.get(e["type"], e["type"]),
                     prix, round(prix / nuits, 2) if nuits else None,
                     arrivee.isoformat() if arrivee else None, depart.isoformat() if depart else None))
                n_escales += 1

        for l in lignes:
            db.execute(
                "INSERT INTO logbook_lines (route_id, timestamp, aws, awa, sog, heading, cog, log, trip, depth, "
                "pressure, water_temp, sea_state, visibility, sails, points_of_sail, position_lat, position_lon, "
                "visual_pos) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ids_routes[l["route"]], l["timestamp"], l["aws"], l["awa"], l["sog"], l["heading"], l["cog"],
                 l["log"], l["trip"], l["depth"], l["pressure"], l["water_temp"], l["sea_state"],
                 l["visibility"], l["sails"], l["points_of_sail"], l["lat"], l["lon"], l["visual_pos"]))

        # Équipage : une fiche existante est reprise si prénom et début du
        # nom correspondent (« Sophie Lucas-Mansion » ↔ Sophie LUCAS) ; sinon
        # une fiche est créée. La première personne est skipper.
        fiches = db.execute("SELECT id, first_name, last_name FROM crew_members").fetchall()
        equipage = []
        for r in croisiere_rows:
            nom_complet = texte(r["ta_Equipiers::Nom complet"])
            if not nom_complet:
                continue
            prenom, _, nom_famille = nom_complet.partition(" ")
            trouve = next((f for f in fiches if plie(f[1]) == plie(prenom)
                           and f[2] and plie(nom_famille).startswith(plie(f[2]))), None)
            if trouve:
                membre_id = trouve[0]
            else:
                membre_id = db.execute(
                    "INSERT INTO crew_members (first_name, last_name, phone, email, created_at) VALUES (?, ?, ?, ?, ?)",
                    (prenom, nom_famille or None, texte(r["ta_Equipiers::Téléphone"]), texte(r["ta_Equipiers::email"]),
                     maintenant)).lastrowid
            db.execute(
                "INSERT INTO cruise_crew (cruise_id, crew_member_id, role, embark_date, disembark_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (cruise_id, membre_id, "skipper" if not equipage else "crew",
                 jour(r["ta_Equipages::Embarquement"]).isoformat(), jour(r["ta_Equipages::Débarquement"]).isoformat()))
            equipage.append(f"{nom_complet} ({'fiche existante' if trouve else 'fiche créée'})")

        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()

    return {"cruise_id": cruise_id, "nom": nom, "routes": len(routes), "lignes": len(lignes),
            "escales": n_escales, "equipage": equipage, "remarques": remarques}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("dossier", type=Path, help="dossier contenant les trois exports .csv")
    p.add_argument("--base", type=Path, required=True, help="la base NautiBook où écrire")
    p.add_argument("--nom", required=True, help="nom de la croisière dans NautiBook")
    p.add_argument("--navire", type=int, default=1, help="id du navire (ship_info), 1 par défaut")
    args = p.parse_args()
    try:
        exports = reconnaitre({f.name: lire(f) for f in sorted(args.dossier.glob("*.csv"))})
        r = importer(args.base, args.nom, args.navire, exports)
    except ImportErreur as e:
        sys.exit(str(e))
    print(f"« {r['nom']} » importée dans {args.base} (croisière n° {r['cruise_id']}) :")
    print(f"  {r['routes']} routes, {r['lignes']} lignes, {r['escales']} escales")
    print("  équipage : " + ", ".join(r["equipage"]))
    for m in r["remarques"]:
        print("  ! " + m)


if __name__ == "__main__":
    main()
