# Trip Cost Manager

Petite application web pour calculer le coût d’un trajet (carburant, entretien, amortissement, péages), le partager entre covoitureurs, et archiver l’historique par véhicule.

## Fonctionnalités

- Gestion des véhicules (conso, carburant, indemnités €/km)
- Ajout de trajets avec calcul automatique
- Archive par véhicule + statistiques globales

## Formule

- Carburant = `distance × (conso / 100) × prix_carburant`
- Entretien = `distance × indemnité_entretien`
- Amortissement = `distance × indemnité_amortissement`
- Total = carburant + entretien + amortissement + péages
- Par personne = total ÷ nombre de personnes (conducteur inclus)

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

Par défaut, l’app utilise une base **SQLite** locale (`trip_cost_manager.db`) pour faciliter les tests sans PostgreSQL.

## Lancer avec Docker Compose

Prérequis : Docker + Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

L’application est disponible sur [http://localhost:8000](http://localhost:8000) avec PostgreSQL.

Arrêter :

```bash
docker compose down
```

Les données PostgreSQL sont conservées dans le volume `postgres_data`.

## Déploiement serveur

Un exemple prêt à l’emploi est fourni dans [`docker-compose.server.example.yml`](docker-compose.server.example.yml).

Sur le serveur :

```bash
git clone <url-du-repo> trip-cost-manager
cd trip-cost-manager

# Option A : utiliser le compose principal
cp .env.example .env
nano .env   # changer POSTGRES_PASSWORD (obligatoire) et APP_PORT si besoin
docker compose up -d --build

# Option B : partir de l'exemple serveur
cp docker-compose.server.example.yml docker-compose.yml
cp .env.example .env
nano .env
docker compose up -d --build
```

L’app écoute ensuite sur `http://IP_DU_SERVEUR:8000` (ou le port défini dans `.env`).

Mises à jour :

```bash
cd trip-cost-manager
git pull
docker compose up -d --build
```

Conseils :
- Changez `POSTGRES_PASSWORD` avant le premier lancement
- Ne commitez jamais le fichier `.env`
- Pour HTTPS, placez Caddy / Nginx / Traefik devant le port de l’app
- Postgres n’est pas exposé publiquement (uniquement le réseau Docker interne)

## Structure

```
app/
  main.py
  models.py
  database.py
  schemas.py
  templates/
  static/
Dockerfile
docker-compose.yml
requirements.txt
```

## Évolutions possibles

- Intégration Google Maps / ViaMichelin (distance / péages)
- Authentification
- Export CSV / PDF
