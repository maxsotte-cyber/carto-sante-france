#!/usr/bin/env python3
"""
Carte interactive de tous les professionnels de santé en France.

Sources (open data) :
  - Annuaire Santé (ANS, data.gouv.fr) : professionnels, profession, spécialité,
    mode d'exercice et adresse du lieu d'exercice
  - FINESS+ (ANS, data.gouv.fr) : coordonnées des établissements
    (hôpitaux, cliniques, pharmacies, laboratoires, EHPAD, centres…)
  - Géocodage IGN (Géoplateforme) : coordonnées des cabinets qui n'ont qu'une adresse

Utilisation :
    pip install -r requirements.txt
    python3 carte_professionnels_sante.py --departements 49     # essai rapide sur un département
    python3 carte_professionnels_sante.py --departements 49,72,53
    python3 carte_professionnels_sante.py                       # France entière (long la 1re fois)
    -> ouvrir carte_professionnels/index.html dans un navigateur

Le géocodage est gardé en cache (donnees/geocodage_cache.csv) : il n'est fait
qu'une fois par adresse, et le script peut être interrompu puis relancé.
Chaque lancement reprend les données du jour : relancer le script met la carte à jour.

Options :
    --departements LISTE   codes séparés par des virgules (ex. 49,2A,974)
    --finess FICHIER       fichier FINESS+ déjà téléchargé (.json ou .json.gz)
    --ps DOSSIER|ZIP       extraction PS_LibreAcces déjà téléchargée
    --sortie DOSSIER       dossier de la carte (par défaut carte_professionnels)
    --geocodage-minutes N  durée maximale du géocodage ; les adresses restantes sont
                           placées au centre de leur commune et géocodées au lancement suivant
"""

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

import sources_sante as S

MODES = {"L": "Libéral", "S": "Salarié", "B": "Bénévole"}


# ---------------------------------------------------------------- Annuaire Santé
def cle_colonne(nom: str) -> str:
    return re.sub(r"[^a-z0-9]", "", S.sans_accents(nom).lower())


def trouver_colonnes(entete, voulues: dict) -> dict:
    cles = {cle_colonne(c): c for c in entete}
    trouvees = {}
    for nom, motifs in voulues.items():
        for m in motifs:
            hit = next((orig for k, orig in cles.items() if k == m), None) \
                or next((orig for k, orig in cles.items() if m in k), None)
            if hit:
                trouvees[nom] = hit
                break
    return trouvees


def _motif(nom: str) -> str:
    return re.sub(r"[^a-z]", "", nom.lower())


def ouvrir_fichier_ps(source: Path, motif: str):
    import io
    import zipfile
    m = _motif(motif)
    if source.is_dir():
        fichiers = sorted(f for f in source.iterdir() if m in _motif(f.name))
        return open(fichiers[-1], encoding="utf-8") if fichiers else None
    zf = zipfile.ZipFile(source)
    noms = sorted(n for n in zf.namelist() if m in _motif(n))
    return io.TextIOWrapper(zf.open(noms[-1]), encoding="utf-8") if noms else None


COLONNES_PS = {
    "id": ["identifiantpp"],
    "nom": ["nomdexercice"],
    "prenom": ["prenomdexercice"],
    "profession": ["libelleprofession"],
    "sf": ["libellesavoirfaire"],
    "mode_code": ["codemodeexercice"],
    "mode_lib": ["libellemodeexercice"],
    "finess": ["numerofinesssite"],
    "raison": ["raisonsocialesite"],
    "enseigne": ["enseignecommercialesite"],
    "num": ["numerovoiecoordstructure", "numerovoie"],
    "indice": ["indicerepetitionvoie"],
    "typvoie": ["libelletypedevoie"],
    "voie": ["libellevoiecoordstructure", "libellevoie"],
    "cp": ["codepostalcoordstructure", "codepostal"],
    "cc": ["codecommunecoordstructure", "codecommune"],
    "commune": ["libellecommunecoordstructure", "libellecommune"],
    "tel": ["telephonecoordstructure"],
}
OBLIGATOIRES = {"id", "nom", "profession", "finess", "cp", "cc", "voie"}


def lire_ps(source: Path, departements) -> pd.DataFrame:
    flux = ouvrir_fichier_ps(source, "Personne_activite")
    if flux is None:
        sys.exit("Fichier PS_LibreAcces_Personne_activite introuvable.")
    entete = flux.readline().rstrip("\n").rstrip("|").split("|")
    flux.close()
    cols = trouver_colonnes(entete, COLONNES_PS)
    manquantes = OBLIGATOIRES - cols.keys()
    if manquantes:
        sys.exit(f"Colonnes introuvables dans l'Annuaire Santé : {manquantes}")

    flux = ouvrir_fichier_ps(source, "Personne_activite")
    morceaux = []
    lecteur = pd.read_csv(flux, sep="|", dtype=str, usecols=list(cols.values()),
                          quoting=3, chunksize=500_000, on_bad_lines="skip", keep_default_na=False)
    for i, bloc in enumerate(lecteur, 1):
        bloc = bloc.rename(columns={v: k for k, v in cols.items()})
        bloc["dep"] = [S.departement_de(cc, cp) for cc, cp in zip(bloc.cc, bloc.cp)]
        garde = (bloc.voie.str.strip() != "") | (bloc.finess.str.strip() != "")
        if departements:
            garde &= bloc.dep.isin(departements) | bloc.finess.str[:2].isin(departements)
        morceaux.append(bloc[garde])
        print(f"\r  {i * 500_000:,} lignes lues".replace(",", " "), end="", flush=True)
    print()
    ps = pd.concat(morceaux, ignore_index=True)
    for c in COLONNES_PS:
        if c not in ps.columns:
            ps[c] = ""

    # Spécialités : dans le fichier principal ou, à défaut, dans le fichier SavoirFaire
    if (ps.sf.str.strip() == "").all():
        flux_sf = ouvrir_fichier_ps(source, "SavoirFaire")
        if flux_sf is not None:
            entete_sf = flux_sf.readline().rstrip("\n").rstrip("|").split("|")
            flux_sf.close()
            c_sf = trouver_colonnes(entete_sf, {"id": ["identifiantpp"], "sf": ["libellesavoirfaire"]})
            if len(c_sf) == 2:
                sf = pd.read_csv(ouvrir_fichier_ps(source, "SavoirFaire"), sep="|", dtype=str,
                                 quoting=3, usecols=list(c_sf.values()), on_bad_lines="skip")
                sf = sf.rename(columns={v: k for k, v in c_sf.items()})
                sf = sf[sf.id.isin(ps.id)].dropna().drop_duplicates("id")
                ps = ps.drop(columns="sf").merge(sf, on="id", how="left")
                ps["sf"] = ps.sf.fillna("")
    return ps


def nettoyer_specialite(sf: str) -> str:
    sf = re.sub(r"\s*\(.*?\)\s*", "", sf)
    sf = re.sub(r"^(Qualifi[ée] en|Sp[ée]cialiste en)\s+", "", sf, flags=re.I)
    return sf.strip().capitalize()


def libelle_specialite(profession: str, sf: str) -> str:
    prof = (profession or "").strip()
    sf = (sf or "").strip()
    if "medecin" in S.sans_accents(prof).lower() and sf:
        return f"Médecin – {nettoyer_specialite(sf)}"
    return prof or "Autre"


def libelle_mode(code: str, libelle: str) -> str:
    code = (code or "").strip().upper()
    if code in MODES:
        return MODES[code]
    lib = S.sans_accents(libelle or "").lower()
    if "liberal" in lib:
        return "Libéral"
    if "salari" in lib:
        return "Salarié"
    if "benevol" in lib:
        return "Bénévole"
    return "Non précisé"


# ---------------------------------------------------------------- lieux d'exercice
def construire_lieux(ps: pd.DataFrame, finess: dict, cadres: dict, geocodeur) -> pd.DataFrame:
    """Un lieu = un établissement FINESS aux coordonnées fiables, sinon une adresse.
    Ajoute à `ps` la colonne `lieu` et renvoie la table des lieux."""
    # 1. Rattachement au n° FINESS du site quand l'établissement a une position fiable
    ok_finess = {f for f, e in finess.items()
                 if S.dans_cadre(cadres, e["departement"], e["lat"], e["lon"])}
    par_finess = ps.finess.isin(ok_finess)
    ps["lieu"] = ""
    ps.loc[par_finess, "lieu"] = "F" + ps.loc[par_finess, "finess"]

    # 2. Les autres : par adresse. Si l'Annuaire n'a pas d'adresse mais un n° FINESS connu,
    #    on reprend l'adresse de l'établissement.
    reste = ps[~par_finess].copy()
    for i in reste.index[(reste.voie.str.strip() == "") & reste.finess.isin(finess)]:
        e = finess[reste.at[i, "finess"]]
        voie = e["voie"] or e["lieudit"] or e["adresse"]
        reste.loc[i, ["num", "typvoie", "voie", "cp", "cc", "dep", "commune"]] = [
            e["numvoie"] if e["voie"] else "", "", voie, e["cp"], e["code_commune"],
            e["departement"], e["acheminement"][len(e["cp"]):].strip()]
        if not reste.at[i, "raison"].strip():
            reste.at[i, "raison"] = e["nom"]
    # Sans voie, le lieu reste plaçable au centre de sa commune
    reste = reste[(reste.voie.str.strip() != "") | (reste.cc.str.strip() != "")]
    reste["adresse"] = (reste.num.str.strip() + reste.indice.str.strip().str.lower() + " "
                        + reste.typvoie.str.strip() + " " + reste.voie.str.strip()) \
        .str.replace(r"\s+", " ", regex=True).str.strip()
    reste["cle"] = (reste.cc.str.strip() + "|" + reste.num.map(S.norm_num) + "|"
                    + reste.voie.map(S.norm_voie))
    ps.loc[reste.index, "lieu"] = "A" + reste.cle

    # 3. Géocodage des adresses (avec cache)
    adresses = reste.drop_duplicates("cle")
    cache = geocodeur.geocoder(list(zip(adresses.cle, adresses.adresse, adresses.cc.str.strip())))
    centres = S.centres_communes()

    lignes, ecartes, approx = [], 0, 0
    for f in set(ps.loc[par_finess, "finess"]):
        e = finess[f]
        lignes.append({"lieu": "F" + f, "lat": e["lat"], "lon": e["lon"], "nom": e["nom"],
                       "adresse": e["adresse"] or e["lieudit"], "ville": e["acheminement"],
                       "tel": e["telephone"], "dep": e["departement"], "prec": ""})
    for r in adresses.itertuples():
        res = cache.get(r.cle) or {}
        lat = float(res["lat"]) if res.get("lat") else None
        lon = float(res["lon"]) if res.get("lon") else None
        # Précision affichée dans la fiche : "" numéro exact, "r" rue, "?" lieu-dit, "c" commune
        prec = {"housenumber": "", "street": "r", "locality": "?"}.get(res.get("type"), "c")
        if lat is not None and not S.dans_cadre(cadres, r.dep, lat, lon):
            lat = lon = None          # position aberrante (autre région, autre continent…)
            ecartes += 1
        if lat is None or prec == "c":
            centre = centres.get(r.cc.strip())
            if centre is None:
                continue
            lat, lon, prec = centre[0], centre[1], "c"
            approx += 1
        nom = r.enseigne.strip() or r.raison.strip()
        lignes.append({"lieu": "A" + r.cle, "lat": lat, "lon": lon, "nom": nom,
                       "adresse": r.adresse, "ville": f"{r.cp.strip()} {r.commune.strip()}".strip(),
                       "tel": r.tel.strip(), "dep": r.dep, "prec": prec})
    if ecartes:
        print(f"  {ecartes} position(s) aberrante(s) écartée(s)")
    if approx:
        print(f"  {approx} lieu(x) placé(s) au centre de leur commune")
    lieux = pd.DataFrame(lignes, columns=["lieu", "lat", "lon", "nom", "adresse", "ville",
                                          "tel", "dep", "prec"])
    return lieux[lieux.dep != ""]


# ---------------------------------------------------------------- sorties
def ecrire_sorties(lieux: pd.DataFrame, ps: pd.DataFrame, dossier: Path) -> dict:
    """Écrit la page (index.html) et un fichier de données par département (data/dep_XX.js).

    Format d'un fichier de département, lu par la page :
      l : lieux       [lat, lon, nom, adresse, ville, téléphone, précision]
      p : professionnels [n° du lieu dans l, nom, n° de spécialité, n° de mode]
    Précision : "" = adresse exacte ou établissement FINESS, "r" = rue,
                "c" = centre de la commune, "?" = approximative.
    """
    data = dossier / "data"
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)

    lieux = lieux.set_index("lieu")
    ps = ps[ps.lieu.isin(lieux.index)]
    specialites = Counter(ps.specialite)
    liste_spe = [s for s, _ in specialites.most_common()]
    idx_spe = {s: i for i, s in enumerate(liste_spe)}
    liste_modes = [m for m, _ in Counter(ps["mode"]).most_common()]
    idx_mode = {m: i for i, m in enumerate(liste_modes)}

    deps = {}
    for dep, groupe_lieux in lieux.groupby("dep"):
        pos = {l: i for i, l in enumerate(groupe_lieux.index)}
        pros = ps[ps.lieu.isin(pos)]
        if pros.empty:
            continue
        donnees = {
            "l": [[round(r.lat, 5), round(r.lon, 5), r.nom, r.adresse, r.ville, r.tel, r.prec]
                  for r in groupe_lieux.itertuples()],
            "p": [[pos[r.lieu], f"{r.prenom.strip()} {r.nom.strip()}".strip(),
                   idx_spe[r.specialite], idx_mode[r.mode]] for r in pros.itertuples()],
        }
        (data / f"dep_{dep}.js").write_text(
            "window.DONNEES_DEP = window.DONNEES_DEP || {};\n"
            f"DONNEES_DEP[{json.dumps(dep)}] = "
            f"{json.dumps(donnees, ensure_ascii=False, separators=(',', ':'))};\n",
            encoding="utf-8")
        deps[dep] = {"nom": S.NOMS_DEP.get(dep, dep),
                     "b": [round(groupe_lieux.lat.min(), 4), round(groupe_lieux.lon.min(), 4),
                           round(groupe_lieux.lat.max(), 4), round(groupe_lieux.lon.max(), 4)],
                     "n": len(pros)}

    ordre = sorted(deps, key=lambda d: (d[:2] in ("97", "98"),
                                        d.replace("2A", "20.1").replace("2B", "20.2").zfill(3)))
    html = (GABARIT_HTML
            .replace("__SPECIALITES__", json.dumps([[s, specialites[s]] for s in liste_spe], ensure_ascii=False))
            .replace("__MODES__", json.dumps(liste_modes, ensure_ascii=False))
            .replace("__DEPS__", json.dumps({d: deps[d] for d in ordre}, ensure_ascii=False))
            .replace("__ORDRE_DEPS__", json.dumps(ordre))
            .replace("__DATE__", S.date_du_jour()))
    (dossier / "index.html").write_text(html, encoding="utf-8")
    return deps


GABARIT_HTML = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Professionnels de santé en France</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.Default.min.css">
<style>
 :root{--fond:#fff;--texte:#1f2933;--doux:#6b7280;--accent:#0f766e;--bord:#e5e7eb}
 *{box-sizing:border-box} body{margin:0;font:14px/1.4 system-ui,sans-serif;color:var(--texte);background:var(--fond)}
 #app{display:flex;height:100vh} #panneau{width:350px;padding:16px;overflow-y:auto;border-right:1px solid var(--bord)}
 #carte{flex:1} h1{font-size:18px;margin:0 0 4px} .doux{color:var(--doux);font-size:12px}
 input[type=search],select{width:100%;padding:8px;border:1px solid var(--bord);border-radius:6px;font:inherit;background:var(--fond)}
 .bloc{margin:12px 0;padding:10px;border:1px solid var(--bord);border-radius:8px}
 .titre{font-weight:600;display:block;margin:0 0 6px}
 .ligne{display:flex;gap:8px;align-items:center;margin:6px 0} .ligne input[type=range]{flex:1} #rayonVal{min-width:52px;text-align:right}
 .spe{display:flex;justify-content:space-between;padding:3px 0;cursor:pointer} .spe span:last-child{color:var(--doux)}
 button{padding:6px 10px;border:1px solid var(--bord);background:#f9fafb;border-radius:6px;cursor:pointer;font:inherit}
 #go{width:100%;padding:10px;background:var(--accent);color:#fff;border:0;font-weight:600;margin:8px 0}
 #compteur{font-weight:600;color:var(--accent);margin:8px 0}
 #export{width:100%} #export:disabled{opacity:.5;cursor:default}
 #specialites{max-height:320px;overflow-y:auto;margin-top:8px}
 .popup h3{margin:0 0 4px;font-size:14px} .popup ul{margin:6px 0 0;padding-left:18px} .popup li{margin:3px 0}
 .popup a{color:var(--accent);text-decoration:none;font-weight:600}
 @media(max-width:700px){#app{flex-direction:column} #panneau{width:100%;height:45vh;border-right:0;border-bottom:1px solid var(--bord)}}
</style></head><body><div id="app">
<div id="panneau">
 <h1>Professionnels de santé</h1>
 <div class="doux">Sources : Annuaire Santé (ANS), FINESS, géocodage IGN. Données mises à jour le __DATE__.</div>
 <div class="bloc">
  <label class="titre" for="dep">Département</label>
  <select id="dep"><option value="">Toute la France</option></select>
  <label class="titre" for="lieu" style="margin-top:10px">Autour d'une ville</label>
  <input type="search" id="lieu" placeholder="Ville ou code postal (ex. Angers)">
  <div class="ligne"><input type="range" id="rayon" min="1" max="100" value="5" aria-label="Rayon de recherche en kilomètres"><span id="rayonVal">5 km</span></div>
  <div id="lieuInfo" class="doux"></div>
 </div>
 <input type="search" id="recherche" placeholder="Nom d'un professionnel ou d'un lieu…">
 <label class="spe" style="margin-top:8px"><span><input type="checkbox" id="tout"> Afficher tous les résultats (peut être long)</span></label>
 <button id="go">Rechercher</button>
 <div id="compteur"></div>
 <button id="export" disabled>Exporter les résultats (CSV)</button>
 <div class="bloc">
  <span class="titre">Mode d'exercice</span>
  <div id="modes"></div>
 </div>
 <div class="bloc">
  <span class="titre">Professions et spécialités</span>
  <input type="search" id="filtreSpe" placeholder="Rechercher une spécialité (ex. pédiatrie)…">
  <p style="margin:8px 0 0"><button id="aucune">Tout décocher</button></p>
  <div id="specialites"></div>
 </div>
</div>
<div id="carte"></div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.min.js"></script>
<script>
const SPECIALITES = __SPECIALITES__;   // [[libellé, nombre de professionnels], …]
const MODES = __MODES__;               // ["Libéral", "Salarié", …]
const DEPS = __DEPS__;                 // {code: {nom, b: [sud, ouest, nord, est], n}}
const ORDRE_DEPS = __ORDRE_DEPS__;     // ordre de la liste : 01, 02… 2A, 2B… 971…
window.DONNEES_DEP = window.DONNEES_DEP || {};   // rempli par les fichiers data/dep_XX.js

const carte = L.map('carte').setView([46.6, 2.4], 6);
const planIGN = L.tileLayer('https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0' +
  '&LAYER=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2&STYLE=normal&TILEMATRIXSET=PM&FORMAT=image/png' +
  '&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}',
  {maxZoom: 19, attribution: '&copy; IGN – Géoplateforme'}).addTo(carte);
const carto = L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
  {maxZoom: 19, subdomains: 'abcd', attribution: '&copy; OpenStreetMap, &copy; CARTO'});
L.control.layers({'Plan IGN': planIGN, 'CARTO Voyager': carto}).addTo(carte);
const groupe = L.markerClusterGroup({chunkedLoading: true}).addTo(carte);

const $ = id => document.getElementById(id);
const echap = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sansAccents = s => String(s).normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const fr = n => n.toLocaleString('fr-FR');

// --- Listes du panneau
ORDRE_DEPS.forEach(code => {
  const d = DEPS[code], o = document.createElement('option');
  o.value = code; o.textContent = `${code} – ${d.nom} (${fr(d.n)})`;
  $('dep').appendChild(o);
});
MODES.forEach((m, i) => {
  const l = document.createElement('label'); l.className = 'spe';
  l.innerHTML = `<span><input type="checkbox" value="${i}"> ${echap(m)}</span>`;
  $('modes').appendChild(l);
});
SPECIALITES.forEach(([nom, n], i) => {
  const l = document.createElement('label'); l.className = 'spe';
  l.dataset.cle = sansAccents(nom);
  l.innerHTML = `<span><input type="checkbox" value="${i}"> ${echap(nom)}</span><span>${fr(n)}</span>`;
  $('specialites').appendChild(l);
});
$('filtreSpe').addEventListener('input', e => {
  const q = sansAccents(e.target.value.trim());
  $('specialites').querySelectorAll('label.spe').forEach(l => {
    l.style.display = !q || l.dataset.cle.includes(q) ? '' : 'none';
  });
});

// --- Chargement d'un département à la demande (balise <script>, compatible ouverture en local)
const chargements = {};
function chargerDep(code) {
  if (DONNEES_DEP[code]) return Promise.resolve();
  if (!chargements[code]) chargements[code] = new Promise((ok, ko) => {
    const s = document.createElement('script');
    s.src = `data/dep_${code}.js`; s.onload = ok; s.onerror = () => ko(new Error(code));
    document.head.appendChild(s);
  });
  return chargements[code];
}

// --- Ville et rayon
let centre = null, cercle = null, villeGeocodee = '';
const distanceKm = (a, b) => carte.distance(a, b) / 1000;
async function geocoderVille(q) {
  const url = 'https://data.geopf.fr/geocodage/search?type=municipality&limit=1&q=' + encodeURIComponent(q);
  const rep = await (await fetch(url)).json();
  const f = rep.features && rep.features[0];
  if (!f) return null;
  const [lon, lat] = f.geometry.coordinates;
  return {latlng: L.latLng(lat, lon), label: `${f.properties.label} (${f.properties.postcode})`};
}
function intersecte(b, c, r) {   // le cercle touche-t-il le rectangle du département ?
  const dLat = r / 111, dLon = r / (111 * Math.cos(c.lat * Math.PI / 180));
  return !(c.lat - dLat > b[2] || c.lat + dLat < b[0] || c.lng - dLon > b[3] || c.lng + dLon < b[1]);
}

// --- Fiche d'un lieu
const PRECISION = {r: 'Position au niveau de la rue', c: 'Position approximative (centre de la commune)',
                   '?': 'Position approximative'};
function lienDoctolib(p, l) {
  const cp = (String(l[4]).match(/^\d{5}/) || [''])[0];
  const commune = String(l[4]).replace(/^\d{5}\s*/, '');
  const spe = SPECIALITES[p[2]][0].replace(/^Médecin – /, '');
  return 'https://www.google.com/search?q=' + encodeURIComponent(`Doctolib ${p[1]} ${spe} ${commune} ${cp}`);
}
function fiche(l, liste) {
  const tri = [...liste].sort((a, b) =>
    SPECIALITES[a[2]][0].localeCompare(SPECIALITES[b[2]][0]) || a[1].localeCompare(b[1]));
  const lignes = tri.slice(0, 100).map(p =>
    `<li><b>${echap(p[1])}</b> – ${echap(SPECIALITES[p[2]][0])} <span class="doux">(${echap(MODES[p[3]])})</span>` +
    ` <a href="${lienDoctolib(p, l)}" target="_blank" rel="noopener"` +
    ` title="Recherche web : le professionnel n'est pas forcément sur Doctolib">Rechercher sur Doctolib ↗</a></li>`).join('');
  const reste = tri.length > 100 ? `<li class="doux">… et ${fr(tri.length - 100)} autres</li>` : '';
  const prec = PRECISION[l[6]] ? `<br><span class="doux">${PRECISION[l[6]]}</span>` : '';
  return `<div class="popup"><h3>${echap(l[2] || 'Lieu d’exercice')}</h3>${echap(l[3])}<br>${echap(l[4])}` +
    (l[5] ? `<br>Tél. ${echap(l[5])}` : '') + prec +
    `<div class="doux" style="margin-top:6px">${fr(tri.length)} professionnel(s)</div><ul>${lignes}${reste}</ul></div>`;
}

// --- Export CSV des résultats affichés (séparateur ";" et BOM pour Excel en français)
let resultats = [];   // [[professionnel, lieu], …] de la dernière recherche
function exporterCSV() {
  const champ = v => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const lignes = [['Nom', 'Profession / spécialité', "Mode d'exercice", 'Lieu', 'Adresse', 'Ville',
                   'Téléphone', 'Latitude', 'Longitude']];
  resultats.forEach(([p, l]) => lignes.push([p[1], SPECIALITES[p[2]][0], MODES[p[3]],
                                             l[2], l[3], l[4], l[5], l[0], l[1]]));
  const csv = '\ufeff' + lignes.map(r => r.map(champ).join(';')).join('\r\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
  a.download = 'professionnels_sante.csv';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// --- Recherche
let numero = 0;
async function rechercher() {
  const n = ++numero;   // pour ignorer une recherche dépassée par une plus récente
  const statut = $('compteur');
  const dep = $('dep').value;
  const qVille = $('lieu').value.trim();
  const q = sansAccents($('recherche').value.trim());
  const spe = new Set([...$('specialites').querySelectorAll('input:checked')].map(i => Number(i.value)));
  const modes = new Set([...$('modes').querySelectorAll('input:checked')].map(i => Number(i.value)));
  const tout = $('tout').checked;
  const r = Number($('rayon').value);

  if (qVille && qVille !== villeGeocodee) {
    statut.textContent = 'Recherche de la ville…';
    try {
      const g = await geocoderVille(qVille);
      if (n !== numero) return;
      villeGeocodee = qVille;
      centre = g ? g.latlng : null;
      $('lieuInfo').textContent = g ? g.label : 'Ville introuvable.';
    } catch (e) {
      $('lieuInfo').textContent = 'Service de géocodage injoignable.';
      centre = null;
    }
  } else if (!qVille) {
    centre = null; villeGeocodee = ''; $('lieuInfo').textContent = '';
  }

  // Rien n'est affiché tant qu'aucun critère n'est saisi (sauf « afficher tout »)
  if (!(dep || centre || q || spe.size) && !tout) {
    groupe.clearLayers();
    if (cercle) { carte.removeLayer(cercle); cercle = null; }
    resultats = []; $('export').disabled = true;
    statut.textContent = 'Choisis un département, une ville, un nom ou une spécialité, puis lance la recherche.';
    return;
  }

  // Départements à charger
  let codes = ORDRE_DEPS.slice();
  if (dep) codes = [dep];
  else if (centre) codes = codes.filter(c => intersecte(DEPS[c].b, centre, r));
  let faits = 0;
  statut.textContent = `Chargement des données (0/${codes.length})…`;
  await Promise.all(codes.map(c => chargerDep(c).catch(() => null).then(() => {
    faits++;
    if (n === numero) statut.textContent = `Chargement des données (${faits}/${codes.length})…`;
  })));
  if (n !== numero) return;
  statut.textContent = 'Filtrage…';
  await new Promise(ok => setTimeout(ok, 0));   // laisse le navigateur afficher le message

  // Filtrage des professionnels, regroupés par lieu
  const marqueurs = [], points = [];
  let nbPros = 0;
  resultats = [];
  for (const c of codes) {
    const D = DONNEES_DEP[c];
    if (!D) continue;
    if (!D.txtL) {   // textes de recherche préparés une seule fois par département
      D.txtL = D.l.map(l => sansAccents(`${l[2]} ${l[3]} ${l[4]}`));
      D.txtP = D.p.map(p => sansAccents(p[1]));
    }
    const dansZone = centre ? D.l.map(l => distanceKm(centre, [l[0], l[1]]) <= r) : null;
    const parLieu = new Map();
    D.p.forEach((p, i) => {
      if (dansZone && !dansZone[p[0]]) return;
      if (spe.size && !spe.has(p[2])) return;
      if (modes.size && !modes.has(p[3])) return;
      if (q && !D.txtP[i].includes(q) && !D.txtL[p[0]].includes(q)) return;
      if (!parLieu.has(p[0])) parLieu.set(p[0], []);
      parLieu.get(p[0]).push(p);
    });
    parLieu.forEach((liste, li) => {
      const l = D.l[li];
      const m = L.marker([l[0], l[1]]).bindPopup(() => fiche(l, liste), {maxHeight: 320, maxWidth: 360});
      marqueurs.push(m); points.push([l[0], l[1]]);
      nbPros += liste.length;
      liste.forEach(p => resultats.push([p, l]));
    });
  }
  groupe.clearLayers();
  groupe.addLayers(marqueurs);
  statut.textContent = marqueurs.length
    ? `${fr(nbPros)} professionnel(s) dans ${fr(marqueurs.length)} lieu(x)` + (centre ? ` · rayon ${r} km` : '')
    : 'Aucun résultat : élargis le rayon, change de département ou retire un filtre.';
  $('export').disabled = !resultats.length;

  // Cercle et recadrage
  if (centre) {
    if (cercle) cercle.setLatLng(centre).setRadius(r * 1000);
    else cercle = L.circle(centre, {radius: r * 1000, color: '#0f766e', weight: 2, fillOpacity: 0.06}).addTo(carte);
    carte.fitBounds(cercle.getBounds(), {padding: [20, 20]});
  } else {
    if (cercle) { carte.removeLayer(cercle); cercle = null; }
    if (points.length) carte.fitBounds(L.latLngBounds(points), {padding: [30, 30], maxZoom: 14});
  }
}

// --- Déclencheurs : bouton, Entrée, et mise à jour automatique des filtres
$('go').onclick = rechercher;
$('export').onclick = exporterCSV;
['lieu', 'recherche'].forEach(id => $(id).addEventListener('keydown', e => { if (e.key === 'Enter') rechercher(); }));
let minuteur;
const relancer = () => { clearTimeout(minuteur); minuteur = setTimeout(rechercher, 400); };
$('rayon').addEventListener('input', () => { $('rayonVal').textContent = $('rayon').value + ' km'; if (centre) relancer(); });
['dep', 'tout', 'modes', 'specialites'].forEach(id => $(id).addEventListener('change', relancer));
$('aucune').onclick = () => { $('specialites').querySelectorAll('input').forEach(i => i.checked = false); relancer(); };
rechercher();
</script></body></html>
"""


# ---------------------------------------------------------------- programme
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--departements", default="")
    p.add_argument("--finess", type=Path)
    p.add_argument("--ps", type=Path)
    p.add_argument("--sortie", type=Path, default=Path("carte_professionnels"))
    p.add_argument("--geocodage-minutes", type=float, default=None)
    args = p.parse_args()
    S.DOSSIER_DONNEES.mkdir(exist_ok=True)
    departements = {d.strip().upper() for d in args.departements.split(",") if d.strip()}

    print("1/5 FINESS")
    etabs = S.lire_etablissements_finess_plus(args.finess or S.recuperer_finess_plus())
    finess = {e["finess"]: e for e in etabs if e["finess"]}
    cadres = S.cadres_departements((e["departement"], e["lat"], e["lon"]) for e in etabs)
    print(f"  {len(finess):,} établissements actifs".replace(",", " "))

    print("2/5 Annuaire Santé (fichier volumineux, quelques minutes)")
    ps = lire_ps(args.ps or S.recuperer_annuaire_sante(), departements)
    ps["specialite"] = [libelle_specialite(a, b) for a, b in zip(ps.profession, ps.sf)]
    ps["mode"] = [libelle_mode(a, b) for a, b in zip(ps.mode_code, ps.mode_lib)]
    print(f"  {len(ps):,} lignes d'activité".replace(",", " "))

    print("3/5 Lieux d'exercice et géocodage")
    geocodeur = S.Geocodeur(minutes_max=args.geocodage_minutes)
    lieux = construire_lieux(ps, finess, cadres, geocodeur)
    if departements:
        lieux = lieux[lieux.dep.isin(departements)]
    ps = ps.drop_duplicates(["id", "lieu", "specialite"])
    repartition = Counter(ps["mode"])
    print("  modes d'exercice : " + ", ".join(f"{m} {fr_n(n)}" for m, n in repartition.most_common()))

    print("4/5 Regroupement par département")
    print("5/5 Écriture de la carte")
    deps = ecrire_sorties(lieux, ps, args.sortie)
    total = sum(d["n"] for d in deps.values())
    print(f"  {len(deps)} département(s), {fr_n(total)} professionnels placés → "
          f"{(args.sortie / 'index.html').resolve()}")


def fr_n(n: int) -> str:
    return f"{n:,}".replace(",", " ")


if __name__ == "__main__":
    main()
