from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, joinedload

from app.comments import trip_comment
from app.database import Base, engine, get_db
from app.fuels import FUEL_TYPES, consumption_unit_label, get_fuel_profile
from app.models import MaintenanceOp, Trip, User, Vehicle
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
    users = list_users(db)
    if not users:
        return None
    cookie = request.cookies.get(ACTIVE_USER_COOKIE)
    if cookie and cookie.isdigit():
        user = db.get(User, int(cookie))
        if user:
            return user
    return users[0]


def set_active_user_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        key=ACTIVE_USER_COOKIE,
        value=str(user_id),
        max_age=60 * 60 * 24 * 365,
        httponly=False,
        samesite="lax",
    )


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


def redirect_with_user(url: str, user_id: int | None = None) -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=303)
    if user_id is not None:
        set_active_user_cookie(response, user_id)
    return response


def require_user_or_redirect(request: Request, db: Session) -> User | RedirectResponse:
    user = get_active_user(request, db)
    if user is None:
        return RedirectResponse(url="/users", status_code=303)
    return user


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


def list_maintenance_ops(db: Session, vehicle_id: int) -> list[MaintenanceOp]:
    return list(
        db.scalars(
            select(MaintenanceOp)
            .where(MaintenanceOp.vehicle_id == vehicle_id)
            .order_by(MaintenanceOp.operation_date.desc(), MaintenanceOp.id.desc())
        ).all()
    )


def maintenance_stats(operations: list[MaintenanceOp]) -> dict:
    total_spent = round(sum(op.price for op in operations), 2)
    last_mileage = max((op.mileage_km for op in operations), default=None)
    return {
        "op_count": len(operations),
        "total_spent": total_spent,
        "last_mileage": round(last_mileage, 1) if last_mileage is not None else None,
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
        },
        status_code=status_code,
    )


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


@app.get("/users")
def users_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "users.html", db, {"error": None, "edit_user": None})


@app.post("/users")
def create_user(
    request: Request,
    name: str = Form(...),
    tagline: str = Form(""),
    db: Session = Depends(get_db),
):
    if not name.strip():
        return render(
            request,
            "users.html",
            db,
            {"error": "Le nom est obligatoire.", "edit_user": None},
            status_code=400,
        )

    user = User(name=name.strip(), tagline=tagline.strip())
    db.add(user)
    db.commit()
    db.refresh(user)
    return redirect_with_user("/", user.id)


@app.post("/users/{user_id}/switch")
def switch_user(user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    return redirect_with_user("/", user.id)


@app.get("/users/{user_id}/edit")
def edit_user_page(user_id: int, request: Request, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    return render(request, "users.html", db, {"error": None, "edit_user": user})


@app.post("/users/{user_id}/edit")
def update_user(
    user_id: int,
    request: Request,
    name: str = Form(...),
    tagline: str = Form(""),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if not name.strip():
        return render(
            request,
            "users.html",
            db,
            {"error": "Le nom est obligatoire.", "edit_user": user},
            status_code=400,
        )
    user.name = name.strip()
    user.tagline = tagline.strip()
    db.commit()
    return redirect_with_user("/users", user.id)


@app.get("/users/{user_id}/delete")
def delete_user_confirm(user_id: int, request: Request, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    vehicle_count = len(user_vehicles(db, user))
    return render(
        request,
        "user_delete.html",
        db,
        {"target_user": user, "vehicle_count": vehicle_count},
    )


@app.post("/users/{user_id}/delete")
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    db.delete(user)
    db.commit()

    remaining = list_users(db)
    response = RedirectResponse(url="/users" if not remaining else "/", status_code=303)
    if remaining:
        set_active_user_cookie(response, remaining[0].id)
    else:
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
        fuel_page_context(vehicles=vehicles, error=None),
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
            fuel_page_context(vehicles=vehicles, error=error),
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
    db: Session = Depends(get_db),
):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user

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
    )
    db.add(trip)
    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}", status_code=303)


@app.get("/vehicles/{vehicle_id}/maintenance")
def maintenance_page(vehicle_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user
    vehicle = get_owned_vehicle(db, user, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable")

    operations = list_maintenance_ops(db, vehicle_id)
    return render(
        request,
        "maintenance.html",
        db,
        {
            "vehicle": vehicle,
            "operations": operations,
            "stats": maintenance_stats(operations),
            "form": default_maintenance_form(),
            "error": None,
        },
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
            {
                "vehicle": vehicle,
                "operations": operations,
                "stats": maintenance_stats(operations),
                "form": form,
                "error": error,
            },
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
