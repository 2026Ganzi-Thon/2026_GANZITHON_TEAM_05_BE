"""Letit v1 common schema. IDs use UUID; Kakao place IDs remain strings.

Use UUID(as_uuid=True) on PostgreSQL and SQLite to keep test parity.
"""
import uuid
from datetime import datetime, date, timezone
from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, String,
    Text, UniqueConstraint, JSON, func
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

JSONField = JSON().with_variant(JSONB, 'postgresql')


def new_uuid():
    return uuid.uuid4()


class User(Base):
    __tablename__ = 'users'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserDefaults(Base):
    __tablename__ = 'user_defaults'
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    mood: Mapped[str | None] = mapped_column(String(30))
    energy_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    barriers: Mapped[list] = mapped_column(JSONField, nullable=False, default=list)
    distance_limit: Mapped[str] = mapped_column(String(10), nullable=False, default='10min')
    time_limit: Mapped[str] = mapped_column(String(10), nullable=False, default='30min')
    budget_limit: Mapped[int | None] = mapped_column(Integer)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (CheckConstraint('energy_level BETWEEN 0 AND 4', name='valid_defaults_energy'),)


class Recommendation(Base):
    __tablename__ = 'recommendations'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    energy_level: Mapped[int] = mapped_column(Integer, nullable=False)
    mission_type: Mapped[str] = mapped_column(String(20), nullable=False)
    place_category: Mapped[str | None] = mapped_column(String(50))
    steps: Mapped[list] = mapped_column(JSONField, nullable=False)
    conditions: Mapped[dict] = mapped_column(JSONField, nullable=False)
    estimated_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("mission_type IN ('place','non_place')", name='valid_recommendation_type'),
        CheckConstraint('energy_level BETWEEN 0 AND 4', name='valid_recommendation_energy'),
    )


class Mission(Base):
    __tablename__ = 'missions'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), index=True, nullable=False)
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey('recommendations.id'))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    energy_level: Mapped[int] = mapped_column(Integer, nullable=False)
    mission_type: Mapped[str] = mapped_column(String(20), nullable=False)
    place_category: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='in_progress')
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    place_info: Mapped[dict | None] = mapped_column(JSONField)
    archived_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        CheckConstraint("status IN ('in_progress','paused','completed','abandoned','replaced')", name='valid_mission_status'),
        CheckConstraint("mission_type IN ('place','non_place')", name='valid_mission_type'),
        CheckConstraint('energy_level BETWEEN 0 AND 4', name='valid_mission_energy'),
    )


class MissionStep(Base):
    __tablename__ = 'mission_steps'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    mission_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('missions.id', ondelete='CASCADE'), index=True, nullable=False)
    step_order: Mapped[int] = mapped_column('step_order', Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (UniqueConstraint('mission_id', 'step_order'), CheckConstraint('step_order BETWEEN 1 AND 5', name='valid_step_order'))


class MissionReview(Base):
    __tablename__ = 'mission_reviews'
    mission_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('missions.id', ondelete='CASCADE'), primary_key=True)
    mood_after: Mapped[str | None] = mapped_column(String(30))
    one_line: Mapped[str | None] = mapped_column(Text)
    retry_activity: Mapped[str | None] = mapped_column(Text)
    good_point: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyRecord(Base):
    __tablename__ = 'daily_records'
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), primary_key=True)
    record_date: Mapped[date] = mapped_column('record_date', Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    ever_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (CheckConstraint("status IN ('completed','partial','rest','undecided')", name='valid_daily_status'),)


class UserFlower(Base):
    __tablename__ = 'user_flowers'
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), primary_key=True)
    flower_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('flower_number BETWEEN 1 AND 18', name='valid_flower_number'),)
