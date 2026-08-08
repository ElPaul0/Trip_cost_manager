from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    consumption_l_per_100km: Mapped[float] = mapped_column(Float, nullable=False)
    fuel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    maintenance_per_km: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    amortization_per_km: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    trips: Mapped[list["Trip"]] = relationship(
        "Trip",
        back_populates="vehicle",
        cascade="all, delete-orphan",
        order_by="desc(Trip.trip_date)",
    )


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    departure: Mapped[str] = mapped_column(String(160), nullable=False)
    arrival: Mapped[str] = mapped_column(String(160), nullable=False)
    trip_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    fuel_price_per_liter: Mapped[float] = mapped_column(Float, nullable=False)
    tolls: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    passengers: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fuel_cost: Mapped[float] = mapped_column(Float, nullable=False)
    maintenance_cost: Mapped[float] = mapped_column(Float, nullable=False)
    amortization_cost: Mapped[float] = mapped_column(Float, nullable=False)
    total_cost: Mapped[float] = mapped_column(Float, nullable=False)
    cost_per_person: Mapped[float] = mapped_column(Float, nullable=False)
    energy_used: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    co2_kg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    vehicle: Mapped["Vehicle"] = relationship("Vehicle", back_populates="trips")
