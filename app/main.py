from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, joinedload

from app.comments import trip_comment
from app.database import Base, engine, get_db
from app.fuels import FUEL_TYPES, consumption_unit_label, get_fuel_profile
from app.models import Trip, Vehicle
from app.schemas import TripCosts, calculate_trip_costs

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Trip Cost Manager")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    if "trips" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("trips")}
    alterations: list[str] = []
    if "name" not in columns:
        alterations.append("ALTER TABLE trips ADD COLUMN name VARCHAR(160) DEFAULT ''")
    if "departure" not in columns:
        alterations.append("ALTER TABLE trips ADD COLUMN departure VARCHAR(160) DEFAULT ''")
    if "arrival" not in columns:
        alterations.append("ALTER TABLE trips ADD COLUMN arrival VARCHAR(160) DEFAULT ''")
    if "trip_date" not in columns:
        alterations.append("ALTER TABLE trips ADD COLUMN trip_date TIMESTAMP")
    if "energy_used" not in columns:
        alterations.append("ALTER TABLE trips ADD COLUMN energy_used FLOAT DEFAULT 0")
    if "co2_kg" not in columns:
        alterations.append("ALTER TABLE trips ADD COLUMN co2_kg FLOAT DEFAULT 0")

    if not alterations:
        return

    with engine.begin() as conn:
        for statement in alterations:
            conn.execute(text(statement))
        if "trip_date" not in columns:
            conn.execute(text("UPDATE trips SET trip_date = created_at WHERE trip_date IS NULL"))


@app.on_event("startup")
def on_startup() -> None:
    ensure_schema()


def euro(value: float | None) -> str:
    if value is None:
        return "0,00 €"
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", " ")


def kg(value: float | None) -> str:
    if value is None:
        return "0 kg"
    return f"{value:,.2f} kg".replace(",", "X").replace(".", ",").replace("X", " ")


templates.env.filters["euro"] = euro
templates.env.filters["kg"] = kg
templates.env.globals["consumption_unit_label"] = consumption_unit_label
templates.env.globals["get_fuel_profile"] = get_fuel_profile


def build_stats(trips: list[Trip]) -> dict:
    trip_count = len(trips)
    total_km = sum(t.distance_km for t in trips)
    total_cost = sum(t.total_cost for t in trips)
    total_co2 = sum(t.co2_kg for t in trips)
    avg_cost_per_km = (total_cost / total_km) if total_km > 0 else 0.0
    return {
        "trip_count": trip_count,
        "total_km": round(total_km, 1),
        "total_cost": round(total_cost, 2),
        "total_co2": round(total_co2, 2),
        "avg_cost_per_km": round(avg_cost_per_km, 3),
    }


def default_form(vehicle_id: int | None = None) -> dict:
    return {
        "name": "",
        "departure": "",
        "arrival": "",
        "trip_date": date.today().isoformat(),
        "vehicle_id": vehicle_id or "",
        "distance_km": "",
        "fuel_price_per_liter": "",
        "tolls": "0",
        "passengers": "1",
    }


def parse_trip_date(value: str) -> datetime:
    parsed = date.fromisoformat(value)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


def selected_vehicle(vehicles: list[Vehicle], vehicle_id: int | None) -> Vehicle | None:
    if vehicle_id is None:
        return vehicles[0] if vehicles else None
    for vehicle in vehicles:
        if vehicle.id == vehicle_id:
            return vehicle
    return vehicles[0] if vehicles else None


def render_trip_form(
    request: Request,
    *,
    db: Session,
    form: dict,
    error: str | None = None,
    costs: TripCosts | None = None,
    comment: str | None = None,
    status_code: int = 200,
):
    vehicles = db.scalars(select(Vehicle).order_by(Vehicle.name)).all()
    selected = form.get("vehicle_id")
    selected_vehicle_id = int(selected) if str(selected).isdigit() else None
    current = selected_vehicle(list(vehicles), selected_vehicle_id)
    profile = get_fuel_profile(current.fuel_type) if current else get_fuel_profile("Essence")
    return templates.TemplateResponse(
        request,
        "trip_form.html",
        {
            "vehicles": vehicles,
            "selected_vehicle_id": current.id if current else None,
            "selected_profile": profile,
            "error": error,
            "form": form,
            "costs": costs,
            "comment": comment,
        },
        status_code=status_code,
    )


@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    vehicles = db.scalars(select(Vehicle).order_by(Vehicle.name)).all()
    trips = db.scalars(select(Trip).order_by(Trip.trip_date.desc(), Trip.id.desc())).all()
    recent_trips = db.scalars(
        select(Trip)
        .options(joinedload(Trip.vehicle))
        .order_by(Trip.trip_date.desc(), Trip.id.desc())
        .limit(5)
    ).unique().all()
    stats = build_stats(list(trips))
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "vehicles": vehicles,
            "stats": stats,
            "recent_trips": recent_trips,
            "vehicle_count": len(vehicles),
        },
    )


@app.get("/vehicles")
def vehicles_page(request: Request, db: Session = Depends(get_db)):
    vehicles = db.scalars(select(Vehicle).order_by(Vehicle.name)).all()
    return templates.TemplateResponse(
        request,
        "vehicles.html",
        fuel_page_context(vehicles=vehicles, error=None),
    )


def validate_vehicle_form(
    *,
    name: str,
    model: str,
    fuel_type: str,
    consumption_l_per_100km: float,
    maintenance_per_km: float,
    amortization_per_km: float,
) -> str | None:
    if not name.strip() or not model.strip():
        return "Le nom et le modèle sont obligatoires."
    if fuel_type not in FUEL_TYPES:
        return "Type de carburant invalide."
    if consumption_l_per_100km < 0:
        return "La consommation ne peut pas être négative."
    if maintenance_per_km < 0 or amortization_per_km < 0:
        return "Les indemnités ne peuvent pas être négatives."
    return None


def fuel_page_context(**extra):
    return {
        "fuel_types": FUEL_TYPES,
        "fuel_profiles": {name: get_fuel_profile(name) for name in FUEL_TYPES},
        **extra,
    }


@app.post("/vehicles")
def create_vehicle(
    request: Request,
    name: str = Form(...),
    model: str = Form(...),
    consumption_l_per_100km: float = Form(...),
    fuel_type: str = Form(...),
    maintenance_per_km: float = Form(0.0),
    amortization_per_km: float = Form(0.0),
    db: Session = Depends(get_db),
):
    error = validate_vehicle_form(
        name=name,
        model=model,
        fuel_type=fuel_type,
        consumption_l_per_100km=consumption_l_per_100km,
        maintenance_per_km=maintenance_per_km,
        amortization_per_km=amortization_per_km,
    )

    if error:
        vehicles = db.scalars(select(Vehicle).order_by(Vehicle.name)).all()
        return templates.TemplateResponse(
            request,
            "vehicles.html",
            fuel_page_context(vehicles=vehicles, error=error),
            status_code=400,
        )

    vehicle = Vehicle(
        name=name.strip(),
        model=model.strip(),
        consumption_l_per_100km=consumption_l_per_100km,
        fuel_type=fuel_type,
        maintenance_per_km=maintenance_per_km,
        amortization_per_km=amortization_per_km,
    )
    db.add(vehicle)
    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}", status_code=303)


@app.get("/vehicles/{vehicle_id}")
def vehicle_detail(vehicle_id: int, request: Request, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")

    trips = db.scalars(
        select(Trip)
        .where(Trip.vehicle_id == vehicle_id)
        .order_by(Trip.trip_date.desc(), Trip.id.desc())
    ).all()
    stats = build_stats(list(trips))
    profile = get_fuel_profile(vehicle.fuel_type)
    return templates.TemplateResponse(
        request,
        "vehicle_detail.html",
        {
            "vehicle": vehicle,
            "trips": trips,
            "stats": stats,
            "profile": profile,
        },
    )


@app.get("/vehicles/{vehicle_id}/edit")
def edit_vehicle_page(vehicle_id: int, request: Request, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")
    return templates.TemplateResponse(
        request,
        "vehicle_edit.html",
        fuel_page_context(vehicle=vehicle, error=None),
    )


@app.post("/vehicles/{vehicle_id}/edit")
def update_vehicle(
    vehicle_id: int,
    request: Request,
    name: str = Form(...),
    model: str = Form(...),
    consumption_l_per_100km: float = Form(...),
    fuel_type: str = Form(...),
    maintenance_per_km: float = Form(0.0),
    amortization_per_km: float = Form(0.0),
    db: Session = Depends(get_db),
):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")

    error = validate_vehicle_form(
        name=name,
        model=model,
        fuel_type=fuel_type,
        consumption_l_per_100km=consumption_l_per_100km,
        maintenance_per_km=maintenance_per_km,
        amortization_per_km=amortization_per_km,
    )
    if error:
        vehicle.name = name
        vehicle.model = model
        vehicle.consumption_l_per_100km = consumption_l_per_100km
        vehicle.fuel_type = fuel_type
        vehicle.maintenance_per_km = maintenance_per_km
        vehicle.amortization_per_km = amortization_per_km
        return templates.TemplateResponse(
            request,
            "vehicle_edit.html",
            fuel_page_context(vehicle=vehicle, error=error),
            status_code=400,
        )

    vehicle.name = name.strip()
    vehicle.model = model.strip()
    vehicle.consumption_l_per_100km = consumption_l_per_100km
    vehicle.fuel_type = fuel_type
    vehicle.maintenance_per_km = maintenance_per_km
    vehicle.amortization_per_km = amortization_per_km
    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}", status_code=303)


@app.get("/vehicles/{vehicle_id}/delete")
def delete_vehicle_confirm(vehicle_id: int, request: Request, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")
    trip_count = db.scalars(
        select(Trip).where(Trip.vehicle_id == vehicle_id)
    ).all()
    return templates.TemplateResponse(
        request,
        "vehicle_delete.html",
        {
            "vehicle": vehicle,
            "trip_count": len(trip_count),
        },
    )


@app.post("/vehicles/{vehicle_id}/delete")
def delete_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")
    db.delete(vehicle)
    db.commit()
    return RedirectResponse(url="/vehicles", status_code=303)


@app.get("/trips/new")
def new_trip_page(request: Request, db: Session = Depends(get_db), vehicle_id: int | None = None):
    return render_trip_form(request, db=db, form=default_form(vehicle_id))


@app.post("/trips/new")
def submit_trip(
    request: Request,
    action: str = Form(...),
    vehicle_id: int = Form(...),
    name: str = Form(...),
    departure: str = Form(...),
    arrival: str = Form(...),
    trip_date: str = Form(...),
    distance_km: float = Form(...),
    fuel_price_per_liter: float = Form(...),
    tolls: float = Form(0.0),
    passengers: int = Form(1),
    db: Session = Depends(get_db),
):
    form = {
        "name": name,
        "departure": departure,
        "arrival": arrival,
        "trip_date": trip_date,
        "vehicle_id": vehicle_id,
        "distance_km": distance_km,
        "fuel_price_per_liter": fuel_price_per_liter,
        "tolls": tolls,
        "passengers": passengers,
    }

    if action not in {"calculate", "archive"}:
        return render_trip_form(
            request,
            db=db,
            form=form,
            error="Action invalide.",
            status_code=400,
        )

    if not name.strip() or not departure.strip() or not arrival.strip():
        return render_trip_form(
            request,
            db=db,
            form=form,
            error="Le nom, le départ et l’arrivée sont obligatoires.",
            status_code=400,
        )

    try:
        parsed_date = parse_trip_date(trip_date)
    except ValueError:
        return render_trip_form(
            request,
            db=db,
            form=form,
            error="La date du trajet est invalide.",
            status_code=400,
        )

    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        return render_trip_form(
            request,
            db=db,
            form=form,
            error="Véhicule introuvable.",
            status_code=400,
        )

    try:
        costs = calculate_trip_costs(
            distance_km=distance_km,
            consumption_per_100km=vehicle.consumption_l_per_100km,
            energy_price=fuel_price_per_liter,
            maintenance_per_km=vehicle.maintenance_per_km,
            amortization_per_km=vehicle.amortization_per_km,
            tolls=tolls,
            passengers=passengers,
            fuel_type=vehicle.fuel_type,
        )
    except ValueError as exc:
        return render_trip_form(
            request,
            db=db,
            form=form,
            error=str(exc),
            status_code=400,
        )

    comment = trip_comment(
        distance_km=distance_km,
        energy_used=costs.energy_used,
        energy_unit=costs.energy_unit,
        total_cost=costs.total_cost,
        cost_per_person=costs.cost_per_person,
        co2_kg=costs.co2_kg,
        passengers=passengers,
        departure=departure,
        arrival=arrival,
        fuel_type=vehicle.fuel_type,
    )

    if action == "calculate":
        return render_trip_form(request, db=db, form=form, costs=costs, comment=comment)

    trip = Trip(
        vehicle_id=vehicle.id,
        name=name.strip(),
        departure=departure.strip(),
        arrival=arrival.strip(),
        trip_date=parsed_date,
        distance_km=distance_km,
        fuel_price_per_liter=fuel_price_per_liter,
        tolls=tolls,
        passengers=passengers,
        fuel_cost=costs.fuel_cost,
        maintenance_cost=costs.maintenance_cost,
        amortization_cost=costs.amortization_cost,
        total_cost=costs.total_cost,
        cost_per_person=costs.cost_per_person,
        energy_used=costs.energy_used,
        co2_kg=costs.co2_kg,
    )
    db.add(trip)
    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}", status_code=303)
