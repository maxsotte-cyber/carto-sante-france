# Cartographie de l'offre de santé en France

Deux cartes interactives construites à partir de données ouvertes. Elles se mettent à jour toutes seules deux fois par semaine.

**Voir les cartes en ligne : https://maxsotte-cyber.github.io/carto-sante-france/**

| Carte | Ce qu'elle montre |
|---|---|
| **Maisons de santé** | Les maisons de santé pluriprofessionnelles (MSP) de France, filtrables selon les spécialités présentes : au moins une des spécialités cochées, ou toutes. Recherche par nom, ville ou code postal. |
| **Professionnels de santé** | Tous les professionnels de l'Annuaire Santé, regroupés par lieu d'exercice. Recherche par département, autour d'une ville (rayon de 1 à 100 km), par nom ou par spécialité. Filtre par mode d'exercice (libéral, salarié, bénévole), lien de recherche Doctolib et export CSV des résultats. |

## Sources

| Source | Producteur | Utilisation |
|---|---|---|
| [FINESS – Structures (FINESS+)](https://www.data.gouv.fr/datasets/69fb10fd3d405934d1077e01) | Agence du Numérique en Santé | Établissements (catégorie, adresse, coordonnées). Fichier JSON publié chaque jour. |
| [Annuaire Santé – extractions en libre accès](https://www.data.gouv.fr/datasets/69025e6c73d1f9b79ca3c365) | Agence du Numérique en Santé | Professionnels, profession, spécialité, mode d'exercice, lieu d'exercice. |
| [Géocodage de la Géoplateforme](https://www.data.gouv.fr/dataservices/api-geoplateforme-geocodage) | IGN | Coordonnées des cabinets qui n'ont qu'une adresse. |
| [API Découpage administratif](https://geo.api.gouv.fr/decoupage-administratif) | État (DINUM) | Centre des communes, pour les adresses introuvables. |

Les données sont sous Licence Ouverte 2.0.

## Méthode

1. **Établissements.** Le fichier FINESS+ est lu au fil de l'eau, sans être chargé entier en mémoire. Seuls les établissements actifs sont conservés. Environ un quart d'entre eux n'a pas encore de coordonnées dans FINESS+ : leur adresse est alors géocodée.
2. **Professionnels.** Chaque ligne d'activité de l'Annuaire Santé est rattachée à un lieu :
   - à l'établissement FINESS déclaré, quand sa position est fiable ;
   - sinon à son adresse, géocodée par l'IGN.

   Pour les maisons de santé, un professionnel est rattaché à la MSP s'il déclare son numéro FINESS ou s'il exerce à la même adresse. Les variantes d'écriture (« 5 bis », accents, tirets) sont neutralisées.
3. **Contrôle des positions.** Pour chaque département, un cadre de référence est calculé à partir des coordonnées FINESS, en écartant les valeurs extrêmes. Un point qui tombe en dehors est rejeté, et le lieu est replacé au centre de sa commune. Sa fiche indique alors « position approximative ».
4. **Spécialités.** Les médecins sont détaillés par spécialité (« Médecin – Pédiatrie »). Les autres professions apparaissent telles quelles.
5. **Publication.** La carte des professionnels est découpée en un fichier par département. La page ne charge que ceux dont la recherche a besoin.

## Mise à jour automatique

Le fichier [`.github/workflows/mise-a-jour.yml`](.github/workflows/mise-a-jour.yml) lance les deux scripts sur les serveurs de GitHub (GitHub Actions) **le lundi et le jeudi**, puis publie les cartes sur GitHub Pages. Aucun ordinateur n'a besoin d'être allumé.

- Chaque lancement télécharge les fichiers du jour.
- Les adresses déjà géocodées sont gardées d'une fois sur l'autre (cache) : seules les nouvelles adresses sont envoyées à l'IGN.
- Au tout premier lancement, le géocodage est limité à 3 heures. Les adresses restantes sont placées provisoirement au centre de leur commune, puis géocodées aux lancements suivants.
- Pour lancer une mise à jour à la main : onglet **Actions** → **Mise à jour des cartes** → **Run workflow**.

## Lancer les scripts sur son ordinateur

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python3 carte_maisons_sante.py                          # → carte_maisons_sante.html
python3 carte_professionnels_sante.py --departements 49 # → carte_professionnels/index.html
```

Sans `--departements`, le script des professionnels traite toute la France. Le premier passage est long à cause du géocodage. Les fichiers téléchargés vont dans `donnees/` et sont retéléchargés quand ils ont plus de 20 heures.

## Structure du dépôt

```
carte_maisons_sante.py          carte des maisons de santé
carte_professionnels_sante.py   carte des professionnels de santé
sources_sante.py                outils communs : téléchargements, lecture de FINESS+, géocodage
accueil/index.html              page d'accueil du site
.github/workflows/              mise à jour automatique
```

## Limites

- **Rattachement partiel aux MSP.** Beaucoup de professionnels déclarent leur propre cabinet plutôt que la maison de santé, ou une adresse légèrement différente. Les structures sans professionnel identifié sont masquées par défaut, et une case permet de les afficher.
- **Sites multiples.** Un professionnel qui exerce sur plusieurs sites apparaît à chacun d'eux.
- **Précision du géocodage.** Une adresse mal saisie peut donner un point approximatif. La fiche le signale quand le lieu est placé au centre de la commune.
- **Doctolib.** Doctolib n'a pas d'annuaire public : le lien lance une recherche web, et le professionnel n'y figure pas forcément.
