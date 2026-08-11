"""Import d’opérations d’entretien depuis un fichier CSV."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timezone

HEADER_ALIASES = {
    "name": {"name", "nom", "operation", "opération", "libelle", "libellé"},
    "operation_date": {"operation_date", "date", "date_operation", "date_opération"},
    "mileage_km": {"mileage_km", "kilometrage", "kilométrage", "km", "mileage"},
    "price": {"price", "prix", "cout", "coût", "cost"},
    "parts_url": {"parts_url", "lien", "url", "link", "piece_url", "pièce_url"},
    "comments": {"comments", "commentaires", "commentaire", "notes", "note"},
}

REQUIRED_FIELDS = ("name", "operation_date", "mileage_km", "price")

TEMPLATE_CSV = (
    "name,operation_date,mileage_km,price,parts_url,comments\n"
    "Vidange + filtre,2024-06-15,120000,89.90,https://example.com/huile,5W40 garage du coin\n"
    "Pneus avant,15/03/2025,125400,220,,Michelin Primacy\n"
)


@dataclass
class ParsedMaintenanceRow:
    name: str
    operation_date: datetime
    mileage_km: float
    price: float
    parts_url: str
    comments: str
    source_line: int


@dataclass
class ImportResult:
    created: list[ParsedMaintenanceRow]
    errors: list[str]


def _normalize_header(value: str) -> str:
    return (
        value.strip()
        .lower()
        .replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("à", "a")
        .replace("ù", "u")
        .replace("ô", "o")
        .replace("î", "i")
        .replace("ç", "c")
        .replace(" ", "_")
        .replace("-", "_")
    )


def _map_headers(fieldnames: list[str] | None) -> dict[str, str]:
    if not fieldnames:
        raise ValueError("Le fichier CSV n’a pas d’en-tête.")

    mapping: dict[str, str] = {}
    normalized = {_normalize_header(name): name for name in fieldnames if name}

    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            key = _normalize_header(alias)
            if key in normalized:
                mapping[field] = normalized[key]
                break

    missing = [field for field in REQUIRED_FIELDS if field not in mapping]
    if missing:
        raise ValueError(
            "Colonnes obligatoires manquantes : "
            + ", ".join(missing)
            + ". Attendu au minimum : name, operation_date, mileage_km, price "
            "(ou équivalents FR : nom, date, kilometrage, prix)."
        )
    return mapping


def _parse_date(value: str) -> datetime:
    raw = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(
        f"Date invalide « {value} » (formats acceptés : AAAA-MM-JJ ou JJ/MM/AAAA)."
    )


def _parse_float(value: str, field_label: str) -> float:
    raw = value.strip().replace(" ", "").replace(",", ".")
    if raw == "":
        raise ValueError(f"{field_label} manquant.")
    try:
        number = float(raw)
    except ValueError as exc:
        raise ValueError(f"{field_label} invalide : « {value} ».") from exc
    if number < 0:
        raise ValueError(f"{field_label} ne peut pas être négatif.")
    return number


def parse_maintenance_csv(content: bytes | str) -> ImportResult:
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig")
    else:
        text = content

    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_headers(reader.fieldnames)

    created: list[ParsedMaintenanceRow] = []
    errors: list[str] = []

    for index, row in enumerate(reader, start=2):
        if not any((value or "").strip() for value in row.values()):
            continue

        try:
            name = (row.get(mapping["name"]) or "").strip()
            if not name:
                raise ValueError("Le nom est obligatoire.")

            date_raw = (row.get(mapping["operation_date"]) or "").strip()
            if not date_raw:
                date_raw = date.today().isoformat()
            operation_date = _parse_date(date_raw)

            mileage_km = _parse_float(row.get(mapping["mileage_km"]) or "", "Kilométrage")
            price = _parse_float(row.get(mapping["price"]) or "", "Prix")

            parts_url = ""
            if "parts_url" in mapping:
                parts_url = (row.get(mapping["parts_url"]) or "").strip()
            if parts_url and not (
                parts_url.startswith("http://") or parts_url.startswith("https://")
            ):
                raise ValueError("Le lien pièce doit commencer par http:// ou https://.")

            comments = ""
            if "comments" in mapping:
                comments = (row.get(mapping["comments"]) or "").strip()

            created.append(
                ParsedMaintenanceRow(
                    name=name,
                    operation_date=operation_date,
                    mileage_km=mileage_km,
                    price=price,
                    parts_url=parts_url,
                    comments=comments,
                    source_line=index,
                )
            )
        except ValueError as exc:
            errors.append(f"Ligne {index} : {exc}")

    return ImportResult(created=created, errors=errors)
