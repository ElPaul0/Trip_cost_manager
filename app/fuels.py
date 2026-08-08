from dataclasses import dataclass


@dataclass(frozen=True)
class FuelProfile:
    label: str
    unit: str  # "L" or "kWh"
    co2_kg_per_unit: float
    consumption_label: str
    price_label: str
    energy_label: str


FUEL_PROFILES: dict[str, FuelProfile] = {
    "Essence": FuelProfile(
        label="Essence",
        unit="L",
        co2_kg_per_unit=2.31,
        consumption_label="Conso (L/100 km)",
        price_label="Prix carburant (€/L)",
        energy_label="L",
    ),
    "Diesel": FuelProfile(
        label="Diesel",
        unit="L",
        co2_kg_per_unit=2.68,
        consumption_label="Conso (L/100 km)",
        price_label="Prix carburant (€/L)",
        energy_label="L",
    ),
    "E85": FuelProfile(
        label="E85",
        unit="L",
        co2_kg_per_unit=1.52,
        consumption_label="Conso (L/100 km)",
        price_label="Prix carburant (€/L)",
        energy_label="L",
    ),
    "GPL": FuelProfile(
        label="GPL",
        unit="L",
        co2_kg_per_unit=1.66,
        consumption_label="Conso (L/100 km)",
        price_label="Prix carburant (€/L)",
        energy_label="L",
    ),
    "EV": FuelProfile(
        label="EV",
        unit="kWh",
        co2_kg_per_unit=0.052,
        consumption_label="Conso (kWh/100 km)",
        price_label="Prix électricité (€/kWh)",
        energy_label="kWh",
    ),
    "Électrique": FuelProfile(
        label="Électrique",
        unit="kWh",
        co2_kg_per_unit=0.052,
        consumption_label="Conso (kWh/100 km)",
        price_label="Prix électricité (€/kWh)",
        energy_label="kWh",
    ),
    "Hybride": FuelProfile(
        label="Hybride",
        unit="L",
        co2_kg_per_unit=2.10,
        consumption_label="Conso (L/100 km)",
        price_label="Prix carburant (€/L)",
        energy_label="L",
    ),
    "Autre": FuelProfile(
        label="Autre",
        unit="L",
        co2_kg_per_unit=2.30,
        consumption_label="Conso (L/100 km)",
        price_label="Prix carburant (€/L)",
        energy_label="L",
    ),
}

FUEL_TYPES = ["Essence", "Diesel", "E85", "GPL", "EV", "Hybride", "Autre"]

DEFAULT_FUEL = FUEL_PROFILES["Essence"]


def get_fuel_profile(fuel_type: str) -> FuelProfile:
    return FUEL_PROFILES.get(fuel_type, DEFAULT_FUEL)


def is_electric(fuel_type: str) -> bool:
    return get_fuel_profile(fuel_type).unit == "kWh"


def consumption_unit_label(fuel_type: str) -> str:
    return "kWh/100" if is_electric(fuel_type) else "L/100"
