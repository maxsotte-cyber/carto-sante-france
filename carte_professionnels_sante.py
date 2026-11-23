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
                       "tel": e["telephone"], "dep": e["departement"], "prec": "finess"})
    for r in adresses.itertuples():
        res = cache.get(r.cle) or {}
        lat = float(res["lat"]) if res.get("lat") else None
        lon = float(res["lon"]) if res.get("lon") else None
        prec = "adresse" if res.get("type") in ("housenumber", "street") else "commune"
        if lat is not None and not S.dans_cadre(cadres, r.dep, lat, lon):
            lat = lon = None          # position aberrante (autre région, autre continent…)
            ecartes += 1
        if lat is None or prec == "commune":
            centre = centres.get(r.cc.strip())
            if centre is None:
                continue
            lat, lon, prec = centre[0], centre[1], "commune"
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
    data = dossier / "data"
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)

    specialites = Counter(ps.specialite)
    liste_spe = [s for s, _ in specialites.most_common()]
    idx_spe = {s: i for i, s in enumerate(liste_spe)}
    liste_modes = ["Libéral", "Salarié", "Bénévole", "Non précisé"]
    idx_mode = {m: i for i, m in enumerate(liste_modes)}
    lieux = lieux.set_index("lieu")
    ps = ps[ps.lieu.isin(lieux.index)]

    deps = {}
    for dep, groupe_lieux in lieux.groupby("dep"):
        ids = list(groupe_lieux.index)
        pos = {l: i for i, l in enumerate(ids)}
        pros = ps[ps.lieu.isin(pos)]
        if pros.empty:
            continue
        donnees = {
            "lieux": [[round(r.lat, 5), round(r.lon, 5), r.nom, r.adresse, r.ville, r.tel,
                       1 if r.prec == "commune" else 0] for r in groupe_lieux.itertuples()],
            "pros": [[pos[r.lieu], r.nom.strip(), r.prenom.strip(), idx_spe[r.specialite],
                      idx_mode[r.mode]] for r in pros.itertuples()],
        }
        (data / f"dep_{dep}.js").write_text(
            f"chargerDepartement({json.dumps(dep)},{json.dumps(donnees, ensure_ascii=False, separators=(',', ':'))});",
            encoding="utf-8")
        deps[dep] = {"nom": S.NOMS_DEP.get(dep, dep), "n": len(pros),
                     "cadre": [round(groupe_lieux.lat.min(), 3), round(groupe_lieux.lat.max(), 3),
                               round(groupe_lieux.lon.min(), 3), round(groupe_lieux.lon.max(), 3)]}

    ordre = sorted(deps, key=lambda d: (d.startswith("97"), d.replace("2A", "20.1").replace("2B", "20.2").zfill(3)))
    html = (GABARIT_HTML
            .replace("__DEPS__", json.dumps({d: deps[d] for d in ordre}, ensure_ascii=False))
            .replace("__SPECIALITES__", json.dumps([[s, specialites[s]] for s in liste_spe], ensure_ascii=False))
            .replace("__ORDRE__", json.dumps(ordre))
            .replace("__MODES__", json.dumps(liste_modes, ensure_ascii=False))
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
 label.titre{display:block;font-weight:600;margin:12px 0 4px}
 input[type=search],input[type=text],select{width:100%;padding:7px;border:1px solid var(--bord);border-radius:6px;font:inherit}
 .ligne{display:flex;gap:8px;align-items:center} .ligne input[type=range]{flex:1}
 #specialites{max-height:220px;overflow-y:auto;border:1px solid var(--bord);border-radius:6px;padding:4px 8px;margin-top:6px}
 .spe{display:flex;justify-content:space-between;padding:2px 0;cursor:pointer} .spe span:last-child{color:var(--doux)}
 #modes label{margin-right:10px;white-space:nowrap}
 button{padding:7px 12px;border:1px solid var(--bord);background:#f9fafb;border-radius:6px;cursor:pointer;font:inherit}
 button.principal{background:var(--accent);color:#fff;border-color:var(--accent);width:100%;margin-top:12px}
 button:disabled{opacity:.5;cursor:default}
 #compteur{font-weight:600;color:var(--accent);margin:10px 0 4px} #message{color:var(--doux);margin:4px 0}
 .popup h3{margin:0 0 4px;font-size:14px} .popup ul{margin:6px 0 0;padding-left:18px;max-height:220px;overflow-y:auto}
 .popup li{margin-bottom:3px} .approx{color:#b45309;font-size:12px}
 @media(max-width:700px){#app{flex-direction:column} #panneau{width:100%;height:45vh;border-right:0;border-bottom:1px solid var(--bord)}}
</style></head><body><div id="app">
<div id="panneau">
 <h1>Professionnels de santé</h1>
 <div class="doux">Sources : Annuaire Santé et FINESS (ANS), géocodage IGN. Données mises à jour le __DATE__.</div>

 <label class="titre" for="dep">Département</label>
 <select id="dep"><option value="">— Tous —</option></select>

 <label class="titre" for="ville">Autour d'une ville</label>
 <input type="text" id="ville" placeholder="Ex. : Angers">
 <div class="ligne"><input type="range" id="rayon" min="1" max="100" value="10" aria-label="Rayon de recherche en kilomètres"><span id="rayonTxt">10 km</span></div>

 <label class="titre" for="nom">Nom d'un professionnel ou d'un lieu</label>
 <input type="search" id="nom" placeholder="Ex. : Dupont, clinique…">

 <label class="titre" for="filtreSpe">Spécialités</label>
 <input type="search" id="filtreSpe" placeholder="Filtrer la liste…">
 <div id="specialites"></div>

 <label class="titre">Mode d'exercice</label>
 <div id="modes"></div>

 <p><label><input type="checkbox" id="tout"> Afficher tous les résultats (lent à l'échelle de la France)</label></p>
 <button class="principal" id="go">Rechercher</button>
 <div id="compteur"></div>
 <div id="message">Choisis un département, une ville, un nom ou une spécialité, puis lance la recherche.</div>
 <p><button id="export" disabled>Exporter les résultats (CSV)</button></p>
</div>
<div id="carte"></div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.min.js"></script>
<script>
const DEPS = __DEPS__;
const ORDRE_DEPS = __ORDRE__;   // ordre d'affichage (01, 02… 2A, 2B… 971…)
const SPECIALITES = __SPECIALITES__;
const MODES = __MODES__;
const $ = id => document.getElementById(id);
const fr = n => n.toLocaleString('fr-FR');
const echap = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const minus = s => String(s ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

// ---- carte
const carte = L.map('carte').setView([46.6, 2.4], 6);
const planIGN = L.tileLayer('https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0' +
  '&LAYER=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2&STYLE=normal&TILEMATRIXSET=PM&FORMAT=image/png' +
  '&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}', {maxZoom: 19, attribution: '&copy; IGN – Géoplateforme'}).addTo(carte);
const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 19, attribution: '&copy; OpenStreetMap'});
L.control.layers({'Plan IGN': planIGN, 'OpenStreetMap': osm}).addTo(carte);
const groupe = L.markerClusterGroup({chunkedLoading: true}).addTo(carte);
let cercle = null;

// ---- panneau
ORDRE_DEPS.forEach(code => {
  const d = DEPS[code], o = document.createElement('option'); o.value = code;
  o.textContent = `${code} – ${d.nom} (${fr(d.n)})`; $('dep').appendChild(o);
});
SPECIALITES.forEach(([nom, n], i) => {
  const l = document.createElement('label'); l.className = 'spe';
  l.innerHTML = `<span><input type="checkbox" value="${i}"> ${echap(nom)}</span><span>${fr(n)}</span>`;
  l.dataset.texte = minus(nom); $('specialites').appendChild(l);
});
MODES.forEach((m, i) => {
  const l = document.createElement('label');
  l.innerHTML = `<input type="checkbox" value="${i}" checked> ${echap(m)}`; $('modes').appendChild(l);
});
$('filtreSpe').addEventListener('input', () => {
  const q = minus($('filtreSpe').value.trim());
  $('specialites').querySelectorAll('label').forEach(l => l.style.display = l.dataset.texte.includes(q) ? '' : 'none');
});
$('rayon').addEventListener('input', () => $('rayonTxt').textContent = $('rayon').value + ' km');

// ---- chargement des données, un fichier par département
const DONNEES = {};
const enAttente = {};
function chargerDepartement(code, d) { DONNEES[code] = d; (enAttente[code] || []).forEach(f => f()); }
function charger(code) {
  if (DONNEES[code]) return Promise.resolve();
  return new Promise((ok, ko) => {
    (enAttente[code] = enAttente[code] || []).push(ok);
    if (enAttente[code].length > 1) return;
    const s = document.createElement('script'); s.src = `data/dep_${code}.js`;
    s.onerror = () => ko(new Error('fichier ' + code)); document.head.appendChild(s);
  });
}

function distanceKm(a, b, c, d) {
  const r = Math.PI / 180, x = Math.sin((c - a) * r / 2) ** 2 +
    Math.cos(a * r) * Math.cos(c * r) * Math.sin((d - b) * r / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(x));
}
function cadreTouche(cadre, lat, lon, km) {
  const dLat = km / 111, dLon = km / (111 * Math.cos(lat * Math.PI / 180));
  return lat + dLat >= cadre[0] && lat - dLat <= cadre[1] && lon + dLon >= cadre[2] && lon - dLon <= cadre[3];
}
async function geocoderVille(texte) {
  const url = 'https://data.geopf.fr/geocodage/search?index=address&type=municipality&limit=1&q=' + encodeURIComponent(texte);
  const r = await fetch(url); const j = await r.json();
  if (!j.features || !j.features.length) return null;
  const [lon, lat] = j.features[0].geometry.coordinates;
  return {lat, lon, nom: j.features[0].properties.city || j.features[0].properties.label};
}
function lienDoctolib(p, lieu) {
  const q = ['Doctolib', p.prenom, p.nom, SPECIALITES[p.spe][0], lieu[4]].filter(Boolean).join(' ');
  return 'https://www.google.com/search?q=' + encodeURIComponent(q);
}

let resultats = [];
async function rechercher() {
  const dep = $('dep').value, ville = $('ville').value.trim(), nom = minus($('nom').value.trim());
  const spes = new Set([...$('specialites').querySelectorAll('input:checked')].map(i => +i.value));
  const modes = new Set([...$('modes').querySelectorAll('input:checked')].map(i => +i.value));
  const tout = $('tout').checked, km = +$('rayon').value;
  if (!dep && !ville && !nom && !spes.size && !tout) {
    $('message').textContent = 'Choisis un département, une ville, un nom ou une spécialité, puis lance la recherche.';
    return;
  }
  $('go').disabled = true;
  try {
    let centre = null;
    if (ville) {
      $('message').textContent = 'Recherche de la ville…';
      centre = await geocoderVille(ville);
      if (!centre) { $('message').textContent = `Ville « ${ville} » introuvable.`; return; }
    }
    let codes = dep ? [dep] : ORDRE_DEPS.slice();
    if (centre) codes = codes.filter(c => cadreTouche(DEPS[c].cadre, centre.lat, centre.lon, km));
    for (let i = 0; i < codes.length; i++) {
      if (!DONNEES[codes[i]]) $('message').textContent = `Chargement des données : ${i + 1} / ${codes.length}…`;
      await charger(codes[i]);
    }
    resultats = [];
    const parLieu = new Map();
    for (const code of codes) {
      const d = DONNEES[code];
      const lieuxOk = d.lieux.map(l => !centre || distanceKm(centre.lat, centre.lon, l[0], l[1]) <= km);
      for (const p of d.pros) {
        const [li, pnom, pprenom, spe, mode] = p;
        if (!lieuxOk[li] || !modes.has(mode)) continue;
        if (spes.size && !spes.has(spe)) continue;
        const lieu = d.lieux[li];
        if (nom && !minus(pnom + ' ' + pprenom + ' ' + lieu[2]).includes(nom)) continue;
        const cle = code + ':' + li;
        if (!parLieu.has(cle)) parLieu.set(cle, {lieu, pros: []});
        const pro = {nom: pnom, prenom: pprenom, spe, mode};
        parLieu.get(cle).pros.push(pro); resultats.push({pro, lieu});
      }
    }
    afficher(parLieu, centre, km);
  } catch (e) {
    $('message').textContent = 'Erreur pendant la recherche : ' + e.message;
  } finally { $('go').disabled = false; }
}

function afficher(parLieu, centre, km) {
  groupe.clearLayers();
  if (cercle) { carte.removeLayer(cercle); cercle = null; }
  const marqueurs = [];
  parLieu.forEach(({lieu, pros}) => {
    const liste = pros.map(p => `<li><b>${echap(p.prenom)} ${echap(p.nom)}</b><br>${echap(SPECIALITES[p.spe][0])} · ${echap(MODES[p.mode])}` +
      `<br><a href="${lienDoctolib(p, lieu)}" target="_blank" rel="noopener" title="Recherche web : le professionnel n'est pas forcément sur Doctolib">Rechercher sur Doctolib ↗</a></li>`).join('');
    marqueurs.push(L.marker([lieu[0], lieu[1]]).bindPopup(
      `<div class="popup"><h3>${echap(lieu[2] || lieu[3])}</h3>${lieu[2] ? echap(lieu[3]) + '<br>' : ''}${echap(lieu[4])}` +
      (lieu[5] ? `<br>Tél. ${echap(lieu[5])}` : '') +
      (lieu[6] ? `<br><span class="approx">Position approximative (centre de la commune)</span>` : '') +
      `<ul>${liste}</ul></div>`, {maxWidth: 320}));
  });
  groupe.addLayers(marqueurs);
  $('compteur').textContent = `${fr(resultats.length)} professionnel(s) · ${fr(parLieu.size)} lieu(x)`;
  $('export').disabled = !resultats.length;
  if (!resultats.length) {
    $('message').textContent = 'Aucun résultat : élargis le rayon, change de département ou retire un filtre.';
  } else {
    $('message').textContent = '';
  }
  if (centre) {
    cercle = L.circle([centre.lat, centre.lon], {radius: km * 1000, color: '#0f766e', fill: false}).addTo(carte);
    carte.fitBounds(cercle.getBounds());
  } else if (marqueurs.length) {
    carte.fitBounds(groupe.getBounds(), {maxZoom: 15});
  }
}

function exporter() {
  const champ = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
  const lignes = [['Nom', 'Prénom', 'Spécialité', "Mode d'exercice", 'Lieu', 'Adresse', 'Commune', 'Téléphone', 'Latitude', 'Longitude']];
  resultats.forEach(({pro, lieu}) => lignes.push([pro.nom, pro.prenom, SPECIALITES[pro.spe][0], MODES[pro.mode],
    lieu[2], lieu[3], lieu[4], lieu[5], lieu[0], lieu[1]]));
  const csv = '﻿' + lignes.map(l => l.map(champ).join(';')).join('\r\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
  a.download = 'professionnels_sante.csv'; a.click();
}

$('go').onclick = rechercher;
['ville', 'nom'].forEach(id => $(id).addEventListener('keydown', e => { if (e.key === 'Enter') rechercher(); }));
$('export').onclick = exporter;
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
