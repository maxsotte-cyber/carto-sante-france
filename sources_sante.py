"""
Outils communs aux deux cartes : téléchargement des données ouvertes,
lecture du nouveau FINESS (FINESS+), géocodage IGN avec cache, centres des communes.

Ce module n'est pas fait pour être lancé seul : les scripts
carte_maisons_sante.py et carte_professionnels_sante.py l'importent.
"""

import csv
import gzip
import io
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

DOSSIER_DONNEES = Path("donnees")

# Jeu « FINESS - Structures » (FINESS+) publié chaque jour par l'ANS sur data.gouv.fr
FINESS_PLUS_DATASET_API = "https://www.data.gouv.fr/api/1/datasets/69fb10fd3d405934d1077e01/"
# Annuaire Santé (extractions en libre accès, republiées chaque jour sur data.gouv.fr)
PS_DATASET_API = "https://www.data.gouv.fr/api/1/datasets/69025e6c73d1f9b79ca3c365/"

# Géocodage de la Géoplateforme (IGN), sans clé
GEOCODAGE_CSV = "https://data.geopf.fr/geocodage/search/csv"
GEOCODAGE_UNITAIRE = "https://data.geopf.fr/geocodage/search"
# Centres des communes (API Découpage administratif)
COMMUNES_API = ("https://geo.api.gouv.fr/communes?fields=code,centre"
                "&type=commune-actuelle,arrondissement-municipal&format=json")

# Un fichier téléchargé est réutilisé tant qu'il a moins de 20 heures :
# les sources sont publiées chaque jour, au-delà on reprend la version du jour.
DUREE_FRAICHEUR_H = 20

NOMS_DEP = {
    "01": "Ain", "02": "Aisne", "03": "Allier", "04": "Alpes-de-Haute-Provence",
    "05": "Hautes-Alpes", "06": "Alpes-Maritimes", "07": "Ardèche", "08": "Ardennes",
    "09": "Ariège", "10": "Aube", "11": "Aude", "12": "Aveyron", "13": "Bouches-du-Rhône",
    "14": "Calvados", "15": "Cantal", "16": "Charente", "17": "Charente-Maritime",
    "18": "Cher", "19": "Corrèze", "2A": "Corse-du-Sud", "2B": "Haute-Corse",
    "21": "Côte-d'Or", "22": "Côtes-d'Armor", "23": "Creuse", "24": "Dordogne",
    "25": "Doubs", "26": "Drôme", "27": "Eure", "28": "Eure-et-Loir", "29": "Finistère",
    "30": "Gard", "31": "Haute-Garonne", "32": "Gers", "33": "Gironde", "34": "Hérault",
    "35": "Ille-et-Vilaine", "36": "Indre", "37": "Indre-et-Loire", "38": "Isère",
    "39": "Jura", "40": "Landes", "41": "Loir-et-Cher", "42": "Loire", "43": "Haute-Loire",
    "44": "Loire-Atlantique", "45": "Loiret", "46": "Lot", "47": "Lot-et-Garonne",
    "48": "Lozère", "49": "Maine-et-Loire", "50": "Manche", "51": "Marne",
    "52": "Haute-Marne", "53": "Mayenne", "54": "Meurthe-et-Moselle", "55": "Meuse",
    "56": "Morbihan", "57": "Moselle", "58": "Nièvre", "59": "Nord", "60": "Oise",
    "61": "Orne", "62": "Pas-de-Calais", "63": "Puy-de-Dôme", "64": "Pyrénées-Atlantiques",
    "65": "Hautes-Pyrénées", "66": "Pyrénées-Orientales", "67": "Bas-Rhin", "68": "Haut-Rhin",
    "69": "Rhône", "70": "Haute-Saône", "71": "Saône-et-Loire", "72": "Sarthe",
    "73": "Savoie", "74": "Haute-Savoie", "75": "Paris", "76": "Seine-Maritime",
    "77": "Seine-et-Marne", "78": "Yvelines", "79": "Deux-Sèvres", "80": "Somme",
    "81": "Tarn", "82": "Tarn-et-Garonne", "83": "Var", "84": "Vaucluse", "85": "Vendée",
    "86": "Vienne", "87": "Haute-Vienne", "88": "Vosges", "89": "Yonne",
    "90": "Territoire de Belfort", "91": "Essonne", "92": "Hauts-de-Seine",
    "93": "Seine-Saint-Denis", "94": "Val-de-Marne", "95": "Val-d'Oise",
    "971": "Guadeloupe", "972": "Martinique", "973": "Guyane", "974": "La Réunion",
    "975": "Saint-Pierre-et-Miquelon", "976": "Mayotte", "977": "Saint-Barthélemy",
    "978": "Saint-Martin", "986": "Wallis-et-Futuna", "987": "Polynésie française",
    "988": "Nouvelle-Calédonie",
}


# ---------------------------------------------------------------- petits utilitaires
def sans_accents(txt) -> str:
    txt = unicodedata.normalize("NFKD", str(txt))
    return "".join(c for c in txt if not unicodedata.combining(c))


def vide(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or str(v).strip() == ""


def norm_voie(txt) -> str:
    if vide(txt):
        return ""
    txt = sans_accents(txt).upper()
    txt = re.sub(r"[^A-Z0-9 ]", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def norm_num(txt) -> str:
    if vide(txt):
        return ""
    m = re.match(r"\s*(\d+)", str(txt))
    return m.group(1) if m else ""


def departement_de(code_commune: str, code_postal: str = "") -> str:
    """Code département à partir du code commune INSEE (ou, à défaut, du code postal)."""
    cc = (code_commune or "").strip().upper()
    if len(cc) == 5:
        return cc[:3] if cc[:2] in ("97", "98") else cc[:2]
    cp = (code_postal or "").strip()
    if len(cp) == 5 and cp.isdigit():
        if cp[:2] in ("97", "98"):
            return cp[:3]
        if cp.startswith("20"):
            return "2A" if int(cp) < 20200 else "2B"
        return cp[:2]
    return ""


def date_du_jour() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y")


def telecharger(url: str, destination: Path) -> Path:
    """Télécharge `url` vers `destination`, sauf si une version de moins de 20 h existe déjà."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        age_h = (time.time() - destination.stat().st_mtime) / 3600
        if age_h < DUREE_FRAICHEUR_H:
            print(f"  déjà à jour : {destination}")
            return destination
    print(f"  téléchargement : {url}")
    provisoire = destination.with_suffix(destination.suffix + ".part")
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        recu = 0
        with open(provisoire, "wb") as f:
            for bloc in r.iter_content(chunk_size=1 << 20):
                f.write(bloc)
                recu += len(bloc)
                if total:
                    print(f"\r  {recu / total:6.1%}", end="", flush=True)
        print()
    provisoire.replace(destination)   # le fichier n'apparaît qu'une fois complet
    return destination


# ---------------------------------------------------------------- FINESS+
def recuperer_finess_plus() -> Path:
    """Télécharge le dernier fichier quotidien FINESS+ (structures), ou à défaut le mensuel."""
    meta = requests.get(FINESS_PLUS_DATASET_API, timeout=60).json()
    ressources = [r for r in meta.get("resources", [])
                  if "structures" in (r.get("title") or r.get("url") or "").lower()
                  and ".json" in (r.get("title") or r.get("url") or "").lower()]
    for genre in ("journalier", "mensuel", "annuel"):
        candidats = [r for r in ressources
                     if genre in ((r.get("title") or "") + (r.get("url") or "")).lower()]
        if candidats:
            res = max(candidats, key=lambda r: (r.get("last_modified") or "",
                                                r.get("title") or ""))
            nom = "finess_structures.json.gz" if res["url"].endswith(".gz") else "finess_structures.json"
            return telecharger(res["url"], DOSSIER_DONNEES / nom)
    sys.exit("Fichier FINESS+ (structures) introuvable sur data.gouv.fr ; "
             "télécharge-le à la main et passe-le avec --finess.")


def ouvrir_binaire(chemin: Path):
    with open(chemin, "rb") as test:
        debut = test.read(2)
    return gzip.open(chemin, "rb") if debut == b"\x1f\x8b" else open(chemin, "rb")


def lire_etablissements_finess_plus(chemin: Path, categories=None, actifs_seulement=True):
    """Parcourt le fichier FINESS+ sans le charger entier en mémoire et renvoie
    une liste de dictionnaires, un par établissement géographique (EGE).

    categories : ensemble de codes catégorie à garder (ex. {"603"}), ou None pour tout.
    """
    import ijson

    etablissements = []
    with ouvrir_binaire(chemin) as flux:
        for pm in ijson.items(flux, "pmej.item", use_float=True):
            for e in pm.get("ege") or []:
                cat = str(e.get("categorieentiteGeographiqueExercice") or "")
                if categories is not None and cat not in categories:
                    continue
                if actifs_seulement and e.get("etatObjet") != "A":
                    continue
                infos = e.get("informationsGeneralesEGE") or {}
                adresses = e.get("adresse") or []
                adr = next((a for a in adresses if a.get("usageAdresse") == "03"),
                           adresses[0] if adresses else {})
                geo = adr.get("coordonneesGeographique") or {}
                tel = ""
                for c in e.get("contact") or []:
                    tel = (c.get("telecom") or {}).get("telephone") or ""
                    if tel:
                        break
                lat, lon = geo.get("directionLatitude"), geo.get("directionLongitude")
                cc = adr.get("cogCommune") or ""
                cp = adr.get("codePostal") or ""
                etablissements.append({
                    "finess": infos.get("numFinessEge") or "",
                    "nom": infos.get("nomEgeLong") or infos.get("nomEgeCourt") or "",
                    "categorie": cat,
                    "numvoie": str(adr.get("numeroVoie") or ""),
                    "typvoie": (adr.get("typeVoie") or "").strip(),
                    "voie": adr.get("libelleVoie") or "",
                    "lieudit": adr.get("lieuDit") or "",
                    "adresse": (adr.get("ligneQuatre") or "").strip(),
                    "cp": cp,
                    "code_commune": cc,
                    "acheminement": " ".join(x for x in (cp, adr.get("ligneAcheminement") or "") if x),
                    "departement": departement_de(cc, cp),
                    "telephone": tel,
                    "lat": float(lat) if not vide(lat) else None,
                    "lon": float(lon) if not vide(lon) else None,
                })
    return etablissements


# ---------------------------------------------------------------- Annuaire Santé
def recuperer_annuaire_sante() -> Path:
    """Fichiers de l'Annuaire Santé republiés chaque jour sur data.gouv.fr."""
    dossier = DOSSIER_DONNEES / "annuaire_sante"
    dossier.mkdir(parents=True, exist_ok=True)
    meta = requests.get(PS_DATASET_API, timeout=60).json()
    trouves = 0
    for res in meta.get("resources", []):
        titre = (res.get("title") or "").lower()
        if "personne-activite" in titre or "savoirfaire" in titre:
            telecharger(res["url"], dossier / titre)
            trouves += 1
    if not trouves:
        sys.exit("Fichiers de l'Annuaire Santé introuvables sur data.gouv.fr ; "
                 "télécharge-les à la main et passe le dossier avec --ps.")
    return dossier


# ---------------------------------------------------------------- centres des communes
def centres_communes() -> dict:
    """{code commune: (lat, lon)} pour placer un lieu au centre de sa commune
    quand son adresse n'a pas pu être géocodée."""
    chemin = DOSSIER_DONNEES / "communes_centres.json"
    if not chemin.exists() or (time.time() - chemin.stat().st_mtime) > 30 * 86400:
        try:
            r = requests.get(COMMUNES_API, timeout=120)
            r.raise_for_status()
            centres = {c["code"]: c["centre"]["coordinates"][::-1]
                       for c in r.json() if c.get("centre")}
            chemin.parent.mkdir(parents=True, exist_ok=True)
            chemin.write_text(json.dumps(centres), encoding="utf-8")
        except Exception as err:   # le service est indisponible : on garde l'ancienne liste
            print(f"  centres des communes indisponibles ({err})")
            if not chemin.exists():
                return {}
    return {k: tuple(v) for k, v in json.loads(chemin.read_text(encoding="utf-8")).items()}


# ---------------------------------------------------------------- géocodage IGN avec cache
class Geocodeur:
    """Géocode des adresses avec le service de l'IGN et garde les résultats
    dans donnees/geocodage_cache.csv : une adresse n'est demandée qu'une fois."""

    COLONNES = ["cle", "lat", "lon", "type", "score"]

    def __init__(self, chemin_cache: Path = None, minutes_max: float = None):
        self.chemin = chemin_cache or DOSSIER_DONNEES / "geocodage_cache.csv"
        self.limite = time.time() + minutes_max * 60 if minutes_max else None
        self.cache = {}
        if self.chemin.exists():
            with open(self.chemin, encoding="utf-8", newline="") as f:
                for ligne in csv.DictReader(f, delimiter=";"):
                    self.cache[ligne["cle"]] = ligne
        self.mode_lot = True

    def temps_ecoule(self) -> bool:
        return self.limite is not None and time.time() > self.limite

    def _enregistrer(self, resultats):
        neuf = not self.chemin.exists()
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        with open(self.chemin, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.COLONNES, delimiter=";")
            if neuf:
                w.writeheader()
            for r in resultats:
                self.cache[r["cle"]] = r
                w.writerow(r)

    @staticmethod
    def _resultat(cle, lat, lon, typ, score):
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return {"cle": cle, "lat": "", "lon": "", "type": "introuvable", "score": "0"}
        return {"cle": cle, "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
                "type": typ or "", "score": str(score or "")}

    def geocoder_lot(self, lot):
        """lot : liste de (cle, texte_adresse, code_commune). Envoi d'un fichier CSV à l'IGN."""
        tampon = io.StringIO()
        w = csv.writer(tampon, delimiter=";")
        w.writerow(["cle", "q", "citycode"])
        for cle, q, cc in lot:
            w.writerow([cle, q, cc])
        r = requests.post(
            GEOCODAGE_CSV,
            files={"data": ("adresses.csv", tampon.getvalue().encode("utf-8"), "text/csv")},
            data=[("columns", "q"), ("citycode", "citycode"), ("indexes", "address"),
                  ("result_columns", "latitude"), ("result_columns", "longitude"),
                  ("result_columns", "result_type"), ("result_columns", "result_score")],
            timeout=600)
        r.raise_for_status()
        lecteur = csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";")
        sortie = []
        for ligne in lecteur:
            sortie.append(self._resultat(ligne.get("cle"), ligne.get("latitude"),
                                         ligne.get("longitude"), ligne.get("result_type"),
                                         ligne.get("result_score")))
        if len(sortie) != len(lot):
            raise ValueError("réponse incomplète du géocodeur")
        return sortie

    def geocoder_unitaire(self, lot):
        sortie = []
        session = requests.Session()
        for cle, q, cc in lot:
            if self.temps_ecoule():
                break
            params = {"q": q, "limit": 1, "index": "address"}
            if cc:
                params["citycode"] = cc
            try:
                r = session.get(GEOCODAGE_UNITAIRE, params=params, timeout=30)
                r.raise_for_status()
                feats = r.json().get("features") or []
            except Exception:
                feats = []
                time.sleep(1)
            if feats:
                lon, lat = feats[0]["geometry"]["coordinates"]
                p = feats[0].get("properties") or {}
                sortie.append(self._resultat(cle, lat, lon, p.get("type"), p.get("score")))
            else:
                sortie.append(self._resultat(cle, None, None, None, None))
            time.sleep(0.03)   # le service accepte environ 50 requêtes par seconde
        return sortie

    def geocoder(self, adresses, taille_lot=5000):
        """adresses : liste de (cle, texte_adresse, code_commune).
        Renvoie le cache complet (dict cle -> résultat)."""
        a_faire = [a for a in dict((a[0], a) for a in adresses).values()
                   if a[0] not in self.cache and a[1].strip()]
        if not a_faire:
            return self.cache
        print(f"  {len(a_faire):,} adresse(s) à géocoder".replace(",", " "))
        for i in range(0, len(a_faire), taille_lot):
            if self.temps_ecoule():
                reste = len(a_faire) - i
                print(f"\n  temps de géocodage écoulé : {reste:,} adresse(s) reportée(s) "
                      "à la prochaine mise à jour".replace(",", " "))
                break
            lot = a_faire[i:i + taille_lot]
            resultats = None
            if self.mode_lot:
                try:
                    resultats = self.geocoder_lot(lot)
                except Exception as err:
                    print(f"\n  envoi par fichier refusé ({err}) : passage en requêtes unitaires")
                    self.mode_lot = False
            if resultats is None:
                resultats = self.geocoder_unitaire(lot)
            self._enregistrer(resultats)
            print(f"\r  {min(i + taille_lot, len(a_faire)):,} / {len(a_faire):,}"
                  .replace(",", " "), end="", flush=True)
        print()
        return self.cache


# ---------------------------------------------------------------- contrôle des positions
def cadres_departements(points) -> dict:
    """points : itérable de (departement, lat, lon) jugés fiables (coordonnées FINESS).
    Renvoie {dep: (lat_min, lat_max, lon_min, lon_max)}, valeurs extrêmes écartées."""
    par_dep = {}
    for dep, lat, lon in points:
        if dep and lat is not None and lon is not None:
            par_dep.setdefault(dep, ([], []))
            par_dep[dep][0].append(lat)
            par_dep[dep][1].append(lon)
    cadres = {}
    for dep, (lats, lons) in par_dep.items():
        if len(lats) < 5:
            continue
        lats, lons = sorted(lats), sorted(lons)
        q = lambda v, p: v[min(len(v) - 1, max(0, int(p * (len(v) - 1))))]
        marge = 0.15
        cadres[dep] = (q(lats, 0.005) - marge, q(lats, 0.995) + marge,
                       q(lons, 0.005) - marge, q(lons, 0.995) + marge)
    return cadres


def dans_cadre(cadres: dict, dep: str, lat, lon) -> bool:
    if lat is None or lon is None:
        return False
    c = cadres.get(dep)
    if c is None:          # pas de cadre connu : on vérifie juste que le point est plausible
        return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)
    return c[0] <= lat <= c[1] and c[2] <= lon <= c[3]
