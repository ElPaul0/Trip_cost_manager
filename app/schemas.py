from dataclasses import dataclass

from app.fuels import get_fuel_profile


@dataclass(frozen=True)
class TripCosts:
    fuel_cost: float
    maintenance_cost: float
    amortization_cost: float
    total_cost: float
    cost_per_person: float
    energy_used: float
    energy_unit: str
    co2_kg: float
    co2_factor: float


def round_money(value: float) -> float:
    return round(value, 2)


def round_qty(value: float, digits: int = 2) -> float:
    return round(value, digits)


def calculate_trip_costs(
    *,
    distance_km: float,
    consumption_per_100km: float,
    energy_price: float,
    maintenance_per_km: float,
    amortization_per_km: float,
    tolls: float,
    passengers: int,
    fuel_type: str,
) -> TripCosts:
    if distance_km <= 0:
        raise ValueError("La distance doit être supérieure à 0.")
    if consumption_per_100km < 0:
        raise ValueError("La consommation ne peut pas être négative.")
    if energy_price < 0:
        raise ValueError("Le prix de l’énergie ne peut pas être négatif.")
    if maintenance_per_km < 0 or amortization_per_km < 0:
        raise ValueError("Les indemnités au kilomètre ne peuvent pas être négatives.")
    if tolls < 0:
        raise ValueError("Le coût des péages ne peut pas être négatif.")
    if passengers < 1:
        raise ValueError("Le nombre de personnes doit être au moins 1.")

    profile = get_fuel_profile(fuel_type)
    energy_used = distance_km * (consumption_per_100km / 100.0)
    fuel_cost = energy_used * energy_price
    maintenance_cost = distance_km * maintenance_per_km
    amortization_cost = distance_km * amortization_per_km
    total_cost = fuel_cost + maintenance_cost + amortization_cost + tolls
    cost_per_person = total_cost / passengers
    co2_kg = energy_used * profile.co2_kg_per_unit

    return TripCosts(
        fuel_cost=round_money(fuel_cost),
        maintenance_cost=round_money(maintenance_cost),
        amortization_cost=round_money(amortization_cost),
        total_cost=round_money(total_cost),
        cost_per_person=round_money(cost_per_person),
        energy_used=round_qty(energy_used),
        energy_unit=profile.unit,
        co2_kg=round_qty(co2_kg),
        co2_factor=profile.co2_kg_per_unit,
    )
