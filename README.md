# Trip Cost Manager

Petite application web pour calculer le coût d’un trajet (carburant, entretien, amortissement, péages), le partager entre covoitureurs, et archiver l’historique par véhicule.

## Fonctionnalités

- Gestion des véhicules (conso, carburant, indemnités €/km, EV / kWh)
- Ajout de trajets avec calcul puis archivage
- CO₂ estimé par trajet + petits commentaires
- Archive par véhicule + statistiques globales

## Formule

- Énergie = `distance × (conso / 100) × prix` (L ou kWh selon le carburant)
- Entretien = `distance × indemnité_entretien`
- Amortissement = `distance × indemnité_amortissement`
- Total = énergie + entretien + amortissement + péages
- Par personne = total ÷ nombre de personnes (conducteur inclus)
- CO₂ = énergie consommée × facteur du carburant

## Lancer en local (sans Docker)

Prérequis : Python 3.12+

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Ouvrir [http://localhost:8000](http://localhost:8000).

Par défaut, l’app utilise une base **SQLite** locale (`trip_cost_manager.db`).

## Lancer en local avec Docker Compose (build)

```bash
cp .env.example .env
docker compose up --build
```

## Déploiement serveur (image prête, sans clone)

À chaque push sur `main`, GitHub Actions publie l’image :

`ghcr.io/elpaul0/trip_cost_manager:latest`

Sur le serveur, il suffit de **2 fichiers** :

1. [`docker-compose.deploy.yml`](docker-compose.deploy.yml)
2. un `.env` (copié depuis [`.env.example`](.env.example))

```bash
mkdir trip-cost-manager && cd trip-cost-manager

# Récupérer les 2 fichiers (curl, scp, ou copier-coller)
curl -fsSL -o docker-compose.yml \
  https://raw.githubusercontent.com/ElPaul0/Trip_cost_manager/main/docker-compose.deploy.yml
curl -fsSL -o .env.example \
  https://raw.githubusercontent.com/ElPaul0/Trip_cost_manager/main/.env.example

cp .env.example .env
nano .env   # changer POSTGRES_PASSWORD

docker compose up -d
```

Mises à jour :

```bash
docker compose pull
docker compose up -d
```

Les trajets et véhicules restent intacts : la base PostgreSQL vit dans le volume `postgres_data`, que `docker compose up -d` ne touche pas.

**Ne jamais lancer** `docker compose down -v` en production (le `-v` supprime le volume et donc toutes les données).

### Versions d’image et rollback

Chaque version stable peut être publiée avec un tag Git (`v1.0`, `v1.1`, …). GitHub Actions pousse alors l’image correspondante sur GHCR :

| Tag Git | Image Docker |
|---------|--------------|
| `v1.0` | `ghcr.io/elpaul0/trip_cost_manager:1.0` |
| `v1.1` | `ghcr.io/elpaul0/trip_cost_manager:1.1` |
| push `main` | `ghcr.io/elpaul0/trip_cost_manager:latest` |

**Figurer la version actuelle** (avant une mise à jour) :

```bash
# Sur le poste de dev — tagger le commit stable
git tag v1.0
git push origin v1.0
```

Sur le serveur, dans `.env` :

```env
APP_IMAGE=ghcr.io/elpaul0/trip_cost_manager:1.0
```

**Passer à une nouvelle version** :

```env
APP_IMAGE=ghcr.io/elpaul0/trip_cost_manager:1.1
# ou :latest pour suivre main
```

```bash
docker compose pull
docker compose up -d
```

**Revenir en arrière** : remettre `APP_IMAGE=...:1.0` puis `pull` + `up -d`. Les données PostgreSQL ne changent pas.


Après le premier run Actions, si le pull échoue avec `unauthorized` / `denied` :

1. GitHub → repo → **Packages** → `trip_cost_manager`
2. **Package settings** → Change visibility → **Public**

(ou reste privé et fais `docker login ghcr.io` sur le serveur)

## Mode GPS (HERE)

Sur la page **Nouveau trajet**, un toggle **Manuel / GPS (HERE)** apparaît si une clé API est configurée.

1. Créez une clé sur [platform.here.com](https://platform.here.com) (services **Routing** + **Geocoding & Search**)
2. Ajoutez dans `.env` :

```env
HERE_API_KEY=votre_cle
HERE_ENABLED=true
```

En mode GPS : suggestions de lieux → **Calculer l’itinéraire** préremplit distance et péages (toujours modifiables).

Sans clé, l’app reste en mode manuel uniquement.

## Structure

```
app/
  main.py
  models.py
  database.py
  schemas.py
  fuels.py
  comments.py
  templates/
  static/
.github/workflows/docker-publish.yml
Dockerfile
docker-compose.yml              # local / build
docker-compose.deploy.yml       # serveur / image
requirements.txt
```

## Évolutions possibles

- Intégration Google Maps / ViaMichelin (distance / péages)
- Authentification
- Export CSV / PDF
