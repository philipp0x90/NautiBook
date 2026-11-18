"""Import d'un journal Diarly (export Markdown, zip) dans le carnet de voyage.

L'export Diarly : un dossier par journal, un sous-dossier par année, un
fichier Markdown par jour (« 2022/08-01.md »), les photos dans « data/ » à
côté, et un diarly_meta.json à la racine qui donne pour chaque entrée sa date
(« journée entière » le plus souvent, donc sans heure) et pour chaque photo sa
position GPS — que les fichiers eux-mêmes n'ont plus.

Correspondances retenues avec l'utilisateur (18/11/2026) :
- une entrée → une publication, datée du jour à 20:00 (HEURE_DEFAUT) quand
  Diarly n'a pas d'heure ; corrigeable ensuite par l'Amiral ;
- la croisière : celle qui contient la date, sinon celle qui suit (proposer) ;
- la ligne de lieu en tête (le modèle quotidien « $location ») devient
  « 📍 Lieu » en tête de publication ;
- un lien de lieu sous des photos devient le nom de lieu de ces photos ;
- les titres « # … » deviennent une ligne en gras, les autres marques
  Markdown sont ramenées à celles du carnet (main._mise_en_forme).

Ce module ne touche pas à la base : lire() renvoie les entrées, main.py
propose, montre l'aperçu et écrit.
"""
import json
import re
import struct
import zipfile
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import unquote

HEURE_DEFAUT = "20:00:00"
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}

# « 2022/08-01.md », éventuellement suivi d'un complément (« 08-01 2.md »).
_CHEMIN_RE = re.compile(r"(?:^|/)(\d{4})/(\d{2})-(\d{2})[^/]*\.md$")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_LIEN_DIARLY_RE = re.compile(r"\[([^\]]*)\]\((diarly://[^)\s]*)\)")
_LIEN_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_COORDS_RE = re.compile(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$")


class DiarlyErreur(Exception):
    """Un export illisible : le message est montré tel quel."""


def _coords_lien(url: str):
    """diarly://map/Nom/42.70,2.97 ou diarly://map/42.70,2.97 → (lat, lon)."""
    m = _COORDS_RE.search(unquote(url))
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def _ligne_carnet(ligne: str) -> str:
    """Une ligne de Markdown ramenée aux marques du carnet."""
    ligne = _LIEN_DIARLY_RE.sub(lambda m: m.group(1), ligne)        # lien interne : son texte
    ligne = _LIEN_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})"
                         if m.group(1) != m.group(2) else m.group(2), ligne)
    titre = re.match(r"\s*#{1,6}\s+(.*?)\s*#*\s*$", ligne)
    if titre:
        return f"**{titre.group(1).strip()}**" if titre.group(1).strip() else ""
    ligne = re.sub(r"^(\s*)>\s?", r"\1", ligne)                       # citation
    ligne = re.sub(r"^(\s*)[-*+]\s+\[[xX]\]\s+", r"\1- ☑ ", ligne)    # cases cochées
    ligne = re.sub(r"^(\s*)[-*+]\s+\[ \]\s+", r"\1- ☐ ", ligne)
    ligne = re.sub(r"^(\s*)[*+]\s+", r"\1- ", ligne)                  # puces * et +
    ligne = re.sub(r"^(\s*)\d+[.)]\s+", r"\1- ", ligne)               # listes numérotées
    ligne = ligne.replace("__", "**")
    return ligne.rstrip()


def _convertir(md: str, dossier: PurePosixPath, fichiers_zip: set, meta_fichiers: dict):
    """Texte Markdown d'une entrée → (texte du carnet, photos).

    Chaque photo : {"chemin": dans le zip, "lat", "lon", "lieu"}. Un lien de
    lieu Diarly seul sur sa ligne nomme les photos qui le précèdent depuis le
    lieu précédent ; en tête d'entrée (avant tout texte), il devient
    « 📍 Lieu »."""
    lignes, photos, sans_lieu = [], [], []
    debut = True
    for brute in md.splitlines():
        images = _IMAGE_RE.findall(brute)
        for src in images:
            chemin = str(dossier / unquote(src))
            if chemin not in fichiers_zip or PurePosixPath(chemin).suffix.lower() not in PHOTO_SUFFIXES:
                continue
            info = meta_fichiers.get(PurePosixPath(chemin).name, {})
            lat = lon = None
            try:
                pos = json.loads(info.get("location") or "null")
                if pos:
                    lat, lon = float(pos["lat"]), float(pos["long"])
            except (ValueError, KeyError, TypeError):
                pass
            photo = {"chemin": chemin, "lat": lat, "lon": lon, "lieu": None}
            photos.append(photo)
            sans_lieu.append(photo)
        reste = _IMAGE_RE.sub("", brute).strip()
        if not reste:
            if not images:
                lignes.append("")
            continue
        seul = _LIEN_DIARLY_RE.fullmatch(reste)
        if seul and "/map/" in seul.group(2):
            nom = seul.group(1).strip()
            if debut:
                lignes.append(f"📍 {nom}")
                lignes.append("")
                continue
            if sans_lieu:
                lat, lon = _coords_lien(seul.group(2))
                for p in sans_lieu:
                    p["lieu"] = nom
                    if p["lat"] is None and lat is not None:
                        p["lat"], p["lon"] = lat, lon
                sans_lieu = []
                continue
            lignes.append(f"📍 {nom}")
            continue
        debut = False
        lignes.append(_ligne_carnet(brute))
    texte = re.sub(r"\n{3,}", "\n\n", "\n".join(lignes)).strip()
    return texte or None, photos


def _horodatage(jour: str, info: dict) -> str:
    """Le jour à HEURE_DEFAUT ; l'heure de Diarly quand l'entrée en a une
    (pas « journée entière »), ramenée à l'heure locale de la machine."""
    if info and not info.get("isFullDay", True) and info.get("dayDate"):
        try:
            moment = datetime.fromisoformat(info["dayDate"].replace("Z", "+00:00")).astimezone()
            if moment.strftime("%Y-%m-%d") == jour:
                return moment.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return f"{jour} {HEURE_DEFAUT}"


def lire(chemin_zip) -> list:
    """Les entrées de l'export, dans l'ordre des dates : {"cle", "journal",
    "jour", "horodatage", "texte", "photos"}. "cle" est le chemin de l'entrée
    dans le zip, qui la retrouve à la confirmation."""
    try:
        archive = zipfile.ZipFile(chemin_zip)
    except zipfile.BadZipFile:
        raise DiarlyErreur("Ce fichier n'est pas un zip : choisissez l'export de Diarly tel quel.")
    with archive:
        noms = [n for n in archive.namelist() if not n.startswith("__MACOSX/")]
        fichiers_zip = set(noms)
        meta_nom = next((n for n in noms if PurePosixPath(n).name == "diarly_meta.json"), None)
        meta = {}
        if meta_nom:
            try:
                meta = json.loads(archive.read(meta_nom).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                meta = {}
        racine = str(PurePosixPath(meta_nom).parent) if meta_nom else ""
        racine = "" if racine == "." else racine
        meta_notes, meta_fichiers = meta.get("notes", {}), meta.get("files", {})
        entrees = []
        for nom in noms:
            m = _CHEMIN_RE.search(nom)
            if not m:
                continue
            jour = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            try:
                datetime.strptime(jour, "%Y-%m-%d")
                md = archive.read(nom).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            chemin = PurePosixPath(nom)
            relatif = "/" + str(chemin.relative_to(racine)) if racine else "/" + nom
            journal = chemin.parent.parent.name
            texte, photos = _convertir(md, chemin.parent, fichiers_zip, meta_fichiers)
            if not texte and not photos:
                continue
            entrees.append({"cle": nom, "journal": journal, "jour": jour,
                            "horodatage": _horodatage(jour, meta_notes.get(relatif, {})),
                            "texte": texte, "photos": photos})
    if not entrees:
        raise DiarlyErreur("Aucune entrée trouvée : est-ce bien un export Markdown de Diarly ?")
    entrees.sort(key=lambda e: (e["horodatage"], e["cle"]))
    return entrees


def corriger_exif(data: bytes) -> bytes:
    """Remet dans l'EXIF d'un JPEG ses vraies dimensions.

    Diarly réduit les photos (2048 px) mais laisse dans l'EXIF celles de
    l'original (PixelXDimension / PixelYDimension, 4032 × 3024, parfois dans
    l'autre sens) : Safari s'en sert pour la taille d'affichage, et la photo
    agrandie paraissait minuscule. Les deux valeurs sont réécrites sur place
    d'après l'en-tête de l'image (SOF) ; tout le reste est intact. Un fichier
    qui n'est pas un JPEG, ou sans EXIF, revient tel quel."""
    if data[:2] != b"\xff\xd8":
        return data
    data = bytearray(data)
    i, exif, taille = 2, None, None
    while i + 4 <= len(data) and data[i] == 0xFF:
        marqueur = data[i + 1]
        longueur = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marqueur == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00" and exif is None:
            exif = i + 10
        elif 0xC0 <= marqueur <= 0xCF and marqueur not in (0xC4, 0xC8, 0xCC):
            hauteur, largeur = struct.unpack(">HH", data[i + 5:i + 9])
            taille = (largeur, hauteur)
        if marqueur == 0xDA or taille and exif:
            break
        i += 2 + longueur
    if exif is None or taille is None:
        return bytes(data)
    try:
        ordre = "<" if data[exif:exif + 2] == b"II" else ">"

        def entrees(debut):
            n = struct.unpack(ordre + "H", data[debut:debut + 2])[0]
            for k in range(n):
                pos = debut + 2 + 12 * k
                tag, typ = struct.unpack(ordre + "HH", data[pos:pos + 4])
                yield tag, typ, pos + 8

        ifd0 = exif + struct.unpack(ordre + "I", data[exif + 4:exif + 8])[0]
        pointeur = next((struct.unpack(ordre + "I", data[v:v + 4])[0]
                         for tag, typ, v in entrees(ifd0) if tag == 0x8769), None)
        if pointeur is None:
            return bytes(data)
        for tag, typ, v in entrees(exif + pointeur):
            if tag in (0xA002, 0xA003):
                valeur = taille[0] if tag == 0xA002 else taille[1]
                if typ == 3:
                    data[v:v + 2] = struct.pack(ordre + "H", valeur)
                elif typ == 4:
                    data[v:v + 4] = struct.pack(ordre + "I", valeur)
    except struct.error:
        pass
    return bytes(data)


def photo(chemin_zip, chemin: str) -> bytes:
    """Le contenu d'une photo de l'export, dimensions EXIF corrigées."""
    with zipfile.ZipFile(chemin_zip) as archive:
        return corriger_exif(archive.read(chemin))


def proposer(jour: str, croisieres: list):
    """La croisière d'une entrée : celle dont les dates contiennent le jour,
    sinon la première qui commence après (les jours de préparation précèdent
    le départ), sinon la dernière avant. croisieres : {"id", "start", "end"},
    dates ISO, triées par début."""
    datees = [c for c in croisieres if c["start"]]
    for c in datees:
        if c["start"][:10] <= jour <= (c["end"] or c["start"])[:10]:
            return c["id"]
    suivantes = [c for c in datees if c["start"][:10] > jour]
    if suivantes:
        return suivantes[0]["id"]
    return datees[-1]["id"] if datees else None
