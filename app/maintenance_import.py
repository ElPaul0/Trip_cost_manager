"""Import d’opérations d’entretien depuis un fichier CSV."""

from __future__ import annotations

import csv
import io
import re
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


def _decode_content(content: bytes | str) -> str:
    if isinstance(content, str):
        return content
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _detect_delimiter(text: str) -> str:
    """Choisit ; ou , selon le contenu réel (pas seulement l’en-tête)."""
    sample_lines = [line for line in text.splitlines() if line.strip()][:30]
    if not sample_lines:
        return ","

    # Prefer Sniffer on a multi-line sample when possible
    sample = "\n".join(sample_lines[:12])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        if dialect.delimiter in {";", ",", "\t"}:
            return dialect.delimiter
    except csv.Error:
        pass

    # Fallback: count separators on data rows (skip header if mixed)
    semi = sum(line.count(";") for line in sample_lines)
    comma = sum(line.count(",") for line in sample_lines)
    if semi >= comma and semi > 0:
        return ";"
    return ","


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
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(
        f"Date invalide « {value} » "
        "(formats acceptés : AAAA-MM-JJ, JJ/MM/AAAA ou JJ/MM/AA)."
    )


def _parse_float(value: str, field_label: str, *, default: float | None = None) -> float:
    raw = value.strip().replace("\u00a0", "").replace(" ", "")
    # 1 234,56 or 1234,56 or 1234.56 — strip currency symbols
    raw = re.sub(r"[€$]", "", raw)
    if raw == "":
        if default is not None:
            return default
        raise ValueError(f"{field_label} manquant.")
    # French decimal comma (keep thousand dots only if no comma)
    if "," in raw and "." in raw:
        # 1.234,56 → 1234.56
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", ".")
    try:
        number = float(raw)
    except ValueError as exc:
        raise ValueError(f"{field_label} invalide : « {value} ».") from exc
    if number < 0:
        raise ValueError(f"{field_label} ne peut pas être négatif.")
    return number


def parse_maintenance_csv(content: bytes | str) -> ImportResult:
    text = _decode_content(content)
    delimiter = _detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    mapping = _map_headers(reader.fieldnames)

    created: list[ParsedMaintenanceRow] = []
    errors: list[str] = []

    for index, row in enumerate(reader, start=2):
        if not any((value or "").strip() for value in row.values()):
            continue

        try:
            name = (row.get(mapping["name"]) or "").strip()
            comments = ""
            if "comments" in mapping:
                comments = (row.get(mapping["comments"]) or "").strip()

            # Lignes « référence » sans nom : utiliser le commentaire ou un libellé par défaut
            if not name:
                name = comments.split(" - ")[0].strip() if comments else ""
                if not name:
                    name = "Sans nom"

            date_raw = (row.get(mapping["operation_date"]) or "").strip()
            if not date_raw:
                date_raw = date.today().isoformat()
            operation_date = _parse_date(date_raw)

            mileage_km = _parse_float(
                row.get(mapping["mileage_km"]) or "",
                "Kilométrage",
                default=0.0,
            )
            price = _parse_float(
                row.get(mapping["price"]) or "",
                "Prix",
                default=0.0,
            )

            parts_url = ""
            if "parts_url" in mapping:
                parts_url = (row.get(mapping["parts_url"]) or "").strip()
            if parts_url and not (
                parts_url.startswith("http://") or parts_url.startswith("https://")
            ):
                # Si ce n'est pas une URL, basculer dans les commentaires plutôt que d'échouer
                if comments:
                    comments = f"{parts_url} — {comments}"
                else:
                    comments = parts_url
                parts_url = ""

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
