#!/usr/bin/env python3
"""
Carte interactive des maisons de santé pluriprofessionnelles (MSP) en France,
filtrable par spécialité présente.

Sources (open data) :
  - FINESS+ (ANS), fichier quotidien des structures (data.gouv.fr)
    -> liste des MSP (catégorie 603) et leurs coordonnées
  - Annuaire Santé (ANS), extraction PS_LibreAcces
    -> professionnels et leur lieu d'exercice, pour déduire les spécialités

Utilisation :
    python3 -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    python3 carte_maisons_sante.py
    -> ouvre carte_maisons_sante.html dans un navigateur

Chaque lancement reprend les données du jour (les fichiers de plus de 20 h
sont retéléchargés) : relancer le script suffit à mettre la carte à jour.

Options :
    --finess FICHIER   fichier FINESS déjà téléchargé : FINESS+ (.json ou .json.gz)
                       ou ancienne extraction géolocalisée (.csv)
    --ps DOSSIER|ZIP   extraction PS_LibreAcces déjà téléchargée
    --avec-centres     ajoute aussi les centres de santé (catégorie 124)
    --sortie FICHIER   nom du fichier HTML produit
"""

import argparse
import io
import json
import re
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
import requests
from pyproj import Transformer

import sources_sante

DOSSIER_DONNEES = Path("donnees")
FINESS_DATASET_API = "https://www.data.gouv.fr/api/1/datasets/53699569a3a729239d2046eb/"
PS_DATASET_API = "https://www.data.gouv.fr/api/1/datasets/69025e6c73d1f9b79ca3c365/"

CATEGORIES = {"603": "Maison de santé"}
CATEGORIE_CENTRES = {"124": "Centre de santé"}

# Projections utilisées par FINESS selon le territoire
EPSG_PAR_SOURCE = {
    "LAMBERT_93": 2154, "UTM_N20": 5490, "UTM_N22": 2972,
    "UTM_S40": 2975, "UTM_S38": 4471,
}
EPSG_PAR_DEPT = {"9A": 5490, "9B": 5490, "9C": 2972, "9D": 2975, "9F": 4471,
                 "971": 5490, "972": 5490, "973": 2972, "974": 2975, "976": 4471}


# ---------------------------------------------------------------- utilitaires
def sans_accents(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", str(txt))
    return "".join(c for c in txt if not unicodedata.combining(c))


def cle_colonne(nom: str) -> str:
    return re.sub(r"[^a-z0-9]", "", sans_accents(nom).lower())


def norm_voie(txt) -> str:
    if txt is None or (isinstance(txt, float) and pd.isna(txt)):
        return ""
    txt = sans_accents(txt).upper()
    txt = re.sub(r"[^A-Z0-9 ]", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def norm_num(txt) -> str:
    if txt is None or (isinstance(txt, float) and pd.isna(txt)):
        return ""
    m = re.match(r"\s*(\d+)", str(txt))
    return m.group(1) if m else ""


def telecharger(url: str, destination: Path) -> Path:
    # Retélécharge le fichier s'il a plus de 20 h (voir sources_sante.telecharger)
    return sources_sante.telecharger(url, destination)


# ---------------------------------------------------------------- FINESS
def recuperer_finess() -> Path:
    """Fichier quotidien FINESS+ : l'ancienne extraction CSV n'est plus mise à jour
    depuis les données du 4 mai 2026."""
    return sources_sante.recuperer_finess_plus()


def recuperer_finess_ancien() -> Path:
    meta = requests.get(FINESS_DATASET_API, timeout=60).json()
    candidats = [r for r in meta["resources"]
                 if "olocalis" in (r.get("title") or "").lower()
                 and (r.get("format") or "").lower() == "csv"]
    if not candidats:
        sys.exit("Fichier FINESS géolocalisé introuvable sur data.gouv.fr ; "
                 "télécharge-le à la main et passe-le avec --finess.")
    res = sorted(candidats, key=lambda r: r.get("last_modified", ""))[-1]
    return telecharger(res["url"], DOSSIER_DONNEES / "finess_geolocalise.csv")


def lire_finess(chemin: Path, categories: dict) -> pd.DataFrame:
    """Lit FINESS+ (.json / .json.gz) ou l'ancienne extraction (.csv)."""
    if chemin.suffix.lower() == ".csv":
        return lire_finess_ancien(chemin, categories)

    etabs = sources_sante.lire_etablissements_finess_plus(chemin, set(categories))
    df = pd.DataFrame(etabs)
    if df.empty:
        sys.exit("Aucune maison de santé trouvée dans le fichier FINESS.")
    df["type"] = df.categorie.map(categories)
    df["departement"] = df.departement.map(lambda d: sources_sante.NOMS_DEP.get(d, d))
    df["lat"] = df.lat.astype(object).where(df.lat.notna(), None)
    df["lon"] = df.lon.astype(object).where(df.lon.notna(), None)

    # Environ une structure sur quatre n'a pas encore de coordonnées dans FINESS+ :
    # on géocode son adresse (IGN), sinon on la place au centre de sa commune.
    sans = df[df.lat.isna()]
    if not sans.empty:
        print(f"  {len(sans)} structures sans coordonnées : géocodage de leur adresse")
        geo = sources_sante.Geocodeur()
        cles = {i: f"{r.code_commune}|{r.adresse or r.lieudit}" for i, r in sans.iterrows()}
        cache = geo.geocoder([(cles[i], r.adresse or r.lieudit, r.code_commune)
                              for i, r in sans.iterrows()])
        centres = sources_sante.centres_communes()
        for i, r in sans.iterrows():
            res = cache.get(cles[i]) or {}
            if res.get("lat") and res.get("type") in ("housenumber", "street"):
                df.at[i, "lat"], df.at[i, "lon"] = float(res["lat"]), float(res["lon"])
            elif r.code_commune in centres:
                df.at[i, "lat"], df.at[i, "lon"] = centres[r.code_commune]

    df["cle_adresse"] = df.cp + "|" + df.numvoie.map(norm_num) + "|" + df.voie.map(norm_voie)
    return df


def lire_finess_ancien(chemin: Path, categories: dict) -> pd.DataFrame:
    """Ancienne extraction (figée au 4 mai 2026).
    Le fichier mélange deux types de lignes : 'structureet' et 'geolocalisation'."""
    structures, geo = [], {}
    with open(chemin, encoding="latin-1") as f:
        for ligne in f:
            champs = ligne.rstrip("\n").split(";")
            if champs[0] == "structureet" and len(champs) >= 20:
                if champs[18] in categories:
                    structures.append(champs)
            elif champs[0] == "geolocalisation" and len(champs) >= 5:
                geo[champs[1]] = (champs[2], champs[3], champs[4])

    lignes = []
    for c in structures:
        nofinesset = c[1]
        dept = c[13]
        x, y, source = geo.get(nofinesset, ("", "", ""))
        epsg = next((v for k, v in EPSG_PAR_SOURCE.items() if k in source.upper()),
                    EPSG_PAR_DEPT.get(dept, 2154))
        acheminement = c[15]
        lignes.append({
            "finess": nofinesset,
            "nom": c[4] or c[3],
            "numvoie": c[7], "typvoie": c[8], "voie": c[9],
            "lieudit": c[11],
            "acheminement": acheminement,
            "cp": acheminement[:5],
            "departement": c[14],
            "telephone": c[16],
            "type": categories[c[18]],
            "x": x, "y": y, "epsg": epsg,
        })
    df = pd.DataFrame(lignes)
    if df.empty:
        sys.exit("Aucune maison de santé trouvée dans le fichier FINESS.")

    # Conversion des coordonnées en latitude / longitude
    df["lat"], df["lon"] = None, None
    for epsg, groupe in df.groupby("epsg"):
        ok = groupe[(groupe.x != "") & (groupe.y != "")]
        if ok.empty:
            continue
        t = Transformer.from_crs(int(epsg), 4326, always_xy=True)
        lon, lat = t.transform(ok.x.astype(float).values, ok.y.astype(float).values)
        df.loc[ok.index, "lon"] = lon
        df.loc[ok.index, "lat"] = lat

    df["adresse"] = (df.numvoie + " " + df.typvoie + " " + df.voie).str.strip()
    df["cle_adresse"] = df.cp + "|" + df.numvoie.map(norm_num) + "|" + df.voie.map(norm_voie)
    return df


# ---------------------------------------------------------------- Annuaire Santé
def recuperer_ps() -> Path:
    """Fichiers de l'Annuaire Santé republiés chaque jour sur data.gouv.fr."""
    dossier = DOSSIER_DONNEES / "annuaire_sante"
    dossier.mkdir(exist_ok=True)
    meta = requests.get(PS_DATASET_API, timeout=60).json()
    for res in meta["resources"]:
        titre = (res.get("title") or "").lower()
        if "personne-activite" in titre or "savoirfaire" in titre:
            telecharger(res["url"], dossier / titre)
    return dossier


def _motif(nom: str) -> str:
    return re.sub(r"[^a-z]", "", nom.lower())


def ouvrir_fichier_ps(source: Path, motif: str):
    """Renvoie un flux texte vers le fichier de l'extraction dont le nom contient `motif`
    (comparaison sans majuscules, tirets ni soulignés)."""
    m = _motif(motif)
    if source.is_dir():
        fichiers = sorted(f for f in source.iterdir() if m in _motif(f.name))
        if not fichiers:
            return None
        return open(fichiers[-1], encoding="utf-8")
    zf = zipfile.ZipFile(source)
    noms = sorted(n for n in zf.namelist() if m in _motif(n))
    if not noms:
        return None
    return io.TextIOWrapper(zf.open(noms[-1]), encoding="utf-8")


def trouver_colonnes(entete, voulues: dict) -> dict:
    cles = {cle_colonne(c): c for c in entete}
    trouvees = {}
    for nom, motifs in voulues.items():
        for m in motifs:
            hit = next((orig for k, orig in cles.items() if m in k), None)
            if hit:
                trouvees[nom] = hit
                break
    return trouvees


def nettoyer_specialite(sf: str) -> str:
    sf = re.sub(r"\s*\(.*?\)\s*", "", sf)
    sf = re.sub(r"^(Qualifi[ée] en|Sp[ée]cialiste en)\s+", "", sf, flags=re.I)
    return sf.strip().capitalize()


def lire_ps(source: Path, finess_msp: set, adresses_msp: set) -> pd.DataFrame:
    flux = ouvrir_fichier_ps(source, "Personne_activite")
    if flux is None:
        sys.exit("Fichier PS_LibreAcces_Personne_activite introuvable.")
    entete = flux.readline().rstrip("\n").rstrip("|").split("|")
    cols = trouver_colonnes(entete, {
        "id": ["identifiantpp"],
        "profession": ["libelleprofession"],
        "sf": ["libellesavoirfaire"],
        "finess": ["numerofinesssite"],
        "cp": ["codepostalcoord"],
        "num": ["numerovoiecoord"],
        "voie": ["libellevoiecoord"],
    })
    manquantes = {"id", "profession", "finess", "cp", "num", "voie"} - cols.keys()
    if manquantes:
        sys.exit(f"Colonnes introuvables dans l'Annuaire Santé : {manquantes}")

    # On relit depuis le début avec pandas, par morceaux (fichier volumineux)
    flux.close()
    flux = ouvrir_fichier_ps(source, "Personne_activite")
    usecols = list(cols.values())
    morceaux = []
    lecteur = pd.read_csv(flux, sep="|", dtype=str, usecols=usecols,
                          quoting=3, chunksize=500_000, on_bad_lines="skip")
    for i, bloc in enumerate(lecteur, 1):
        bloc = bloc.rename(columns={v: k for k, v in cols.items()})
        bloc["cle_adresse"] = (bloc.cp.fillna("") + "|" + bloc.num.map(norm_num)
                               + "|" + bloc.voie.map(norm_voie))
        garde = bloc.finess.isin(finess_msp) | bloc.cle_adresse.isin(adresses_msp)
        morceaux.append(bloc[garde])
        print(f"\r  {i * 500_000:,} lignes lues".replace(",", " "), end="", flush=True)
    print()
    ps = pd.concat(morceaux, ignore_index=True)

    # Spécialités des médecins : dans le fichier principal ou le fichier SavoirFaire
    if "sf" not in ps.columns:
        flux_sf = ouvrir_fichier_ps(source, "SavoirFaire")
        if flux_sf is not None:
            entete_sf = flux_sf.readline().rstrip("\n").rstrip("|").split("|")
            flux_sf.close()
            c_sf = trouver_colonnes(entete_sf, {"id": ["identifiantpp"],
                                                "sf": ["libellesavoirfaire"]})
            if len(c_sf) == 2:
                flux_sf = ouvrir_fichier_ps(source, "SavoirFaire")
                sf = pd.read_csv(flux_sf, sep="|", dtype=str, quoting=3,
                                 usecols=list(c_sf.values()), on_bad_lines="skip")
                sf = sf.rename(columns={v: k for k, v in c_sf.items()})
                sf = sf[sf.id.isin(ps.id)].dropna().drop_duplicates("id")
                ps = ps.merge(sf, on="id", how="left")
    if "sf" not in ps.columns:
        ps["sf"] = None
    return ps


def libelle_specialite(row) -> str:
    prof = (row.profession or "").strip()
    sf = row.sf if isinstance(row.sf, str) and row.sf.strip() else ""
    if prof.lower().startswith("m") and "decin" in sans_accents(prof).lower() and sf:
        return f"Médecin – {nettoyer_specialite(sf)}"
    return prof or "Autre"


# ---------------------------------------------------------------- carte
GABARIT_HTML = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Maisons de santé en France</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.Default.min.css">
<style>
 :root{--fond:#fff;--texte:#1f2933;--doux:#6b7280;--accent:#0f766e;--bord:#e5e7eb}
 *{box-sizing:border-box} body{margin:0;font:14px/1.4 system-ui,sans-serif;color:var(--texte);background:var(--fond)}
 #app{display:flex;height:100vh} #panneau{width:330px;padding:16px;overflow-y:auto;border-right:1px solid var(--bord)}
 #carte{flex:1} h1{font-size:18px;margin:0 0 4px} .doux{color:var(--doux);font-size:12px}
 input[type=search]{width:100%;padding:8px;margin:12px 0;border:1px solid var(--bord);border-radius:6px}
 .mode{display:flex;gap:12px;margin:8px 0 12px} .spe{display:flex;justify-content:space-between;padding:3px 0;cursor:pointer}
 .spe span:last-child{color:var(--doux)} #compteur{font-weight:600;color:var(--accent);margin:8px 0}
 button{padding:6px 10px;border:1px solid var(--bord);background:#f9fafb;border-radius:6px;cursor:pointer}
 .popup h3{margin:0 0 4px;font-size:14px} .popup ul{margin:6px 0 0;padding-left:18px}
 @media(max-width:700px){#app{flex-direction:column} #panneau{width:100%;height:40vh;border-right:0;border-bottom:1px solid var(--bord)}}
</style></head><body><div id="app">
<div id="panneau">
 <h1>Maisons de santé</h1>
 <div class="doux">Sources : FINESS et Annuaire Santé (ANS). Spécialités déduites des professionnels rattachés à l'adresse ou au n° FINESS de la structure.</div>
 <div class="doux">Données mises à jour le __DATE__.</div>
 <input type="search" id="recherche" placeholder="Rechercher un nom, une ville, un code postal…">
 <div id="compteur"></div>
 <div class="mode">
  <label><input type="radio" name="mode" value="ou" checked> au moins une</label>
  <label><input type="radio" name="mode" value="et"> toutes</label>
 </div>
 <label class="spe"><span><input type="checkbox" id="sansPro"> Inclure les structures sans professionnel identifié</span></label>
 <p><button id="aucune">Tout décocher</button></p>
 <div id="specialites"></div>
</div>
<div id="carte"></div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.min.js"></script>
<script>
const DONNEES = __DONNEES__;
const SPECIALITES = __SPECIALITES__;
const carte = L.map('carte').setView([46.6, 2.4], 6);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 19, attribution: '&copy; OpenStreetMap'}).addTo(carte);
const groupe = L.markerClusterGroup({chunkedLoading: true}).addTo(carte);

const echap = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
DONNEES.forEach(d => {
  const liste = Object.entries(d.s).sort((a,b) => b[1]-a[1])
    .map(([k,v]) => `<li>${echap(k)} (${v})</li>`).join('') || '<li>Aucun professionnel identifié</li>';
  d.marker = L.marker([d.lat, d.lon]).bindPopup(
    `<div class="popup"><h3>${echap(d.nom)}</h3>${echap(d.adr)}<br>${echap(d.ville)}` +
    (d.tel ? `<br>Tél. ${echap(d.tel)}` : '') + `<br><span class="doux">${echap(d.type)} · FINESS ${d.f}</span><ul>${liste}</ul></div>`);
  d.texte = (d.nom + ' ' + d.ville + ' ' + d.adr).toLowerCase();
});

const zone = document.getElementById('specialites');
SPECIALITES.forEach(([nom, n]) => {
  const l = document.createElement('label'); l.className = 'spe';
  l.innerHTML = `<span><input type="checkbox" value="${echap(nom)}"> ${echap(nom)}</span><span>${n}</span>`;
  zone.appendChild(l);
});

function filtrer() {
  const choix = [...zone.querySelectorAll('input:checked')].map(i => i.value);
  const mode = document.querySelector('input[name=mode]:checked').value;
  const q = document.getElementById('recherche').value.trim().toLowerCase();
  const sansPro = document.getElementById('sansPro').checked;
  const visibles = DONNEES.filter(d => {
    if (q && !d.texte.includes(q)) return false;
    const vide = Object.keys(d.s).length === 0;
    if (!choix.length) return sansPro || !vide;
    return mode === 'ou' ? choix.some(c => c in d.s) : choix.every(c => c in d.s);
  });
  groupe.clearLayers();
  groupe.addLayers(visibles.map(d => d.marker));
  document.getElementById('compteur').textContent =
    `${visibles.length.toLocaleString('fr-FR')} structure(s) affichée(s) sur ${DONNEES.length.toLocaleString('fr-FR')}`;
}
document.getElementById('panneau').addEventListener('input', filtrer);
document.getElementById('aucune').onclick = () => { zone.querySelectorAll('input').forEach(i => i.checked = false); filtrer(); };
filtrer();
</script></body></html>
"""


def generer_html(msp: pd.DataFrame, specialites: dict, sortie: Path):
    donnees, compte = [], Counter()
    for r in msp.itertuples():
        if r.lat is None or pd.isna(r.lat):
            continue
        s = specialites.get(r.finess, {})
        compte.update(s.keys())
        donnees.append({
            "f": r.finess, "nom": r.nom, "adr": r.adresse or r.lieudit,
            "ville": r.acheminement, "tel": r.telephone, "type": r.type,
            "lat": round(float(r.lat), 6), "lon": round(float(r.lon), 6), "s": s,
        })
    html = (GABARIT_HTML
            .replace("__DONNEES__", json.dumps(donnees, ensure_ascii=False))
            .replace("__SPECIALITES__", json.dumps(compte.most_common(), ensure_ascii=False))
            .replace("__DATE__", sources_sante.date_du_jour()))
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(html, encoding="utf-8")
    return len(donnees)


# ---------------------------------------------------------------- programme
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--finess", type=Path)
    p.add_argument("--ps", type=Path)
    p.add_argument("--avec-centres", action="store_true")
    p.add_argument("--sortie", type=Path, default=Path("carte_maisons_sante.html"))
    args = p.parse_args()
    DOSSIER_DONNEES.mkdir(exist_ok=True)

    categories = dict(CATEGORIES)
    if args.avec_centres:
        categories.update(CATEGORIE_CENTRES)

    print("1/4 FINESS")
    finess_path = args.finess or recuperer_finess()
    msp = lire_finess(finess_path, categories)
    print(f"  {len(msp)} structures, dont {msp.lat.notna().sum()} géolocalisées")

    print("2/4 Annuaire Santé (fichier volumineux, quelques minutes)")
    ps_path = args.ps or recuperer_ps()
    ps = lire_ps(ps_path, set(msp.finess), set(msp.cle_adresse))
    print(f"  {len(ps)} lignes d'activité rattachées")

    print("3/4 Rattachement des professionnels aux structures")
    adresse_vers_finess = defaultdict(list)
    for f, cle in zip(msp.finess, msp.cle_adresse):
        adresse_vers_finess[cle].append(f)
    ps["specialite"] = ps.apply(libelle_specialite, axis=1)
    ensemble_finess = set(msp.finess)
    pros = defaultdict(set)           # finess -> {(id, spécialité)}
    for r in ps.itertuples():
        cibles = [r.finess] if r.finess in ensemble_finess else adresse_vers_finess.get(r.cle_adresse, [])
        for f in cibles:
            pros[f].add((r.id, r.specialite))
    specialites = {f: dict(Counter(s for _, s in couples)) for f, couples in pros.items()}
    print(f"  {len(specialites)} structures avec au moins un professionnel identifié")

    print("4/4 Génération de la carte")
    n = generer_html(msp, specialites, args.sortie)
    print(f"  {n} structures placées → {args.sortie.resolve()}")


if __name__ == "__main__":
    main()
