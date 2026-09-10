"""RevFi SQLite layer — users and Sales CRM tables."""
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, Text,
    create_engine, inspect as sa_inspect, text,
)
from sqlalchemy.pool import StaticPool

_DB_PATH = None
_engine = None
metadata = MetaData()

users = Table("users", metadata,
    Column("username", String(100), primary_key=True),
    Column("password", String(200), nullable=False),
    Column("role", String(50), default="user"),
    Column("name", String(200), default=""),
    Column("email", String(200), default=""),
    Column("active", Boolean, default=True),
    Column("menus", Text, default="[]"),
    Column("theme", String(20), default="light"),
    Column("created", DateTime, default=datetime.utcnow),
)

products = Table("products", metadata,
    Column("id", String(20), primary_key=True),
    Column("slug", String(80), nullable=False, unique=True, index=True),
    Column("name", String(200), nullable=False),
    Column("icon", String(20), default="📦"),
    Column("sort_order", Integer, default=0),
    Column("active", Boolean, default=True),
    Column("created_at", DateTime, default=datetime.utcnow),
)

sales_leads = Table("sales_leads", metadata,
    Column("id", String(20), primary_key=True),
    Column("restaurant_name", String(300), nullable=False),
    Column("contact_name", String(300), default=""),
    Column("contact_email", String(300), default=""),
    Column("contact_phone", String(100), default=""),
    Column("city", String(200), default=""),
    Column("cuisine_type", String(200), default=""),
    Column("stage", String(100), default="New", index=True),
    Column("priority", String(50), default="medium"),
    Column("plan_interest", String(50), default="basic"),
    Column("billing_cycle", String(20), default="monthly"),
    Column("source", String(50), default="manual"),
    Column("referred_by", String(300), default=""),
    Column("product_id", String(20), default="", index=True),
    Column("assigned_to", String(100), default=""),
    Column("notes", Text, default=""),
    Column("next_followup", String(20), default=""),
    Column("activity_log", Text, default="[]"),
    Column("created_by", String(100), default=""),
    Column("created_at", DateTime, default=datetime.utcnow),
    Column("updated_at", DateTime, default=datetime.utcnow),
)

sales_subscriptions = Table("sales_subscriptions", metadata,
    Column("id", String(20), primary_key=True),
    Column("restaurant_name", String(300), nullable=False),
    Column("plan", String(50), default="basic"),
    Column("billing_cycle", String(20), default="monthly"),
    Column("amount", Float, default=0.0),
    Column("status", String(50), default="active"),
    Column("start_date", String(20), default=""),
    Column("next_billing", String(20), default=""),
    Column("assigned_to", String(100), default=""),
    Column("product_id", String(20), default="", index=True),
    Column("contact_email", String(300), default=""),
    Column("contact_name", String(300), default=""),
    Column("created_by", String(100), default=""),
    Column("created_at", DateTime, default=datetime.utcnow),
    Column("updated_at", DateTime, default=datetime.utcnow),
)

sales_billing = Table("sales_billing", metadata,
    Column("id", String(20), primary_key=True),
    Column("subscription_id", String(20), nullable=False, index=True),
    Column("restaurant_name", String(300), default=""),
    Column("contact_email", String(300), default=""),
    Column("contact_name", String(300), default=""),
    Column("plan", String(50), default="basic"),
    Column("billing_cycle", String(20), default="monthly"),
    Column("amount", Float, default=0.0),
    Column("status", String(50), default="pending", index=True),
    Column("due_date", String(20), default=""),
    Column("issued_date", String(20), default=""),
    Column("paid_date", String(20), default=""),
    Column("paid_amount", Float, default=0.0),
    Column("payment_method", String(50), default=""),
    Column("invoice_number", String(100), default=""),
    Column("notes", Text, default=""),
    Column("last_reminder", String(50), default=""),
    Column("created_by", String(100), default=""),
    Column("created_at", DateTime, default=datetime.utcnow),
    Column("updated_at", DateTime, default=datetime.utcnow),
    Column("updated_by", String(100), default=""),
)


def get_engine():
    global _engine, _DB_PATH
    if _engine:
        return _engine
    base = Path(__file__).parent
    db_path = base / "data" / "revfi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _DB_PATH = str(db_path)
    _engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=StaticPool,
        echo=False,
    )
    with _engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()
    return _engine


def _ensure_column(engine, table, column, ddl):
    insp = sa_inspect(engine)
    if table not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns(table)}
    if column not in cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def init_db():
    engine = get_engine()
    metadata.create_all(engine)
    _ensure_column(engine, "sales_leads", "referred_by", "referred_by VARCHAR(300) DEFAULT ''")
    _ensure_column(engine, "sales_leads", "product_id", "product_id VARCHAR(20) DEFAULT ''")
    _ensure_column(engine, "sales_subscriptions", "product_id", "product_id VARCHAR(20) DEFAULT ''")
    return engine
