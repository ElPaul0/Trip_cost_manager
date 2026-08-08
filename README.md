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

### Première publication GHCR

Après le premier run Actions, si le pull échoue avec `unauthorized` / `denied` :

1. GitHub → repo → **Packages** → `trip_cost_manager`
2. **Package settings** → Change visibility → **Public**

(ou reste privé et fais `docker login ghcr.io` sur le serveur)

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
