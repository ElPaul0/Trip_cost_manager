from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Charge .env avant les modules qui lisent os.getenv (HERE, DATABASE_URL)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, joinedload

from app import here_maps
from app.comments import trip_comment
from app.database import Base, engine, get_db
from app.fuels import FUEL_TYPES, consumption_unit_label, get_fuel_profile
from app.maintenance_import import TEMPLATE_CSV, parse_maintenance_csv
from app.models import MaintenanceOp, Trip, User, Vehicle
from app.pin import hash_pin, user_has_pin, validate_pin_format, verify_pin
from app.schemas import TripCosts, calculate_trip_costs

BASE_DIR = Path(__file__).resolve().parent
ACTIVE_USER_COOKIE = "active_user_id"

app = FastAPI(title="Trip Cost Manager")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    with engine.begin() as conn:
        if "users" in table_names:
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            if "pin_hash" not in user_columns:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN pin_hash VARCHAR(120) DEFAULT ''")
                )

        if "vehicles" in table_names:
            vehicle_columns = {column["name"] for column in inspector.get_columns("vehicles")}
            if "user_id" not in vehicle_columns:
                default_user = conn.execute(
                    text("SELECT id FROM users ORDER BY id LIMIT 1")
                ).first()
                if default_user is None:
                    conn.execute(
                        text(
                            "INSERT INTO users (name, tagline) VALUES "
                            "('Utilisateur', 'Espace de départ')"
                        )
                    )
                    default_user = conn.execute(
                        text("SELECT id FROM users ORDER BY id LIMIT 1")
                    ).first()
                user_id = default_user[0]
                conn.execute(text("ALTER TABLE vehicles ADD COLUMN user_id INTEGER"))
                conn.execute(
                    text("UPDATE vehicles SET user_id = :user_id WHERE user_id IS NULL"),
                    {"user_id": user_id},
                )

        if "trips" in table_names:
            # refresh columns after possible earlier alters in same process
            inspector = inspect(engine)
            trip_columns = {column["name"] for column in inspector.get_columns("trips")}
            alterations: list[str] = []
            if "name" not in trip_columns:
                alterations.append("ALTER TABLE trips ADD COLUMN name VARCHAR(160) DEFAULT ''")
            if "departure" not in trip_columns:
                alterations.append(
                    "ALTER TABLE trips ADD COLUMN departure VARCHAR(160) DEFAULT ''"
                )
            if "arrival" not in trip_columns:
                alterations.append(
                    "ALTER TABLE trips ADD COLUMN arrival VARCHAR(160) DEFAULT ''"
                )
            if "trip_date" not in trip_columns:
                alterations.append("ALTER TABLE trips ADD COLUMN trip_date TIMESTAMP")
            if "energy_used" not in trip_columns:
                alterations.append("ALTER TABLE trips ADD COLUMN energy_used FLOAT DEFAULT 0")
            if "co2_kg" not in trip_columns:
                alterations.append("ALTER TABLE trips ADD COLUMN co2_kg FLOAT DEFAULT 0")
            if "is_round_trip" not in trip_columns:
                alterations.append(
                    "ALTER TABLE trips ADD COLUMN is_round_trip BOOLEAN DEFAULT FALSE"
                )
            for statement in alterations:
                conn.execute(text(statement))
            if "trip_date" not in trip_columns:
                conn.execute(
                    text("UPDATE trips SET trip_date = created_at WHERE trip_date IS NULL")
                )


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
templates.env.filters["short_place"] = here_maps.short_place
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


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(User.name)).all())


def get_active_user(request: Request, db: Session) -> User | None:
    cookie = request.cookies.get(ACTIVE_USER_COOKIE)
    if cookie and cookie.isdigit():
        user = db.get(User, int(cookie))
        if user:
            return user
    return None


def set_active_user_cookie(
    response: Response,
    user_id: int,
    *,
    remember: bool = True,
) -> None:
    kwargs = {
        "key": ACTIVE_USER_COOKIE,
        "value": str(user_id),
        "httponly": False,
        "samesite": "lax",
    }
    if remember:
        kwargs["max_age"] = 60 * 60 * 24 * 365
    response.set_cookie(**kwargs)


def user_context(request: Request, db: Session, **extra):
    users = list_users(db)
    active_user = get_active_user(request, db)
    return {
        "users": users,
        "active_user": active_user,
        **extra,
    }


def render(
    request: Request,
    template_name: str,
    db: Session,
    context: dict | None = None,
    status_code: int = 200,
):
    payload = user_context(request, db, **(context or {}))
    return templates.TemplateResponse(
        request,
        template_name,
        payload,
        status_code=status_code,
    )


def redirect_with_user(
    url: str,
    user_id: int | None = None,
    *,
    remember: bool = True,
) -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=303)
    if user_id is not None:
        set_active_user_cookie(response, user_id, remember=remember)
    return response


def require_user_or_redirect(request: Request, db: Session) -> User | RedirectResponse:
    user = get_active_user(request, db)
    if user is None:
        return RedirectResponse(url="/welcome", status_code=303)
    return user


def form_remember(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "on", "yes"}


def user_vehicles(db: Session, user: User) -> list[Vehicle]:
    return list(
        db.scalars(
            select(Vehicle).where(Vehicle.user_id == user.id).order_by(Vehicle.name)
        ).all()
    )


def get_owned_vehicle(db: Session, user: User, vehicle_id: int) -> Vehicle | None:
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle or vehicle.user_id != user.id:
        return None
    return vehicle


def get_owned_maintenance_op(
    db: Session, user: User, vehicle_id: int, op_id: int
) -> tuple[Vehicle, MaintenanceOp] | None:
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        return None
    operation = db.get(MaintenanceOp, op_id)
    if not operation or operation.vehicle_id != vehicle.id:
        return None
    return vehicle, operation


def get_owned_trip(
    db: Session, user: User, vehicle_id: int, trip_id: int
) -> tuple[Vehicle, Trip] | None:
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        return None
    trip = db.get(Trip, trip_id)
    if not trip or trip.vehicle_id != vehicle.id:
        return None
    return vehicle, trip


def apply_trip_form_to_model(
    trip: Trip,
    *,
    name: str,
    departure: str,
    arrival: str,
    trip_date: datetime,
    distance_km: float,
    fuel_price_per_liter: float,
    tolls: float,
    passengers: int,
    vehicle: Vehicle,
    is_round_trip: bool = False,
) -> TripCosts:
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
    trip.name = name.strip()
    trip.departure = departure.strip()
    trip.arrival = arrival.strip()
    trip.trip_date = trip_date
    trip.distance_km = distance_km
    trip.fuel_price_per_liter = fuel_price_per_liter
    trip.tolls = tolls
    trip.passengers = passengers
    trip.is_round_trip = is_round_trip
    trip.fuel_cost = costs.fuel_cost
    trip.maintenance_cost = costs.maintenance_cost
    trip.amortization_cost = costs.amortization_cost
    trip.total_cost = costs.total_cost
    trip.cost_per_person = costs.cost_per_person
    trip.energy_used = costs.energy_used
    trip.co2_kg = costs.co2_kg
    return costs


def validate_trip_form(
    *,
    name: str,
    departure: str,
    arrival: str,
    trip_date: str,
    distance_km: float,
    fuel_price_per_liter: float,
    tolls: float,
    passengers: int,
) -> tuple[str | None, datetime | None]:
    if not name.strip() or not departure.strip() or not arrival.strip():
        return "Le nom, le départ et l’arrivée sont obligatoires.", None
    try:
        parsed_date = parse_trip_date(trip_date)
    except ValueError:
        return "La date du trajet est invalide.", None
    if distance_km <= 0:
        return "La distance doit être supérieure à 0.", None
    if fuel_price_per_liter < 0:
        return "Le prix de l’énergie ne peut pas être négatif.", None
    if tolls < 0:
        return "Le coût des péages ne peut pas être négatif.", None
    if passengers < 1:
        return "Le nombre de personnes doit être au moins 1.", None
    return None, parsed_date


def list_maintenance_ops(db: Session, vehicle_id: int) -> list[MaintenanceOp]:
    return list(
        db.scalars(
            select(MaintenanceOp)
            .where(MaintenanceOp.vehicle_id == vehicle_id)
            .order_by(MaintenanceOp.operation_date.desc(), MaintenanceOp.id.desc())
        ).all()
    )


def latest_nonzero_mileage(operations: list[MaintenanceOp]) -> float | None:
    """Dernier km connu : on ignore les 0 (souvent erreurs / immat)."""
    for op in operations:
        if op.mileage_km and op.mileage_km > 0:
            return round(op.mileage_km, 1)
    return None


def vehicles_last_mileages(db: Session, vehicles: list[Vehicle]) -> dict[int, float | None]:
    return {
        vehicle.id: latest_nonzero_mileage(list_maintenance_ops(db, vehicle.id))
        for vehicle in vehicles
    }


def maintenance_stats(operations: list[MaintenanceOp]) -> dict:
    total_spent = round(sum(op.price for op in operations), 2)
    return {
        "op_count": len(operations),
        "total_spent": total_spent,
        "last_mileage": latest_nonzero_mileage(operations),
    }


def default_maintenance_form() -> dict:
    return {
        "name": "",
        "operation_date": date.today().isoformat(),
        "mileage_km": "",
        "price": "0",
        "parts_url": "",
        "comments": "",
    }


def validate_maintenance_form(
    *,
    name: str,
    operation_date: str,
    mileage_km: float,
    price: float,
    parts_url: str,
) -> tuple[str | None, datetime | None]:
    if not name.strip():
        return "Le nom de l’opération est obligatoire.", None
    try:
        parsed_date = parse_trip_date(operation_date)
    except ValueError:
        return "La date est invalide.", None
    if mileage_km < 0:
        return "Le kilométrage ne peut pas être négatif.", None
    if price < 0:
        return "Le prix ne peut pas être négatif.", None
    url = parts_url.strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        return "Le lien pièce doit commencer par http:// ou https://.", None
    return None, parsed_date


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
        "is_round_trip": False,
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


def render_trip_form(
    request: Request,
    *,
    db: Session,
    user: User,
    form: dict,
    error: str | None = None,
    costs: TripCosts | None = None,
    comment: str | None = None,
    status_code: int = 200,
):
    vehicles = user_vehicles(db, user)
    selected = form.get("vehicle_id")
    selected_vehicle_id = int(selected) if str(selected).isdigit() else None
    current = selected_vehicle(vehicles, selected_vehicle_id)
    profile = get_fuel_profile(current.fuel_type) if current else get_fuel_profile("Essence")
    return render(
        request,
        "trip_form.html",
        db,
        {
            "vehicles": vehicles,
            "selected_vehicle_id": current.id if current else None,
            "selected_profile": profile,
            "error": error,
            "form": form,
            "costs": costs,
            "comment": comment,
            "here_enabled": here_maps.is_enabled(),
        },
        status_code=status_code,
    )


class HereRouteRequest(BaseModel):
    origin_lat: float = Field(..., ge=-90, le=90)
    origin_lng: float = Field(..., ge=-180, le=180)
    destination_lat: float = Field(..., ge=-90, le=90)
    destination_lng: float = Field(..., ge=-180, le=180)


@app.get("/api/here/status")
def here_status():
    return {"enabled": here_maps.is_enabled()}


@app.get("/api/here/suggest")
def here_suggest(q: str = "", limit: int = 6):
    if not here_maps.is_enabled():
        raise HTTPException(status_code=503, detail="HERE non configuré.")
    try:
        suggestions = here_maps.suggest_places(q, limit=limit)
    except here_maps.HereMapsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "items": [
            {
                "label": s.label,
                "short_label": s.short_label,
                "lat": s.lat,
                "lng": s.lng,
            }
            for s in suggestions
        ]
    }


@app.post("/api/here/route")
def here_route(payload: HereRouteRequest):
    if not here_maps.is_enabled():
        raise HTTPException(status_code=503, detail="HERE non configuré.")
    try:
        estimate = here_maps.calculate_route(
            origin_lat=payload.origin_lat,
            origin_lng=payload.origin_lng,
            destination_lat=payload.destination_lat,
            destination_lng=payload.destination_lng,
        )
    except here_maps.HereMapsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "distance_km": estimate.distance_km,
        "tolls_eur": estimate.tolls_eur,
        "duration_seconds": estimate.duration_seconds,
    }


@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user

    vehicles = user_vehicles(db, user)
    vehicle_ids = [vehicle.id for vehicle in vehicles]
    trips: list[Trip] = []
    recent_trips: list[Trip] = []
    if vehicle_ids:
        trips = list(
            db.scalars(
                select(Trip)
                .where(Trip.vehicle_id.in_(vehicle_ids))
                .order_by(Trip.trip_date.desc(), Trip.id.desc())
            ).all()
        )
        recent_trips = list(
            db.scalars(
                select(Trip)
                .options(joinedload(Trip.vehicle))
                .where(Trip.vehicle_id.in_(vehicle_ids))
                .order_by(Trip.trip_date.desc(), Trip.id.desc())
                .limit(5)
            )
            .unique()
            .all()
        )

    stats = build_stats(trips)
    return render(
        request,
        "index.html",
        db,
        {
            "vehicles": vehicles,
            "stats": stats,
            "recent_trips": recent_trips,
            "vehicle_count": len(vehicles),
        },
    )


@app.get("/welcome")
def welcome_page(request: Request, db: Session = Depends(get_db)):
    active = get_active_user(request, db)
    if active is not None and request.query_params.get("change") != "1":
        return RedirectResponse(url="/", status_code=303)
    return render(
        request,
        "welcome.html",
        db,
        {"error": None, "force_choose": request.query_params.get("change") == "1"},
    )


@app.post("/welcome/select")
def welcome_select_user(
    user_id: int = Form(...),
    remember: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    return redirect_with_user("/", user.id, remember=form_remember(remember))


@app.post("/welcome/create")
def welcome_create_user(
    request: Request,
    name: str = Form(...),
    tagline: str = Form(""),
    pin: str = Form(""),
    remember: str | None = Form(None),
    db: Session = Depends(get_db),
):
    error = None
    if not name.strip():
        error = "Le nom est obligatoire."
    else:
        error = validate_pin_format(pin)
    if error:
        return render(
            request,
            "welcome.html",
            db,
            {"error": error, "force_choose": True},
            status_code=400,
        )
    user = User(
        name=name.strip(),
        tagline=tagline.strip(),
        pin_hash=hash_pin(pin),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return redirect_with_user("/", user.id, remember=form_remember(remember))


@app.get("/users")
def users_page(request: Request, db: Session = Depends(get_db)):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render(
        request,
        "users.html",
        db,
        {"error": None, "edit_user": None, "edit_has_pin": False},
    )


@app.post("/users")
def create_user(
    request: Request,
    name: str = Form(...),
    tagline: str = Form(""),
    pin: str = Form(""),
    db: Session = Depends(get_db),
):
    active = require_user_or_redirect(request, db)
    if isinstance(active, RedirectResponse):
        return active
    error = None
    if not name.strip():
        error = "Le nom est obligatoire."
    else:
        error = validate_pin_format(pin)
    if error:
        return render(
            request,
            "users.html",
            db,
            {"error": error, "edit_user": None, "edit_has_pin": False},
            status_code=400,
        )

    user = User(
        name=name.strip(),
        tagline=tagline.strip(),
        pin_hash=hash_pin(pin),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return redirect_with_user("/", user.id, remember=True)


@app.post("/users/{user_id}/switch")
def switch_user(user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    return redirect_with_user("/", user.id, remember=True)


@app.get("/users/{user_id}/edit")
def edit_user_page(user_id: int, request: Request, db: Session = Depends(get_db)):
    active = require_user_or_redirect(request, db)
    if isinstance(active, RedirectResponse):
        return active
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if user.id != active.id:
        raise HTTPException(
            status_code=403,
            detail="Vous ne pouvez modifier que l’utilisateur actuellement actif.",
        )
    return render(
        request,
        "users.html",
        db,
        {
            "error": None,
            "edit_user": user,
            "edit_has_pin": user_has_pin(user.pin_hash),
        },
    )


@app.post("/users/{user_id}/edit")
def update_user(
    user_id: int,
    request: Request,
    name: str = Form(...),
    tagline: str = Form(""),
    current_pin: str = Form(""),
    new_pin: str = Form(""),
    clear_pin: str | None = Form(None),
    db: Session = Depends(get_db),
):
    active = require_user_or_redirect(request, db)
    if isinstance(active, RedirectResponse):
        return active
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if user.id != active.id:
        raise HTTPException(
            status_code=403,
            detail="Vous ne pouvez modifier que l’utilisateur actuellement actif.",
        )

    has_pin = user_has_pin(user.pin_hash)
    error = None
    if not name.strip():
        error = "Le nom est obligatoire."
    elif has_pin and not verify_pin(current_pin, user.pin_hash):
        error = "Code PIN incorrect."
    elif clear_pin:
        pass
    else:
        error = validate_pin_format(new_pin)

    if error:
        return render(
            request,
            "users.html",
            db,
            {"error": error, "edit_user": user, "edit_has_pin": has_pin},
            status_code=400,
        )

    user.name = name.strip()
    user.tagline = tagline.strip()
    if clear_pin:
        user.pin_hash = ""
    elif new_pin.strip():
        user.pin_hash = hash_pin(new_pin)
    db.commit()
    return redirect_with_user("/users", user.id)


@app.get("/users/{user_id}/delete")
def delete_user_confirm(user_id: int, request: Request, db: Session = Depends(get_db)):
    active = require_user_or_redirect(request, db)
    if isinstance(active, RedirectResponse):
        return active
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if user.id != active.id:
        raise HTTPException(
            status_code=403,
            detail="Vous ne pouvez supprimer que l’utilisateur actuellement actif.",
        )
    vehicle_count = len(user_vehicles(db, user))
    return render(
        request,
        "user_delete.html",
        db,
        {
            "target_user": user,
            "vehicle_count": vehicle_count,
            "has_pin": user_has_pin(user.pin_hash),
            "error": None,
        },
    )


@app.post("/users/{user_id}/delete")
def delete_user(
    user_id: int,
    request: Request,
    current_pin: str = Form(""),
    db: Session = Depends(get_db),
):
    active = require_user_or_redirect(request, db)
    if isinstance(active, RedirectResponse):
        return active
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if user.id != active.id:
        raise HTTPException(
            status_code=403,
            detail="Vous ne pouvez supprimer que l’utilisateur actuellement actif.",
        )

    has_pin = user_has_pin(user.pin_hash)
    if has_pin and not verify_pin(current_pin, user.pin_hash):
        vehicle_count = len(user_vehicles(db, user))
        return render(
            request,
            "user_delete.html",
            db,
            {
                "target_user": user,
                "vehicle_count": vehicle_count,
                "has_pin": True,
                "error": "Code PIN incorrect.",
            },
            status_code=400,
        )

    db.delete(user)
    db.commit()

    response = RedirectResponse(url="/welcome", status_code=303)
    response.delete_cookie(ACTIVE_USER_COOKIE)
    return response


@app.get("/vehicles")
def vehicles_page(request: Request, db: Session = Depends(get_db)):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicles = user_vehicles(db, user)
    return render(
        request,
        "vehicles.html",
        db,
        fuel_page_context(
            vehicles=vehicles,
            last_mileages=vehicles_last_mileages(db, vehicles),
            error=None,
        ),
    )


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
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user

    error = validate_vehicle_form(
        name=name,
        model=model,
        fuel_type=fuel_type,
        consumption_l_per_100km=consumption_l_per_100km,
        maintenance_per_km=maintenance_per_km,
        amortization_per_km=amortization_per_km,
    )

    if error:
        vehicles = user_vehicles(db, user)
        return render(
            request,
            "vehicles.html",
            db,
            fuel_page_context(
                vehicles=vehicles,
                last_mileages=vehicles_last_mileages(db, vehicles),
                error=error,
            ),
            status_code=400,
        )

    vehicle = Vehicle(
        user_id=user.id,
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
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")

    trips = db.scalars(
        select(Trip)
        .where(Trip.vehicle_id == vehicle_id)
        .order_by(Trip.trip_date.desc(), Trip.id.desc())
    ).all()
    stats = build_stats(list(trips))
    maintenance_ops = list_maintenance_ops(db, vehicle_id)
    stats["last_mileage"] = latest_nonzero_mileage(maintenance_ops)
    profile = get_fuel_profile(vehicle.fuel_type)
    return render(
        request,
        "vehicle_detail.html",
        db,
        {
            "vehicle": vehicle,
            "trips": trips,
            "stats": stats,
            "profile": profile,
        },
    )


@app.get("/vehicles/{vehicle_id}/edit")
def edit_vehicle_page(vehicle_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")
    return render(
        request,
        "vehicle_edit.html",
        db,
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
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = get_owned_vehicle(db, user, vehicle_id)
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
        return render(
            request,
            "vehicle_edit.html",
            db,
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
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")
    trip_count = len(
        list(db.scalars(select(Trip).where(Trip.vehicle_id == vehicle_id)).all())
    )
    return render(
        request,
        "vehicle_delete.html",
        db,
        {
            "vehicle": vehicle,
            "trip_count": trip_count,
        },
    )


@app.post("/vehicles/{vehicle_id}/delete")
def delete_vehicle(vehicle_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")
    db.delete(vehicle)
    db.commit()
    return RedirectResponse(url="/vehicles", status_code=303)


@app.get("/trips/new")
def new_trip_page(
    request: Request,
    db: Session = Depends(get_db),
    vehicle_id: int | None = None,
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render_trip_form(request, db=db, user=user, form=default_form(vehicle_id))


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
    is_round_trip: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user

    round_trip = is_round_trip in {"1", "on", "true", "True"}
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
        "is_round_trip": round_trip,
    }

    if action not in {"calculate", "archive"}:
        return render_trip_form(
            request,
            db=db,
            user=user,
            form=form,
            error="Action invalide.",
            status_code=400,
        )

    if not name.strip() or not departure.strip() or not arrival.strip():
        return render_trip_form(
            request,
            db=db,
            user=user,
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
            user=user,
            form=form,
            error="La date du trajet est invalide.",
            status_code=400,
        )

    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        return render_trip_form(
            request,
            db=db,
            user=user,
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
            user=user,
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
        return render_trip_form(
            request,
            db=db,
            user=user,
            form=form,
            costs=costs,
            comment=comment,
        )

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
        is_round_trip=round_trip,
    )
    db.add(trip)
    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}", status_code=303)


@app.get("/vehicles/{vehicle_id}/trips/{trip_id}/edit")
def edit_trip_page(
    vehicle_id: int,
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    owned = get_owned_trip(db, user, vehicle_id, trip_id)
    if not owned:
        raise HTTPException(status_code=404, detail="Trajet introuvable")
    vehicle, trip = owned
    profile = get_fuel_profile(vehicle.fuel_type)
    return render(
        request,
        "trip_edit.html",
        db,
        {
            "vehicle": vehicle,
            "trip": trip,
            "profile": profile,
            "form": {
                "name": trip.name,
                "departure": trip.departure,
                "arrival": trip.arrival,
                "trip_date": trip.trip_date.date().isoformat(),
                "distance_km": trip.distance_km,
                "fuel_price_per_liter": trip.fuel_price_per_liter,
                "tolls": trip.tolls,
                "passengers": trip.passengers,
                "is_round_trip": bool(trip.is_round_trip),
            },
            "error": None,
        },
    )


@app.post("/vehicles/{vehicle_id}/trips/{trip_id}/edit")
def update_trip(
    vehicle_id: int,
    trip_id: int,
    request: Request,
    name: str = Form(...),
    departure: str = Form(...),
    arrival: str = Form(...),
    trip_date: str = Form(...),
    distance_km: float = Form(...),
    fuel_price_per_liter: float = Form(...),
    tolls: float = Form(0.0),
    passengers: int = Form(1),
    is_round_trip: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    owned = get_owned_trip(db, user, vehicle_id, trip_id)
    if not owned:
        raise HTTPException(status_code=404, detail="Trajet introuvable")
    vehicle, trip = owned
    profile = get_fuel_profile(vehicle.fuel_type)

    round_trip = is_round_trip in {"1", "on", "true", "True"}
    form = {
        "name": name,
        "departure": departure,
        "arrival": arrival,
        "trip_date": trip_date,
        "distance_km": distance_km,
        "fuel_price_per_liter": fuel_price_per_liter,
        "tolls": tolls,
        "passengers": passengers,
        "is_round_trip": round_trip,
    }
    error, parsed_date = validate_trip_form(
        name=name,
        departure=departure,
        arrival=arrival,
        trip_date=trip_date,
        distance_km=distance_km,
        fuel_price_per_liter=fuel_price_per_liter,
        tolls=tolls,
        passengers=passengers,
    )
    if error:
        return render(
            request,
            "trip_edit.html",
            db,
            {
                "vehicle": vehicle,
                "trip": trip,
                "profile": profile,
                "form": form,
                "error": error,
            },
            status_code=400,
        )

    try:
        apply_trip_form_to_model(
            trip,
            name=name,
            departure=departure,
            arrival=arrival,
            trip_date=parsed_date,
            distance_km=distance_km,
            fuel_price_per_liter=fuel_price_per_liter,
            tolls=tolls,
            passengers=passengers,
            vehicle=vehicle,
            is_round_trip=round_trip,
        )
    except ValueError as exc:
        return render(
            request,
            "trip_edit.html",
            db,
            {
                "vehicle": vehicle,
                "trip": trip,
                "profile": profile,
                "form": form,
                "error": str(exc),
            },
            status_code=400,
        )

    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}", status_code=303)


@app.get("/vehicles/{vehicle_id}/trips/{trip_id}/delete")
def delete_trip_confirm(
    vehicle_id: int,
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    owned = get_owned_trip(db, user, vehicle_id, trip_id)
    if not owned:
        raise HTTPException(status_code=404, detail="Trajet introuvable")
    vehicle, trip = owned
    return render(
        request,
        "trip_delete.html",
        db,
        {"vehicle": vehicle, "trip": trip},
    )


@app.post("/vehicles/{vehicle_id}/trips/{trip_id}/delete")
def delete_trip(
    vehicle_id: int,
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    owned = get_owned_trip(db, user, vehicle_id, trip_id)
    if not owned:
        raise HTTPException(status_code=404, detail="Trajet introuvable")
    vehicle, trip = owned
    db.delete(trip)
    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}", status_code=303)


def _maintenance_page_context(
    vehicle: Vehicle,
    operations: list[MaintenanceOp],
    *,
    form: dict | None = None,
    error: str | None = None,
    import_message: str | None = None,
    import_errors: list[str] | None = None,
) -> dict:
    return {
        "vehicle": vehicle,
        "operations": operations,
        "stats": maintenance_stats(operations),
        "form": form or default_maintenance_form(),
        "error": error,
        "import_message": import_message,
        "import_errors": import_errors or [],
    }


@app.get("/vehicles/{vehicle_id}/maintenance")
def maintenance_page(
    vehicle_id: int,
    request: Request,
    db: Session = Depends(get_db),
    imported: int | None = None,
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")

    operations = list_maintenance_ops(db, vehicle_id)
    import_message = None
    if imported is not None and imported > 0:
        import_message = f"{imported} opération(s) importée(s) avec succès."
    return render(
        request,
        "maintenance.html",
        db,
        _maintenance_page_context(
            vehicle,
            operations,
            import_message=import_message,
        ),
    )


@app.get("/vehicles/{vehicle_id}/maintenance/import/template")
def download_maintenance_import_template(
    vehicle_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")
    return Response(
        content=TEMPLATE_CSV,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="maintenance_import_template.csv"'
        },
    )


@app.post("/vehicles/{vehicle_id}/maintenance/import")
async def import_maintenance_ops(
    vehicle_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")

    filename = (file.filename or "").lower()
    if filename and not filename.endswith(".csv"):
        operations = list_maintenance_ops(db, vehicle_id)
        return render(
            request,
            "maintenance.html",
            db,
            _maintenance_page_context(
                vehicle,
                operations,
                import_errors=["Format non supporté : utilisez un fichier .csv"],
            ),
            status_code=400,
        )

    content = await file.read()
    if not content.strip():
        operations = list_maintenance_ops(db, vehicle_id)
        return render(
            request,
            "maintenance.html",
            db,
            _maintenance_page_context(
                vehicle,
                operations,
                import_errors=["Le fichier est vide."],
            ),
            status_code=400,
        )

    try:
        result = parse_maintenance_csv(content)
    except ValueError as exc:
        operations = list_maintenance_ops(db, vehicle_id)
        return render(
            request,
            "maintenance.html",
            db,
            _maintenance_page_context(
                vehicle,
                operations,
                import_errors=[str(exc)],
            ),
            status_code=400,
        )

    if not result.created and result.errors:
        operations = list_maintenance_ops(db, vehicle_id)
        return render(
            request,
            "maintenance.html",
            db,
            _maintenance_page_context(
                vehicle,
                operations,
                import_errors=result.errors,
            ),
            status_code=400,
        )

    for row in result.created:
        db.add(
            MaintenanceOp(
                vehicle_id=vehicle.id,
                name=row.name,
                operation_date=row.operation_date,
                mileage_km=row.mileage_km,
                price=row.price,
                parts_url=row.parts_url,
                comments=row.comments,
            )
        )
    db.commit()

    if result.errors:
        operations = list_maintenance_ops(db, vehicle_id)
        return render(
            request,
            "maintenance.html",
            db,
            _maintenance_page_context(
                vehicle,
                operations,
                import_message=(
                    f"{len(result.created)} opération(s) importée(s), "
                    f"{len(result.errors)} ligne(s) ignorée(s)."
                ),
                import_errors=result.errors,
            ),
        )

    return RedirectResponse(
        url=f"/vehicles/{vehicle.id}/maintenance?imported={len(result.created)}",
        status_code=303,
    )


@app.post("/vehicles/{vehicle_id}/maintenance")
def create_maintenance_op(
    vehicle_id: int,
    request: Request,
    name: str = Form(...),
    operation_date: str = Form(...),
    mileage_km: float = Form(...),
    price: float = Form(0.0),
    parts_url: str = Form(""),
    comments: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")

    form = {
        "name": name,
        "operation_date": operation_date,
        "mileage_km": mileage_km,
        "price": price,
        "parts_url": parts_url,
        "comments": comments,
    }
    error, parsed_date = validate_maintenance_form(
        name=name,
        operation_date=operation_date,
        mileage_km=mileage_km,
        price=price,
        parts_url=parts_url,
    )
    operations = list_maintenance_ops(db, vehicle_id)
    if error:
        return render(
            request,
            "maintenance.html",
            db,
            _maintenance_page_context(
                vehicle,
                operations,
                form=form,
                error=error,
            ),
            status_code=400,
        )

    operation = MaintenanceOp(
        vehicle_id=vehicle.id,
        name=name.strip(),
        operation_date=parsed_date,
        mileage_km=mileage_km,
        price=price,
        parts_url=parts_url.strip(),
        comments=comments.strip(),
    )
    db.add(operation)
    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}/maintenance", status_code=303)


@app.get("/vehicles/{vehicle_id}/maintenance/{op_id}/edit")
def edit_maintenance_page(
    vehicle_id: int,
    op_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    owned = get_owned_maintenance_op(db, user, vehicle_id, op_id)
    if not owned:
        raise HTTPException(status_code=404, detail="Opération introuvable")
    vehicle, operation = owned
    return render(
        request,
        "maintenance_edit.html",
        db,
        {
            "vehicle": vehicle,
            "operation": operation,
            "form": {
                "name": operation.name,
                "operation_date": operation.operation_date.date().isoformat(),
                "mileage_km": operation.mileage_km,
                "price": operation.price,
                "parts_url": operation.parts_url,
                "comments": operation.comments,
            },
            "error": None,
        },
    )


@app.post("/vehicles/{vehicle_id}/maintenance/{op_id}/edit")
def update_maintenance_op(
    vehicle_id: int,
    op_id: int,
    request: Request,
    name: str = Form(...),
    operation_date: str = Form(...),
    mileage_km: float = Form(...),
    price: float = Form(0.0),
    parts_url: str = Form(""),
    comments: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    owned = get_owned_maintenance_op(db, user, vehicle_id, op_id)
    if not owned:
        raise HTTPException(status_code=404, detail="Opération introuvable")
    vehicle, operation = owned

    form = {
        "name": name,
        "operation_date": operation_date,
        "mileage_km": mileage_km,
        "price": price,
        "parts_url": parts_url,
        "comments": comments,
    }
    error, parsed_date = validate_maintenance_form(
        name=name,
        operation_date=operation_date,
        mileage_km=mileage_km,
        price=price,
        parts_url=parts_url,
    )
    if error:
        return render(
            request,
            "maintenance_edit.html",
            db,
            {
                "vehicle": vehicle,
                "operation": operation,
                "form": form,
                "error": error,
            },
            status_code=400,
        )

    operation.name = name.strip()
    operation.operation_date = parsed_date
    operation.mileage_km = mileage_km
    operation.price = price
    operation.parts_url = parts_url.strip()
    operation.comments = comments.strip()
    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}/maintenance", status_code=303)


@app.get("/vehicles/{vehicle_id}/maintenance/{op_id}/delete")
def delete_maintenance_confirm(
    vehicle_id: int,
    op_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    owned = get_owned_maintenance_op(db, user, vehicle_id, op_id)
    if not owned:
        raise HTTPException(status_code=404, detail="Opération introuvable")
    vehicle, operation = owned
    return render(
        request,
        "maintenance_delete.html",
        db,
        {"vehicle": vehicle, "operation": operation},
    )


@app.post("/vehicles/{vehicle_id}/maintenance/{op_id}/delete")
def delete_maintenance_op(
    vehicle_id: int,
    op_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    owned = get_owned_maintenance_op(db, user, vehicle_id, op_id)
    if not owned:
        raise HTTPException(status_code=404, detail="Opération introuvable")
    vehicle, operation = owned
    db.delete(operation)
    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}/maintenance", status_code=303)
