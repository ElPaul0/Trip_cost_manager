import random


def trip_comment(
    *,
    distance_km: float,
    energy_used: float,
    energy_unit: str,
    total_cost: float,
    cost_per_person: float,
    co2_kg: float,
    passengers: int,
    departure: str,
    arrival: str,
    fuel_type: str,
) -> str:
    comments: list[str] = []

    if distance_km < 25:
        comments += [
            "Trajet express : à peine le temps de mettre la playlist en route.",
            "Court mais efficace. Même le GPS n’a pas eu le temps de se plaindre.",
            "Micro-trajet détecté. Attention aux frais fixes qui piquent plus que les kilomètres.",
        ]
    elif distance_km < 120:
        comments += [
            "Belle balade du quotidien — ni trop court, ni trop long.",
            "Trajet tranquille. Idéal pour écouter un podcast sans finir en feuilleton.",
        ]
    elif distance_km < 450:
        comments += [
            "Ça commence à sentir le vrai trajet. Pause café recommandée.",
            "Distance sérieuse : le siège commence à connaître vos formes.",
        ]
    elif distance_km < 800:
        comments += [
            "Longue route en perspective. Les snacks deviennent stratégiques.",
            "Trajet marathon. Hydratez-vous… et le réservoir aussi.",
        ]
    else:
        comments += [
            "Expédition longue distance. On est plus en mode road movie que courses du samedi.",
            "Respect. À ce kilométrage, même le compteur demande une pause.",
        ]

    consumption_per_100 = (energy_used / distance_km) * 100 if distance_km else 0
    if energy_unit == "L":
        if consumption_per_100 <= 5.5:
            comments.append("Conso sage : la pompe vous tire presque un sourire.")
        elif consumption_per_100 >= 9:
            comments.append("Grosse soif aujourd’hui… le réservoir a clairement un appétit d’ogre.")
    else:
        if consumption_per_100 <= 15:
            comments.append("Efficacité électrique au top. Les électrons ont bien bossé.")
        elif consumption_per_100 >= 22:
            comments.append("Batterie en mode sport : ça avance, mais ça grignote les kWh.")

    if co2_kg < 5:
        comments.append("Empreinte CO₂ légère. La planète note discrètement +1.")
    elif co2_kg > 120:
        comments.append("CO₂ costaud sur ce trajet… le covoiturage devient votre super-pouvoir.")

    if passengers >= 3:
        comments.append("Covoiturage activé : les frais fondent, le moral monte.")
    elif passengers == 1 and cost_per_person > 40:
        comments.append("Solo et salé. Un passager de plus et la facture respire.")

    if total_cost < 8:
        comments.append("Coût mini. On frôle le trajet offert par l’univers.")
    elif total_cost > 120:
        comments.append("Budget trajet bien présent. Heureusement, l’archive s’en souviendra pour vous.")

    dep = departure.strip().lower()
    arr = arrival.strip().lower()
    if dep == arr:
        comments.append("Départ = arrivée… road trip philosophique, ou oubli adorable ?")
    if {"rennes", "lyon"} <= {dep, arr}:
        comments.append("Rennes ↔ Lyon : le grand classique. La N/A7 connaît déjà votre nom.")
    if "vacance" in departure.lower() or "vacance" in arrival.lower():
        comments.append("Mode vacances détecté. Pensez à compter aussi le coût des glaces.")

    if fuel_type in {"EV", "Électrique"}:
        comments.append("Zéro échappement, 100 % silence… sauf le GPS qui rediscute l’itinéraire.")

    if not comments:
        comments.append("Trajet calculé. Ni trop, ni trop peu : juste ce qu’il faut.")

    return random.choice(comments)
