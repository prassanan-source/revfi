"""
Revfi — Subscription Management Platform
Flask/SQLAlchemy/SQLite · aligned with Raya Finance & Temple apps
"""

import os, json, uuid, smtplib, ssl, io, re, html, sqlite3, zipfile, requests as http_requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from datetime import datetime, date, timedelta
from functools import wraps
from xml.etree import ElementTree as ET
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, jsonify, abort)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ── App Setup ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "revfi-dev-secret-change-in-prod")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(DATA_DIR, 'revfi.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ── Models ───────────────────────────────────────────────────────────────────

class User(db.Model):
    __tablename__ = "users"
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(120), nullable=False)
    email    = db.Column(db.String(200), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)
    role     = db.Column(db.String(30), default="sales")   # admin | sales
    active   = db.Column(db.Boolean, default=True)
    created  = db.Column(db.DateTime, default=datetime.utcnow)


class Product(db.Model):
    __tablename__ = "products"
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    slug        = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.Text)
    icon        = db.Column(db.String(10), default="📦")
    color       = db.Column(db.String(10), default="#2E7D5E")
    active      = db.Column(db.Boolean, default=True)
    plans       = db.relationship("Plan", backref="product", lazy=True, cascade="all, delete-orphan")


class Plan(db.Model):
    __tablename__ = "plans"
    id          = db.Column(db.Integer, primary_key=True)
    product_id  = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    name        = db.Column(db.String(100), nullable=False)
    price       = db.Column(db.Float, nullable=False)
    cadence     = db.Column(db.String(20), default="monthly")  # monthly|quarterly|annual
    features    = db.Column(db.Text)
    active      = db.Column(db.Boolean, default=True)


class Lead(db.Model):
    __tablename__ = "leads"
    id           = db.Column(db.Integer, primary_key=True)
    company      = db.Column(db.String(200), nullable=False)
    contact      = db.Column(db.String(200))
    email        = db.Column(db.String(200))
    phone        = db.Column(db.String(50))
    product_id   = db.Column(db.Integer, db.ForeignKey("products.id"))
    plan_id      = db.Column(db.Integer, db.ForeignKey("plans.id"))
    status       = db.Column(db.String(50), default="New")
    assignee_id  = db.Column(db.Integer, db.ForeignKey("users.id"))
    referral_id  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    referral_pct    = db.Column(db.Float,   default=0)
    referral_months = db.Column(db.Integer, default=6)   # consecutive months before referral pays out
    # Discount fields
    discount_type   = db.Column(db.String(30),  default="none")  # none|pct|fixed|waive_setup|free_months
    discount_value  = db.Column(db.Float,        default=0)       # % or $ amount or # months
    discount_reason = db.Column(db.String(200),  default="")      # e.g. "Early adopter"
    setup_fee       = db.Column(db.Float,        default=300)     # one-time setup fee (0 = waived)
    notes           = db.Column(db.Text)
    value           = db.Column(db.Float,        default=0)       # monthly subscription fee
    billing_status  = db.Column(db.String(20),   default="active")  # active | unsubscribed
    unsubscribed_at = db.Column(db.DateTime,     nullable=True)
    resubscribed_at = db.Column(db.DateTime,     nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product  = db.relationship("Product", foreign_keys=[product_id])
    plan     = db.relationship("Plan",    foreign_keys=[plan_id])
    assignee = db.relationship("User",    foreign_keys=[assignee_id])
    referral = db.relationship("User",    foreign_keys=[referral_id])


class Agreement(db.Model):
    __tablename__ = "agreements"
    id           = db.Column(db.Integer, primary_key=True)
    ref          = db.Column(db.String(30), unique=True, nullable=False)
    lead_id      = db.Column(db.Integer, db.ForeignKey("leads.id"))
    signer_name  = db.Column(db.String(200))
    signer_title = db.Column(db.String(200))
    status       = db.Column(db.String(30), default="Pending Signature")  # Pending Signature|Signed|Declined
    sent_at      = db.Column(db.DateTime, default=datetime.utcnow)
    signed_at    = db.Column(db.DateTime, nullable=True)
    notes           = db.Column(db.Text)
    # Snapshot of discount at time of agreement send
    discount_type   = db.Column(db.String(30),  default="none")
    discount_value  = db.Column(db.Float,        default=0)
    discount_reason = db.Column(db.String(200),  default="")
    setup_fee       = db.Column(db.Float,        default=0)
    final_monthly   = db.Column(db.Float,        default=0)  # discounted monthly amount
    terms_body      = db.Column(db.Text)  # filled terms snapshot at send time

    lead = db.relationship("Lead", backref="agreements")


class AgreementTemplate(db.Model):
    __tablename__ = "agreement_templates"
    id           = db.Column(db.Integer, primary_key=True)
    title        = db.Column(db.String(200), default="Subscription Agreement")
    company_name = db.Column(db.String(200), default="Karyva.ai")  # {{company}}
    body         = db.Column(db.Text, default="")
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow)


class Invoice(db.Model):
    __tablename__ = "invoices"
    id         = db.Column(db.Integer, primary_key=True)
    ref        = db.Column(db.String(30), unique=True, nullable=False)
    lead_id    = db.Column(db.Integer, db.ForeignKey("leads.id"))
    amount     = db.Column(db.Float, nullable=False)
    cadence    = db.Column(db.String(20), default="monthly")
    due_date   = db.Column(db.Date, nullable=False)
    status     = db.Column(db.String(20), default="Sent")  # Sent|Paid|Overdue|Void
    sent_at    = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at    = db.Column(db.DateTime, nullable=True)
    notes           = db.Column(db.Text)
    square_invoice_id = db.Column(db.String(200), nullable=True)  # Square invoice ID after send
    square_public_url = db.Column(db.String(500), nullable=True)  # Square-hosted payment page

    lead = db.relationship("Lead", backref="invoices")


class Commission(db.Model):
    __tablename__ = "commissions"
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"))
    lead_id    = db.Column(db.Integer, db.ForeignKey("leads.id"))
    kind       = db.Column(db.String(20))   # sales | referral
    rate       = db.Column(db.Float)
    amount     = db.Column(db.Float)
    period     = db.Column(db.String(20))   # e.g. "2026-09"
    status     = db.Column(db.String(20), default="Pending")  # Pending|Paid
    notes      = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id])
    lead = db.relationship("Lead", foreign_keys=[lead_id])


class SquareSettings(db.Model):
    __tablename__ = "square_settings"
    id              = db.Column(db.Integer, primary_key=True)
    access_token    = db.Column(db.String(500), default="")
    location_id     = db.Column(db.String(200), default="")
    environment     = db.Column(db.String(20),  default="sandbox")   # sandbox | production
    webhook_secret  = db.Column(db.String(200), default="")
    updated_at      = db.Column(db.DateTime,    default=datetime.utcnow)


class EmailSettings(db.Model):
    __tablename__ = "email_settings"
    id         = db.Column(db.Integer, primary_key=True)
    smtp_host  = db.Column(db.String(200), default="smtp.gmail.com")
    smtp_port  = db.Column(db.Integer,     default=587)
    smtp_user  = db.Column(db.String(200), default="")
    smtp_pass  = db.Column(db.String(200), default="")
    from_name  = db.Column(db.String(200), default="Karyva.ai Team")
    from_email = db.Column(db.String(200), default="")
    use_tls    = db.Column(db.Boolean,     default=True)
    updated_at = db.Column(db.DateTime,    default=datetime.utcnow)


class SubscriptionActivity(db.Model):
    __tablename__ = "subscription_activities"
    id           = db.Column(db.Integer, primary_key=True)
    lead_id      = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False)
    action       = db.Column(db.String(30), nullable=False)
    channel      = db.Column(db.String(30), default="sales")
    notes        = db.Column(db.Text, default="")
    happened_at  = db.Column(db.Date, nullable=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    lead = db.relationship("Lead", backref=db.backref("sub_activities", lazy="dynamic",
                                                      order_by="SubscriptionActivity.created_at.desc()"))
    user = db.relationship("User", foreign_keys=[user_id])


# ── Helpers ──────────────────────────────────────────────────────────────────
PIPELINE_STAGES = ["New","Contacted","Demo Scheduled","Proposal Sent",
                   "Agreement Sent","Closed Won","Closed Lost"]

STAGE_COLORS = {
    "New": "secondary", "Contacted": "primary", "Demo Scheduled": "info",
    "Proposal Sent": "warning", "Agreement Sent": "cyan",
    "Closed Won": "success", "Closed Lost": "danger",
}

SALES_COMMISSION_RATE = 12.0   # default %
CADENCE_MULTIPLIERS = {"monthly": 1, "quarterly": 3, "annual": 12}

_FALLBACK_AGREEMENT_TERMS = """1. <b>Subscription.</b> {{client}} agrees to subscribe with <b>{{company}}</b> to <b>{{product}} — {{plan}}</b> at <b>{{price}}</b>, billed {{cadence}}.
2. <b>Payment.</b> Invoices are due within 14 days of issuance. Late payments may incur a 1.5% monthly fee.
3. <b>Term.</b> This agreement commences on the date signed and auto-renews unless cancelled with 30 days written notice.
4. <b>Cancellation.</b> Either party may terminate with 30 days written notice. No refunds for partial billing periods.
5. <b>Confidentiality.</b> Both parties agree to keep the terms of this agreement confidential.
6. <b>Governing Law.</b> This agreement is governed by the laws of the State of California."""

AGREEMENT_DEFAULT_FILE = os.path.join(BASE_DIR, "agreement_default.txt")
KARYVA_AGREEMENT_DOCX = os.path.join(BASE_DIR, "Karyva_AI_Agreement_Final.docx")


def default_agreement_terms():
    try:
        with open(AGREEMENT_DEFAULT_FILE, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    return _FALLBACK_AGREEMENT_TERMS


def extract_docx_text(fileobj):
    """Plain-text paragraphs from a .docx upload (no extra dependency)."""
    with zipfile.ZipFile(fileobj) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    w_p = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
    w_t = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
    lines = []
    for p in root.iter(w_p):
        parts = []
        for t in p.iter(w_t):
            if t.text:
                parts.append(t.text)
            if t.tail:
                parts.append(t.tail)
        lines.append("".join(parts).rstrip())
    return "\n".join(lines).strip()

# ── Email Sending ─────────────────────────────────────────────────────────────

def get_email_settings():
    s = EmailSettings.query.first()
    if not s:
        s = EmailSettings()
        db.session.add(s)
        db.session.commit()
    return s

def get_agreement_template():
    t = AgreementTemplate.query.first()
    dirty = False
    default_body = default_agreement_terms()
    if not t:
        t = AgreementTemplate(
            title="Subscription & Services Agreement",
            company_name="Karyva.ai",
            body=default_body,
        )
        db.session.add(t)
        dirty = True
    if not (t.company_name or "").strip():
        t.company_name = "Karyva.ai"
        dirty = True
    if not (t.body or "").strip() or "HOW THIS AGREEMENT IS ACCEPTED" not in (t.body or ""):
        t.body = default_body
        t.title = t.title or "Subscription & Services Agreement"
        dirty = True
    if dirty:
        db.session.commit()
    return t


def issuer_brand():
    return (get_agreement_template().company_name or "Karyva.ai").strip() or "Karyva.ai"


def agreement_term_context(agr, lead):
    prod = lead.product.name if lead.product else issuer_brand()
    plan = lead.plan.name if lead.plan else ""
    final_monthly = agr.final_monthly if agr.final_monthly else lead.value
    setup_fee = agr.setup_fee if agr.setup_fee else 0
    _, _, discount_label = calc_discounted_price(lead)
    cadence = (lead.plan.cadence if lead.plan else "monthly")
    date_sent = agr.sent_at.strftime("%B %d, %Y") if agr.sent_at else datetime.utcnow().strftime("%B %d, %Y")
    if setup_fee and setup_fee > 0:
        setup_txt = f"${setup_fee:,.2f} (due at signing)"
    elif lead.setup_fee == 0 and agr.discount_type == "waive_setup":
        setup_txt = "Waived"
    else:
        setup_txt = "$0.00"
    issuer = issuer_brand()
    return {
        "company": issuer,
        "vendor": issuer,
        "client": lead.company or "",
        "contact": lead.contact or "",
        "email": lead.email or "",
        "product": prod,
        "plan": plan,
        "price": f"${final_monthly:,.2f}/month",
        "cadence": cadence,
        "ref": agr.ref or "",
        "date": date_sent,
        "signer_name": agr.signer_name or lead.contact or "",
        "signer_title": agr.signer_title or "",
        "setup_fee": setup_txt,
        "discount": discount_label or agr.discount_reason or "",
        "notes": agr.notes or "",
    }


def fill_agreement_terms(body, agr, lead):
    ctx = agreement_term_context(agr, lead)
    safe = {k: html.escape(str(v), quote=False) for k, v in ctx.items()}

    def repl(m):
        key = m.group(1)
        return safe.get(key, m.group(0))

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", repl, body or "")


def _pdf_safe_line(text):
    s = text.replace("&", "&amp;")
    s = s.replace("<b>", "\x00b\x00").replace("</b>", "\x00/b\x00")
    s = s.replace("<", "&lt;").replace(">", "&gt;")
    return s.replace("\x00b\x00", "<b>").replace("\x00/b\x00", "</b>")


def snapshot_agreement_terms(agr, lead):
    tmpl = get_agreement_template()
    agr.terms_body = fill_agreement_terms(tmpl.body, agr, lead)


def generate_agreement_pdf(agr, lead):
    """Generate a branded agreement PDF and return bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            topMargin=0.75*inch, bottomMargin=0.75*inch,
                            leftMargin=1*inch, rightMargin=1*inch)
    styles = getSampleStyleSheet()
    navy   = colors.HexColor("#0F1C2E")
    gold   = colors.HexColor("#C9A84C")
    grey   = colors.HexColor("#6B7280")
    light  = colors.HexColor("#F9FAFB")

    title_style = ParagraphStyle("AgrTitle", parent=styles["Normal"], fontSize=20,
                                 fontName="Helvetica-Bold", textColor=navy,
                                 spaceAfter=10, leading=26)
    sub_style   = ParagraphStyle("AgrSub", parent=styles["Normal"], fontSize=10,
                                 fontName="Helvetica", textColor=grey,
                                 spaceBefore=2, spaceAfter=14, leading=14)
    head_style  = ParagraphStyle("AgrHead", parent=styles["Normal"], fontSize=12,
                                 fontName="Helvetica-Bold", textColor=navy,
                                 spaceBefore=14, spaceAfter=6, leading=16)
    body_style  = ParagraphStyle("AgrBody", parent=styles["Normal"], fontSize=9,
                                 fontName="Helvetica", textColor=colors.HexColor("#374151"),
                                 leading=13)
    small_style = ParagraphStyle("AgrSmall", parent=styles["Normal"], fontSize=8,
                                 fontName="Helvetica", textColor=grey, leading=12)
    hdr_style   = ParagraphStyle("AgrBanner", parent=styles["Normal"], leading=18)

    issuer = issuer_brand()
    prod  = lead.product.name if lead.product else issuer
    plan  = lead.plan.name    if lead.plan    else ""
    # Use agreement's snapshotted final price, fallback to calc
    final_monthly = agr.final_monthly if agr.final_monthly else lead.value
    setup_fee     = agr.setup_fee     if agr.setup_fee     else 0
    _, _, discount_label = calc_discounted_price(lead)
    price = f"${final_monthly:,.2f}/month"
    date_sent = agr.sent_at.strftime("%B %d, %Y")

    story = []

    issuer_esc = html.escape(issuer)
    header_data = [[Paragraph(
        f'<font color="#C9A84C" size="13"><b>{issuer_esc}</b></font><br/>'
        f'<font size="8" color="#8A9BB0">Subscription &amp; Services Agreement</font>',
        hdr_style,
    )]]
    header_tbl = Table(header_data, colWidths=[6.5*inch])
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), navy),
        ("TOPPADDING",    (0,0), (-1,-1), 14),
        ("BOTTOMPADDING", (0,0), (-1,-1), 14),
        ("LEFTPADDING",   (0,0), (-1,-1), 16),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 14))
    story.append(Paragraph(
        f"Reference: {html.escape(agr.ref)} &nbsp;&nbsp;·&nbsp;&nbsp; Date: {date_sent}",
        sub_style,
    ))
    story.append(HRFlowable(width="100%", thickness=1, color=gold, spaceAfter=12))

    terms_src = (agr.terms_body or "").strip() or fill_agreement_terms(get_agreement_template().body, agr, lead)
    heading_names = {
        "HOW THIS AGREEMENT IS ACCEPTED",
        "NO HANDWRITTEN OR ELECTRONIC SIGNATURE IS REQUIRED.",
        "PARTIES",
        "CORE SUBSCRIPTION TERMS",
        "ACCEPTANCE BY EMAIL REPLY",
        "Acceptance Process",
        "Authorized Acceptor",
        "Example Acceptance Email",
        "Record Retention",
        "AND",
    }
    for raw in terms_src.splitlines():
        t = raw.strip()
        if not t:
            story.append(Spacer(1, 8))
            continue
        markup = _pdf_safe_line(t)
        plain = re.sub(r"<[^>]+>", "", t).strip()
        is_head = (
            plain in heading_names
            or (plain.upper() == plain and 3 < len(plain) < 72 and any(c.isalpha() for c in plain))
            or bool(re.match(r"^[A-J]\.\s", plain))
            or bool(re.match(r"^[A-J]\.\d", plain))
        )
        if is_head:
            story.append(Paragraph(markup, head_style))
        else:
            story.append(Paragraph(markup, body_style))
            story.append(Spacer(1, 3))

    if agr.notes:
        story.append(Spacer(1, 10))
        story.append(Paragraph("Additional Notes", head_style))
        story.append(Paragraph(_pdf_safe_line(agr.notes), body_style))

    doc.build(story)
    return buf.getvalue()


def calc_discounted_price(lead):
    """
    Return (final_monthly, setup_fee, discount_label) after applying discounts.
    discount_type: none | pct | fixed | waive_setup | free_months
    """
    base = float(lead.value or 0)
    setup = float(lead.setup_fee or 0)
    dtype = lead.discount_type or "none"
    dval  = float(lead.discount_value or 0)
    reason = lead.discount_reason or ""

    if dtype == "pct":
        final = round(base * (1 - dval / 100), 2)
        label = f"{dval:.0f}% discount ({reason})" if reason else f"{dval:.0f}% discount"
    elif dtype == "fixed":
        final = round(max(base - dval, 0), 2)
        label = f"${dval:,.0f} off/mo ({reason})" if reason else f"${dval:,.0f} off/mo"
    elif dtype == "waive_setup":
        final = base
        setup = 0
        label = f"Setup fee waived ({reason})" if reason else "Setup fee waived"
    elif dtype == "free_months":
        final = base
        label = f"First {int(dval)} month(s) free ({reason})" if reason else f"First {int(dval)} month(s) free"
    else:
        final = base
        label = ""

    return final, setup, label


def get_square_settings():
    s = SquareSettings.query.first()
    if not s:
        s = SquareSettings()
        db.session.add(s)
        db.session.commit()
    return s

def square_api(method, path, payload=None):
    """Make a Square API call. Returns (response_dict, error_str)."""
    cfg = get_square_settings()
    if not cfg.access_token:
        return None, "Square not configured. Go to Settings → Square to add your credentials."
    base = "https://connect.squareupsandbox.com" if cfg.environment == "sandbox" else "https://connect.squareup.com"
    headers = {
        "Authorization": f"Bearer {cfg.access_token}",
        "Content-Type":  "application/json",
        "Square-Version": "2025-04-16",
    }
    try:
        resp = http_requests.request(method, f"{base}{path}", headers=headers,
                                     json=payload, timeout=20)
        data = resp.json()
        if resp.status_code >= 400:
            errs = data.get("errors", [{}])
            return None, errs[0].get("detail", f"Square error {resp.status_code}")
        return data, None
    except Exception as e:
        return None, str(e)

def square_find_or_create_customer(lead):
    """Find existing Square customer by email or create one. Returns (customer_id, error)."""
    # Search by email
    body = {"query": {"filter": {"email_address": {"exact": lead.email}}}}
    data, err = square_api("POST", "/v2/customers/search", body)
    if err:
        return None, err
    customers = data.get("customers", [])
    if customers:
        return customers[0]["id"], None
    # Create new customer
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "given_name":  (lead.contact or "").split()[0] if lead.contact else lead.company,
        "family_name": " ".join((lead.contact or "").split()[1:]) or lead.company,
        "email_address": lead.email,
        "company_name":  lead.company,
        "reference_id":  f"revfi-lead-{lead.id}",
    }
    data, err = square_api("POST", "/v2/customers", body)
    if err:
        return None, err
    return data["customer"]["id"], None

def square_send_invoice(inv):
    """
    Full Square invoice flow:
      1. Find/create customer
      2. Create order
      3. Create invoice
      4. Publish invoice
    Returns (public_url, square_invoice_id, error).
    """
    lead = inv.lead
    if not lead:
        return None, None, "No subscriber linked to this invoice."
    if not lead.email:
        return None, None, "Subscriber has no email address."

    cfg = get_square_settings()
    if not cfg.access_token or not cfg.location_id:
        return None, None, "Square not configured. Add your Access Token and Location ID in Settings → Square."

    # Step 1 — Customer
    customer_id, err = square_find_or_create_customer(lead)
    if err:
        return None, None, f"Customer: {err}"

    # Step 2 — Order (amount in cents)
    amount_cents = int(round(inv.amount * 100))
    prod_name = ""
    if lead.product:
        prod_name = lead.product.name
    if lead.plan:
        prod_name += f" — {lead.plan.name}"
    order_body = {
        "idempotency_key": str(uuid.uuid4()),
        "order": {
            "location_id": cfg.location_id,
            "customer_id": customer_id,
            "line_items": [{
                "name":     prod_name or "Karyva.ai Subscription",
                "quantity": "1",
                "base_price_money": {"amount": amount_cents, "currency": "USD"},
                "note": f"{inv.cadence.capitalize()} subscription · {inv.ref}",
            }],
            "reference_id": inv.ref,
        }
    }
    data, err = square_api("POST", "/v2/orders", order_body)
    if err:
        return None, None, f"Order: {err}"
    order_id = data["order"]["id"]

    # Step 3 — Create invoice
    inv_body = {
        "idempotency_key": str(uuid.uuid4()),
        "invoice": {
            "location_id":    cfg.location_id,
            "order_id":       order_id,
            "invoice_number": inv.ref,
            "title":          f"Karyva.ai — {prod_name or 'Subscription'}",
            "description":    f"{inv.cadence.capitalize()} subscription invoice",
            "delivery_method": "EMAIL",
            "primary_recipient": {"customer_id": customer_id},
            "payment_requests": [{
                "request_type": "BALANCE",
                "due_date":     inv.due_date.strftime("%Y-%m-%d"),
                "automatic_payment_source": "NONE",
                "tipping_enabled": False,
            }],
            "accepted_payment_methods": {
                "card":             True,
                "square_gift_card": False,
                "bank_account":     False,
                "buy_now_pay_later": False,
                "cash_app_pay":     False,
            },
            "sale_or_service_date": inv.due_date.strftime("%Y-%m-%d"),
        }
    }
    data, err = square_api("POST", "/v2/invoices", inv_body)
    if err:
        return None, None, f"Invoice create: {err}"
    sq_invoice_id = data["invoice"]["id"]
    version       = data["invoice"]["version"]

    # Step 4 — Publish
    pub_body = {"version": version, "idempotency_key": str(uuid.uuid4())}
    data, err = square_api("POST", f"/v2/invoices/{sq_invoice_id}/publish", pub_body)
    if err:
        return None, None, f"Publish: {err}"

    public_url = data["invoice"].get("public_url", "")
    return public_url, sq_invoice_id, None


def send_email(to_addr, subject, html_body, to_name="", attachment_bytes=None, attachment_name=None):
    """Send email via configured SMTP. Returns (True, None) or (False, error_msg)."""
    try:
        cfg = get_email_settings()
        if not cfg.smtp_user or not cfg.smtp_pass or not cfg.from_email:
            return False, "Email not configured. Go to Settings → Email to set up SMTP."

        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = f"{cfg.from_name} <{cfg.from_email}>"
        msg["To"]      = f"{to_name} <{to_addr}>" if to_name else to_addr
        # Set Reply-To to the SMTP user so replies come back to us
        msg["Reply-To"] = cfg.smtp_user

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html_body, "html"))
        msg.attach(alt)

        if attachment_bytes and attachment_name:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment_bytes)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{attachment_name}"')
            msg.attach(part)

        context = ssl.create_default_context()
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
            if cfg.use_tls:
                server.starttls(context=context)
            server.login(cfg.smtp_user, cfg.smtp_pass)
            server.sendmail(cfg.from_email, to_addr, msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)

def agreement_email_html(agr, lead):
    issuer = issuer_brand()
    prod  = lead.product.name if lead.product else issuer
    plan  = lead.plan.name    if lead.plan    else ""
    final_monthly = agr.final_monthly if agr.final_monthly else lead.value
    setup_fee     = agr.setup_fee     if agr.setup_fee     else 0
    _, _, discount_label = calc_discounted_price(lead)
    price = f"${final_monthly:,.2f}/mo"
    if lead.value != final_monthly:
        discount_rows = (
            f'<tr><td style="padding:10px 14px;font-weight:700;">Standard Fee</td>'
            f'<td style="padding:10px 14px;text-decoration:line-through;color:#9CA3AF;">${lead.value:,.2f}/mo</td></tr>'
            f'<tr style="background:#f3f4f6;"><td style="padding:10px 14px;font-weight:700;">Discount</td>'
            f'<td style="padding:10px 14px;color:#D97706;">{discount_label}</td></tr>'
            f'<tr><td style="padding:10px 14px;font-weight:700;">Your Monthly Fee</td>'
            f'<td style="padding:10px 14px;color:#059669;font-weight:700;">{price}</td></tr>'
        )
    else:
        discount_rows = (
            f'<tr><td style="padding:10px 14px;font-weight:700;">Monthly Fee</td>'
            f'<td style="padding:10px 14px;color:#059669;font-weight:700;">{price}</td></tr>'
        )
    if setup_fee and setup_fee > 0:
        discount_rows += (
            f'<tr style="background:#f3f4f6;"><td style="padding:10px 14px;font-weight:700;">Setup Fee</td>'
            f'<td style="padding:10px 14px;">${setup_fee:,.2f} (one-time)</td></tr>'
        )
    elif agr.discount_type == "waive_setup":
        discount_rows += (
            f'<tr style="background:#f3f4f6;"><td style="padding:10px 14px;font-weight:700;">Setup Fee</td>'
            f'<td style="padding:10px 14px;color:#059669;">Waived</td></tr>'
        )
    return f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#222;">
  <div style="background:#0F1C2E;padding:28px 32px;border-radius:10px 10px 0 0;">
    <div style="color:#C9A84C;font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;">{issuer}</div>
    <div style="color:#fff;font-size:22px;font-weight:800;margin-top:4px;">Subscription Agreement</div>
  </div>
  <div style="background:#f9fafb;padding:32px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;">
    <p>Dear <strong>{agr.signer_name or lead.contact}</strong>,</p>
    <p>Please review and sign your <strong>{prod} {plan}</strong> subscription agreement with {issuer}.</p>
    <table style="width:100%;border-collapse:collapse;margin:24px 0;font-size:14px;">
      <tr style="background:#f3f4f6;"><td style="padding:10px 14px;font-weight:700;width:40%;">Agreement Ref</td><td style="padding:10px 14px;">{agr.ref}</td></tr>
      <tr><td style="padding:10px 14px;font-weight:700;">Company</td><td style="padding:10px 14px;">{lead.company}</td></tr>
      <tr style="background:#f3f4f6;"><td style="padding:10px 14px;font-weight:700;">Product</td><td style="padding:10px 14px;">{prod} — {plan}</td></tr>
      {discount_rows}
      <tr style="background:#f3f4f6;"><td style="padding:10px 14px;font-weight:700;">Date Sent</td><td style="padding:10px 14px;">{agr.sent_at.strftime('%B %d, %Y')}</td></tr>
    </table>
    {f'<p style="background:#fffbeb;border-left:4px solid #C9A84C;padding:12px 16px;border-radius:4px;">{agr.notes}</p>' if agr.notes else ''}
    <div style="background:#F0FFF4;border:2px solid #6EE7B7;border-radius:8px;padding:18px 20px;margin:24px 0;">
      <p style="font-size:15px;font-weight:700;color:#065F46;margin:0 0 10px;">How to confirm or decline this agreement</p>
      <p style="margin:0 0 8px;color:#374151;font-size:14px;">
        Simply <strong>reply to this email</strong> with one of the following words in your message:
      </p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tr>
          <td style="padding:8px 12px;background:#D1FAE5;border-radius:6px;font-weight:700;color:#065F46;width:50%;">
            ✅ To <u>accept</u>: reply with<br>
            <span style="font-size:16px;letter-spacing:1px;">"Agreed"</span> or <span style="font-size:16px;letter-spacing:1px;">"Confirmed"</span>
          </td>
          <td style="width:12px;"></td>
          <td style="padding:8px 12px;background:#FEE2E2;border-radius:6px;font-weight:700;color:#991B1B;width:50%;">
            ❌ To <u>decline</u>: reply with<br>
            <span style="font-size:16px;letter-spacing:1px;">"Declined"</span>
          </td>
        </tr>
      </table>
      <p style="margin:10px 0 0;color:#6B7280;font-size:12px;">
        Your reply is automatically detected by our system. The agreement reference <strong>{agr.ref}</strong> 
        must appear in the subject line (it will be there if you hit Reply). 
        Your response will update the agreement status within minutes.
      </p>
    </div>
    <p style="margin-top:32px;color:#6b7280;font-size:13px;">
      {issuer} · <a href="mailto:shan@revfi.ai" style="color:#C9A84C;">shan@revfi.ai</a>
    </p>
  </div>
</div>"""

def invoice_email_html(inv, lead):
    issuer = issuer_brand()
    prod  = lead.product.name if lead and lead.product else issuer
    plan  = lead.plan.name    if lead and lead.plan    else ""
    # Use the invoice amount (already discounted at creation time)
    amount = inv.amount
    return f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#222;">
  <div style="background:#0F1C2E;padding:28px 32px;border-radius:10px 10px 0 0;">
    <div style="color:#C9A84C;font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;">{issuer}</div>
    <div style="color:#fff;font-size:22px;font-weight:800;margin-top:4px;">Invoice {inv.ref}</div>
  </div>
  <div style="background:#f9fafb;padding:32px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;">
    <p>Dear {lead.contact if lead else 'Subscriber'},</p>
    <p>Please find your invoice for <strong>{prod} {plan}</strong> subscription.</p>
    <table style="width:100%;border-collapse:collapse;margin:24px 0;font-size:14px;">
      <tr style="background:#f3f4f6;"><td style="padding:10px 14px;font-weight:700;width:40%;">Invoice</td><td style="padding:10px 14px;">{inv.ref}</td></tr>
      <tr><td style="padding:10px 14px;font-weight:700;">Company</td><td style="padding:10px 14px;">{lead.company if lead else '—'}</td></tr>
      <tr style="background:#f3f4f6;"><td style="padding:10px 14px;font-weight:700;">Product</td><td style="padding:10px 14px;">{prod} — {plan}</td></tr>
      <tr><td style="padding:10px 14px;font-weight:700;">Amount Due</td><td style="padding:10px 14px;color:#059669;font-weight:800;font-size:18px;">${amount:,.2f}</td></tr>
      <tr style="background:#f3f4f6;"><td style="padding:10px 14px;font-weight:700;">Schedule</td><td style="padding:10px 14px;">{inv.cadence.capitalize()}</td></tr>
      <tr><td style="padding:10px 14px;font-weight:700;">Due Date</td><td style="padding:10px 14px;color:#DC2626;font-weight:700;">{inv.due_date.strftime('%B %d, %Y')}</td></tr>
    </table>
    {f'<p style="background:#fffbeb;border-left:4px solid #C9A84C;padding:12px 16px;border-radius:4px;">{inv.notes}</p>' if inv.notes else ''}
    <p style="margin-top:32px;color:#6b7280;font-size:13px;">
      {issuer} · <a href="mailto:shan@revfi.ai" style="color:#C9A84C;">shan@revfi.ai</a>
    </p>
  </div>
</div>"""

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        u = User.query.get(session["user_id"])
        if not u or u.role != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

def current_user():
    if "user_id" in session:
        return User.query.get(session["user_id"])
    return None

def next_ref(model, prefix):
    """Generate sequential reference like INV-2026-0042"""
    year = datetime.utcnow().year
    count = db.session.query(db.func.count(model.id)).scalar() + 1
    return f"{prefix}-{year}-{count:04d}"

def fmt_usd(val):
    try:
        return f"${float(val):,.0f}"
    except Exception:
        return "$0"

def is_billable(lead):
    """Closed-won accounts that are not in an unsubscribed hold."""
    if not lead or lead.status != "Closed Won":
        return False
    return (getattr(lead, "billing_status", None) or "active") == "active"


def log_sub_activity(lead, action, channel, notes, happened_at=None):
    when = happened_at
    if isinstance(when, str) and when:
        try:
            when = datetime.strptime(when[:10], "%Y-%m-%d").date()
        except ValueError:
            when = date.today()
    elif not when:
        when = date.today()
    db.session.add(SubscriptionActivity(
        lead_id=lead.id,
        action=action,
        channel=channel if channel in ("sales", "company") else "sales",
        notes=(notes or "").strip(),
        happened_at=when,
        user_id=session.get("user_id"),
    ))


def can_manage_subscriber(lead):
    if session.get("user_role") == "admin":
        return True
    return lead and lead.assignee_id == session.get("user_id")

app.jinja_env.globals.update(
    fmt_usd=fmt_usd,
    current_user=current_user,
    PIPELINE_STAGES=PIPELINE_STAGES,
    STAGE_COLORS=STAGE_COLORS,
    datetime=datetime,
    date=date,
    is_billable=is_billable,
)

# ── Auth ─────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(email=request.form["email"]).first()
        if u and check_password_hash(u.password, request.form["password"]) and u.active:
            session["user_id"] = u.id
            session["user_name"] = u.name
            session["user_role"] = u.role
            return redirect(url_for("dashboard"))
        flash("Invalid credentials.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    is_admin = session.get("user_role") == "admin"
    current_uid = session.get("user_id")

    # Admin can filter by rep; non-admin always sees only their own leads
    filter_user = request.args.get("user_id", "") if is_admin else ""
    leads_q = Lead.query
    if is_admin and filter_user:
        leads_q = leads_q.filter_by(assignee_id=filter_user)
    elif not is_admin:
        leads_q = leads_q.filter_by(assignee_id=current_uid)
    leads = leads_q.order_by(Lead.created_at.desc()).all()

    won    = [l for l in leads if l.status == "Closed Won"]
    lost   = [l for l in leads if l.status == "Closed Lost"]
    active = [l for l in leads if l.status not in ("Closed Won","Closed Lost")]

    # MRR uses discounted price of billable (not unsubscribed) accounts
    mrr = sum(calc_discounted_price(l)[0] for l in won if is_billable(l))
    arr = mrr * 12
    pipeline_val = sum(calc_discounted_price(l)[0] for l in active)



    # All stages for kanban
    kanban = {s: [l for l in leads if l.status == s] for s in PIPELINE_STAGES}

    users    = User.query.filter_by(active=True).all()
    products = Product.query.filter_by(active=True).all()

    # Subscriptions — non-admin sees only their own won leads
    subs_q = Lead.query.filter_by(status="Closed Won")
    if not is_admin:
        subs_q = subs_q.filter_by(assignee_id=current_uid)
    subscribers = [s for s in subs_q.all() if is_billable(s)]
    sub_prices  = {s.id: calc_discounted_price(s)[0] for s in subscribers}

    # Billing — non-admin sees invoices for their own subscribers only
    if is_admin:
        invoices_all = Invoice.query.order_by(Invoice.due_date.desc()).limit(20).all()
        paid_invoices = sum(i.amount for i in Invoice.query.all() if i.status == "Paid")
        sent_invoices = sum(i.amount for i in Invoice.query.all() if i.status == "Sent")
    else:
        my_lead_ids = [l.id for l in subscribers]
        my_invs = Invoice.query.filter(Invoice.lead_id.in_(my_lead_ids)).order_by(Invoice.due_date.desc()).limit(20).all() if my_lead_ids else []
        invoices_all  = my_invs
        paid_invoices = sum(i.amount for i in my_invs if i.status == "Paid")
        sent_invoices = sum(i.amount for i in my_invs if i.status == "Sent")

    # Swimlane: all leads regardless of filter — grouped by assignee × stage
    # Only shown to admin
    all_leads = Lead.query.order_by(Lead.created_at.desc()).all()
    sales_reps = User.query.filter_by(active=True, role="sales").all()
    # Include admin users who also have leads
    assignee_ids = {l.assignee_id for l in all_leads if l.assignee_id}
    swimlane_users = User.query.filter(User.id.in_(assignee_ids)).order_by(User.name).all()
    # swimlane[user_id][stage] = [leads]
    swimlane = {}
    for u in swimlane_users:
        swimlane[u.id] = {s: [] for s in PIPELINE_STAGES}
    # Unassigned row
    swimlane[0] = {s: [] for s in PIPELINE_STAGES}
    for l in all_leads:
        uid = l.assignee_id or 0
        if uid not in swimlane:
            swimlane[uid] = {s: [] for s in PIPELINE_STAGES}
        swimlane[uid][l.status].append(l)
    # Remove unassigned if empty
    if not any(swimlane[0].values()):
        del swimlane[0]

    # Per-rep stats for swimlane header
    rep_stats = {}
    for u in swimlane_users:
        rep_leads = [l for l in all_leads if l.assignee_id == u.id]
        rep_won   = [l for l in rep_leads if l.status == "Closed Won"]
        rep_stats[u.id] = {
            "total":    len(rep_leads),
            "pipeline": len([l for l in rep_leads if l.status not in ("Closed Won","Closed Lost")]),
            "won":      len(rep_won),
            "mrr":      sum(calc_discounted_price(l)[0] for l in rep_won),
        }

    # Pre-compute aggregates for template (avoids Jinja sum/selectattr bugs)
    stage_counts = {s: sum(1 for l in leads if l.status == s) for s in PIPELINE_STAGES}
    prod_mrr = {}
    for p in products:
        prod_mrr[p.id] = sum(
            calc_discounted_price(l)[0]
            for l in leads if l.status == "Closed Won" and l.product_id == p.id
        )
    # Swimlane column totals: total leads per stage across all reps
    swimlane_col_totals = {s: sum(len(swimlane.get(uid, {}).get(s, [])) for uid in swimlane) for s in PIPELINE_STAGES}

    return render_template("dashboard.html",
        leads=leads, won=won, lost=lost, active=active,
        mrr=mrr, arr=arr, pipeline_val=pipeline_val,
        paid_invoices=paid_invoices, sent_invoices=sent_invoices,
        kanban=kanban, users=users, products=products,
        filter_user=filter_user,
        subscribers=subscribers, sub_prices=sub_prices,
        invoices_all=invoices_all,
        PIPELINE_STAGES=PIPELINE_STAGES,
        is_admin=is_admin,
        swimlane=swimlane, swimlane_users=swimlane_users,
        rep_stats=rep_stats,
        stage_counts=stage_counts, prod_mrr=prod_mrr,
        swimlane_col_totals=swimlane_col_totals,
    )

# ── Leads ────────────────────────────────────────────────────────────────────

@app.route("/leads")
@login_required
def leads():
    is_admin = session.get("user_role") == "admin"
    q = Lead.query
    # Non-admin: always scoped to their own leads, no filters shown
    if not is_admin:
        q = q.filter_by(assignee_id=session.get("user_id"))
    else:
        if f := request.args.get("product"):
            q = q.filter_by(product_id=f)
        if s := request.args.get("status"):
            q = q.filter_by(status=s)
        if a := request.args.get("assignee"):
            q = q.filter_by(assignee_id=a)
    leads_list = q.order_by(Lead.created_at.desc()).all()
    products = Product.query.filter_by(active=True).all()
    users    = User.query.filter_by(active=True).all()
    return render_template("leads.html", leads=leads_list,
                           products=products, users=users, is_admin=is_admin)

@app.route("/leads/new", methods=["GET","POST"])
@login_required
def lead_new():
    if request.method == "POST":
        f = request.form
        lead = Lead(
            company      = f["company"],
            contact      = f.get("contact",""),
            email        = f.get("email",""),
            phone        = f.get("phone",""),
            product_id   = f.get("product_id") or None,
            plan_id      = f.get("plan_id") or None,
            status       = f.get("status","New"),
            assignee_id  = f.get("assignee_id") or None,
            referral_id  = f.get("referral_id") or None,
            referral_pct    = float(f.get("referral_pct") or 0),
            referral_months = int(f.get("referral_months") or 6),
            discount_type   = f.get("discount_type","none"),
            discount_value  = float(f.get("discount_value") or 0),
            discount_reason = f.get("discount_reason",""),
            setup_fee       = float(f.get("setup_fee") or 0),
            notes           = f.get("notes",""),
            value           = float(f.get("value",0)),
        )
        db.session.add(lead)
        db.session.commit()
        flash(f"Lead for {lead.company} created.", "success")
        return redirect(url_for("leads"))
    products = Product.query.filter_by(active=True).all()
    users    = User.query.filter_by(active=True).all()
    plans    = Plan.query.filter_by(active=True).all()
    return render_template("lead_form.html", lead=None,
                           products=products, users=users, plans=plans)

@app.route("/leads/<int:lid>/edit", methods=["GET","POST"])
@login_required
def lead_edit(lid):
    lead = Lead.query.get_or_404(lid)
    if request.method == "POST":
        f = request.form
        lead.company      = f["company"]
        lead.contact      = f.get("contact","")
        lead.email        = f.get("email","")
        lead.phone        = f.get("phone","")
        lead.product_id   = f.get("product_id") or None
        lead.plan_id      = f.get("plan_id") or None
        lead.status       = f.get("status","New")
        lead.assignee_id  = f.get("assignee_id") or None
        lead.referral_id  = f.get("referral_id") or None
        lead.referral_pct    = float(f.get("referral_pct") or 0)
        lead.referral_months = int(f.get("referral_months") or 6)
        lead.discount_type   = f.get("discount_type","none")
        lead.discount_value  = float(f.get("discount_value") or 0)
        lead.discount_reason = f.get("discount_reason","")
        lead.setup_fee       = float(f.get("setup_fee") or 0)
        lead.notes           = f.get("notes","")
        lead.value           = float(f.get("value",0))
        lead.updated_at   = datetime.utcnow()
        db.session.commit()
        flash("Lead updated.", "success")
        return redirect(url_for("leads"))
    products = Product.query.filter_by(active=True).all()
    users    = User.query.filter_by(active=True).all()
    plans    = Plan.query.filter_by(active=True).all()
    return render_template("lead_form.html", lead=lead,
                           products=products, users=users, plans=plans)

@app.route("/api/plans-for-product/<int:pid>")
@login_required
def api_plans_for_product(pid):
    plans = Plan.query.filter_by(product_id=pid, active=True).all()
    return jsonify([{"id": p.id, "name": p.name, "price": p.price, "cadence": p.cadence} for p in plans])

# ── Agreements ───────────────────────────────────────────────────────────────

@app.route("/agreements")
@login_required
def agreements():
    agrs = Agreement.query.order_by(Agreement.sent_at.desc()).all()
    eligible_leads = Lead.query.filter(
        Lead.status.in_(["Proposal Sent","Demo Scheduled","Contacted","New"])
    ).all()
    return render_template("agreements.html", agreements=agrs,
                           eligible_leads=eligible_leads)

@app.route("/agreements/send", methods=["POST"])
@login_required
def agreement_send():
    f = request.form
    lead = Lead.query.get_or_404(f["lead_id"])
    final_monthly, final_setup, discount_label = calc_discounted_price(lead)
    agr = Agreement(
        ref             = next_ref(Agreement, "AGR"),
        lead_id         = lead.id,
        signer_name     = f.get("signer_name",""),
        signer_title    = f.get("signer_title",""),
        notes           = f.get("notes",""),
        discount_type   = lead.discount_type,
        discount_value  = lead.discount_value,
        discount_reason = lead.discount_reason,
        setup_fee       = final_setup,
        final_monthly   = final_monthly,
    )
    snapshot_agreement_terms(agr, lead)
    lead.status = "Agreement Sent"
    lead.updated_at = datetime.utcnow()
    db.session.add(agr)
    db.session.commit()
    # Generate PDF and send email
    pdf_bytes = generate_agreement_pdf(agr, lead)
    ok, err = send_email(
        to_addr         = lead.email,
        to_name         = agr.signer_name or lead.contact,
        subject         = f"[Karyva.ai] Subscription Agreement {agr.ref} — {lead.company}",
        html_body       = agreement_email_html(agr, lead),
        attachment_bytes= pdf_bytes,
        attachment_name = f"{agr.ref}-Agreement.pdf",
    )
    if ok:
        flash(f"Agreement {agr.ref} sent to {lead.email} ✓", "success")
    else:
        flash(f"Agreement saved but email failed: {err}", "warning")
    return redirect(url_for("agreements"))

@app.route("/agreements/<int:aid>/sign", methods=["POST"])
@login_required
def agreement_sign(aid):
    agr = Agreement.query.get_or_404(aid)
    agr.status    = "Signed"
    agr.signed_at = datetime.utcnow()
    # promote lead
    agr.lead.status = "Closed Won"
    agr.lead.updated_at = datetime.utcnow()
    # auto-create commissions
    _create_commissions(agr.lead)
    db.session.commit()
    flash(f"Agreement {agr.ref} marked Signed — lead moved to Closed Won.", "success")
    return redirect(url_for("agreements"))

@app.route("/agreements/<int:aid>/decline", methods=["POST"])
@login_required
def agreement_decline(aid):
    agr = Agreement.query.get_or_404(aid)
    agr.status = "Declined"
    db.session.commit()
    flash(f"Agreement {agr.ref} marked Declined.", "warning")
    return redirect(url_for("agreements"))

@app.route("/agreements/<int:aid>/resend", methods=["POST"])
@login_required
def agreement_resend(aid):
    agr = Agreement.query.get_or_404(aid)
    lead = agr.lead
    if not lead:
        flash("No lead linked to this agreement.", "danger")
        return redirect(url_for("agreements"))

    # Allow optional override of signer name/title/notes from form
    if request.form.get("signer_name"):
        agr.signer_name  = request.form["signer_name"]
    if request.form.get("signer_title"):
        agr.signer_title = request.form["signer_title"]
    if request.form.get("notes"):
        agr.notes = request.form["notes"]

    # Re-snapshot current discount from lead
    final_monthly, final_setup, _ = calc_discounted_price(agr.lead)
    agr.discount_type   = agr.lead.discount_type
    agr.discount_value  = agr.lead.discount_value
    agr.discount_reason = agr.lead.discount_reason
    agr.setup_fee       = final_setup
    agr.final_monthly   = final_monthly
    snapshot_agreement_terms(agr, lead)
    # Reset status back to Pending and update sent timestamp
    agr.status    = "Pending Signature"
    agr.signed_at = None
    agr.sent_at   = datetime.utcnow()

    # Also reset lead status if it was Declined/Closed Lost
    if lead.status in ("Closed Lost",):
        lead.status     = "Agreement Sent"
        lead.updated_at = datetime.utcnow()

    db.session.commit()

    # Regenerate PDF and resend
    pdf_bytes = generate_agreement_pdf(agr, lead)
    ok, err = send_email(
        to_addr         = lead.email,
        to_name         = agr.signer_name or lead.contact,
        subject         = f"[Karyva.ai] Updated Agreement {agr.ref} — {lead.company}",
        html_body       = agreement_email_html(agr, lead),
        attachment_bytes= pdf_bytes,
        attachment_name = f"{agr.ref}-Agreement.pdf",
    )
    if ok:
        flash(f"Agreement {agr.ref} resent to {lead.email} ✓", "success")
    else:
        flash(f"Agreement updated but email failed: {err}", "warning")
    return redirect(url_for("agreements"))

# ── Invoices ─────────────────────────────────────────────────────────────────

@app.route("/invoices")
@login_required
def invoices():
    invs = Invoice.query.order_by(Invoice.due_date.desc()).all()
    subscribers = [s for s in Lead.query.filter_by(status="Closed Won").all() if is_billable(s)]
    sub_prices = {}
    for s in subscribers:
        final, setup, label = calc_discounted_price(s)
        sub_prices[s.id] = {"final": final, "setup": setup, "label": label}
    return render_template("invoices.html", invoices=invs, subscribers=subscribers, sub_prices=sub_prices)

@app.route("/invoices/new", methods=["POST"])
@login_required
def invoice_new():
    f = request.form
    lead   = Lead.query.get(f.get("lead_id"))
    if lead and not is_billable(lead):
        flash("No invoices while this subscriber is unsubscribed. Record a renewal first.", "warning")
        return redirect(url_for("invoices"))
    amount = float(f.get("amount") or 0)
    include_setup = f.get("include_setup_fee") == "1"

    # If amount is 0 and we have a lead, calculate from lead
    if amount == 0 and lead:
        final, setup, _ = calc_discounted_price(lead)
        amount = final + (setup if include_setup else 0)

    cadence = f.get("cadence", "monthly")
    if not cadence and lead and lead.plan:
        cadence = lead.plan.cadence

    notes = f.get("notes","")
    if include_setup and lead and lead.setup_fee:
        setup_line = f"Includes one-time setup fee: ${lead.setup_fee:,.2f}"
        notes = (notes + " | " + setup_line).strip() if notes else setup_line

    inv = Invoice(
        ref     = next_ref(Invoice, "INV"),
        lead_id = lead.id if lead else None,
        amount  = amount,
        cadence = cadence or "monthly",
        due_date= datetime.strptime(f["due_date"], "%Y-%m-%d").date(),
        notes   = notes,
    )
    db.session.add(inv)
    db.session.commit()

    send_via = request.form.get("send_via", "square")

    if send_via == "square" and lead:
        pub_url, sq_id, err = square_send_invoice(inv)
        if err:
            flash(f"Invoice saved but Square send failed: {err}", "warning")
        else:
            inv.square_invoice_id = sq_id
            inv.square_public_url = pub_url
            db.session.commit()
            flash(f"Invoice {inv.ref} sent via Square ✓ — customer will receive a payment link.", "success")
    elif lead and lead.email:
        ok, err = send_email(
            to_addr  = lead.email,
            to_name  = lead.contact,
            subject  = f"[Karyva.ai] Invoice {inv.ref} — {lead.company}",
            html_body= invoice_email_html(inv, lead),
        )
        if ok:
            flash(f"Invoice {inv.ref} emailed to {lead.email} ✓", "success")
        else:
            flash(f"Invoice saved but email failed: {err}", "warning")
    else:
        flash(f"Invoice {inv.ref} created (no email on file).", "success")
    return redirect(url_for("invoices"))

@app.route("/invoices/<int:iid>/mark-paid", methods=["POST"])
@login_required
def invoice_mark_paid(iid):
    inv = Invoice.query.get_or_404(iid)
    inv.status  = "Paid"
    inv.paid_at = datetime.utcnow()
    db.session.commit()
    flash(f"Invoice {inv.ref} marked as Paid.", "success")
    return redirect(url_for("invoices"))

@app.route("/invoices/<int:iid>/void", methods=["POST"])
@login_required
def invoice_void(iid):
    inv = Invoice.query.get_or_404(iid)
    inv.status = "Void"
    db.session.commit()
    flash(f"Invoice {inv.ref} voided.", "warning")
    return redirect(url_for("invoices"))

@app.route("/invoices/generate-bulk", methods=["POST"])
@login_required
def invoice_generate_bulk():
    """Generate monthly invoices for billable subscribers without one this month."""
    period_start = date.today().replace(day=1)
    subs = Lead.query.filter_by(status="Closed Won").all()
    created = 0
    skipped = 0
    for lead in subs:
        if not is_billable(lead):
            skipped += 1
            continue
        existing = Invoice.query.filter(
            Invoice.lead_id == lead.id,
            Invoice.sent_at >= datetime(period_start.year, period_start.month, 1)
        ).first()
        if not existing:
            inv = Invoice(
                ref      = next_ref(Invoice, "INV"),
                lead_id  = lead.id,
                amount   = calc_discounted_price(lead)[0],
                cadence  = lead.plan.cadence if lead.plan else "monthly",
                due_date = period_start + timedelta(days=14),
            )
            db.session.add(inv)
            created += 1
    db.session.commit()
    # Email each newly generated invoice
    emailed = 0
    for inv in Invoice.query.filter(Invoice.sent_at >= datetime(period_start.year, period_start.month, 1)).all():
        lead = inv.lead
        if lead and lead.email:
            ok, _ = send_email(
                to_addr  = lead.email,
                to_name  = lead.contact,
                subject  = f"[Karyva.ai] Invoice {inv.ref} — {lead.company}",
                html_body= invoice_email_html(inv, lead),
            )
            if ok:
                emailed += 1
    flash(f"Generated {created} invoice(s), emailed {emailed}. Skipped {skipped} unsubscribed.", "success")
    return redirect(url_for("invoices"))

# ── Commissions ───────────────────────────────────────────────────────────────

@app.route("/commissions")
@login_required
def commissions():
    is_admin = session.get("user_role") == "admin"
    period = request.args.get("period", datetime.utcnow().strftime("%Y-%m"))
    q = Commission.query.filter_by(period=period)
    if not is_admin:
        q = q.filter_by(user_id=session.get("user_id"))
    comms = q.order_by(Commission.created_at.desc()).all()
    users  = User.query.filter_by(active=True).all()
    # summary per user
    summary = {}
    for c in comms:
        uid = c.user_id
        if uid not in summary:
            summary[uid] = {"user": c.user, "sales": 0, "referral": 0, "total": 0, "status": c.status}
        summary[uid][c.kind] += c.amount
        summary[uid]["total"] += c.amount
    return render_template("commissions.html", commissions=comms,
                           summary=summary.values(), period=period)

@app.route("/commissions/<int:cid>/mark-paid", methods=["POST"])
@login_required
@admin_required
def commission_mark_paid(cid):
    c = Commission.query.get_or_404(cid)
    c.status = "Paid"
    db.session.commit()
    flash("Commission marked as Paid.", "success")
    return redirect(url_for("commissions"))

def _create_commissions(lead):
    """
    Create sales + referral commission records for a newly won lead.
    Referral commission: % of first month fee, paid out after N consecutive paid months.
    Status is set to Pending until manually marked Paid (admin confirms retention).
    """
    period = datetime.utcnow().strftime("%Y-%m")
    final_monthly, _, _ = calc_discounted_price(lead)

    # Sales commission — % of discounted monthly fee
    if lead.assignee_id:
        rate   = SALES_COMMISSION_RATE
        amount = round(final_monthly * rate / 100, 2)
        existing = Commission.query.filter_by(lead_id=lead.id, user_id=lead.assignee_id, kind="sales").first()
        if not existing:
            db.session.add(Commission(user_id=lead.assignee_id, lead_id=lead.id,
                                      kind="sales", rate=rate, amount=amount, period=period))

    # Referral commission — % of first month fee, held Pending until retention confirmed
    if lead.referral_id and lead.referral_pct > 0:
        months  = int(lead.referral_months or 6)
        amount  = round(final_monthly * lead.referral_pct / 100, 2)
        existing = Commission.query.filter_by(lead_id=lead.id, user_id=lead.referral_id, kind="referral").first()
        if not existing:
            c = Commission(
                user_id  = lead.referral_id,
                lead_id  = lead.id,
                kind     = "referral",
                rate     = lead.referral_pct,
                amount   = amount,
                period   = period,
                status   = "Pending",  # held until retention confirmed
            )
            c.notes = f"Pays out after {months} consecutive paid months"
            db.session.add(c)

# ── Subscribers ───────────────────────────────────────────────────────────────

@app.route("/subscribers")
@login_required
def subscribers():
    is_admin = session.get("user_role") == "admin"
    q = Lead.query.filter_by(status="Closed Won")
    if not is_admin:
        q = q.filter_by(assignee_id=session.get("user_id"))
    all_subs = q.order_by(Lead.updated_at.desc()).all()
    show = request.args.get("show", "all")
    if show == "active":
        subs = [s for s in all_subs if is_billable(s)]
    elif show == "ended":
        subs = [s for s in all_subs if not is_billable(s)]
    else:
        subs = all_subs
        show = "all"
    sub_prices = {}
    for s in all_subs:
        final, setup, label = calc_discounted_price(s)
        sub_prices[s.id] = {"final": final, "setup": setup, "label": label}
    mrr = sum(sub_prices[s.id]["final"] for s in all_subs if is_billable(s))
    counts = {
        "all": len(all_subs),
        "active": sum(1 for s in all_subs if is_billable(s)),
        "ended": sum(1 for s in all_subs if not is_billable(s)),
    }
    return render_template("subscribers.html", subscribers=subs, mrr=mrr,
                           sub_prices=sub_prices, show=show, counts=counts)


@app.route("/subscribers/<int:lid>")
@login_required
def subscriber_detail(lid):
    lead = Lead.query.get_or_404(lid)
    if lead.status != "Closed Won":
        flash("That account is not a subscriber.", "warning")
        return redirect(url_for("subscribers"))
    if not can_manage_subscriber(lead):
        flash("You can only view your own subscribers.", "danger")
        return redirect(url_for("subscribers"))
    final, setup, label = calc_discounted_price(lead)
    activities = lead.sub_activities.order_by(SubscriptionActivity.created_at.desc()).all()
    return render_template("subscriber_detail.html", s=lead, activities=activities,
                           price={"final": final, "setup": setup, "label": label},
                           billable=is_billable(lead))


@app.route("/subscribers/<int:lid>/end", methods=["POST"])
@login_required
def subscriber_end(lid):
    lead = Lead.query.get_or_404(lid)
    if not can_manage_subscriber(lead):
        flash("You can only update your own subscribers.", "danger")
        return redirect(url_for("subscribers"))
    if not is_billable(lead) and (lead.billing_status or "active") == "unsubscribed":
        flash(f"{lead.company} is already unsubscribed.", "info")
        return redirect(url_for("subscriber_detail", lid=lid))
    lead.billing_status = "unsubscribed"
    lead.unsubscribed_at = datetime.utcnow()
    lead.updated_at = datetime.utcnow()
    log_sub_activity(lead, "ended", request.form.get("channel", "sales"),
                     request.form.get("notes", ""), request.form.get("happened_at"))
    db.session.commit()
    flash(f"{lead.company} marked unsubscribed. Automatic invoices are paused.", "warning")
    return redirect(url_for("subscriber_detail", lid=lid))


@app.route("/subscribers/<int:lid>/renew", methods=["POST"])
@login_required
def subscriber_renew(lid):
    lead = Lead.query.get_or_404(lid)
    if not can_manage_subscriber(lead):
        flash("You can only update your own subscribers.", "danger")
        return redirect(url_for("subscribers"))
    lead.billing_status = "active"
    lead.resubscribed_at = datetime.utcnow()
    lead.status = "Closed Won"
    lead.updated_at = datetime.utcnow()
    log_sub_activity(lead, "renewed", request.form.get("channel", "sales"),
                     request.form.get("notes", ""), request.form.get("happened_at"))
    db.session.commit()
    flash(f"{lead.company} renewed. Invoices will generate again.", "success")
    return redirect(url_for("subscriber_detail", lid=lid))


@app.route("/subscribers/<int:lid>/note", methods=["POST"])
@login_required
def subscriber_note(lid):
    lead = Lead.query.get_or_404(lid)
    if not can_manage_subscriber(lead):
        flash("You can only update your own subscribers.", "danger")
        return redirect(url_for("subscribers"))
    notes = (request.form.get("notes") or "").strip()
    if not notes:
        flash("Enter a note.", "warning")
        return redirect(url_for("subscriber_detail", lid=lid))
    log_sub_activity(lead, "note", request.form.get("channel", "sales"),
                     notes, request.form.get("happened_at"))
    db.session.commit()
    flash("Activity recorded.", "success")
    return redirect(url_for("subscriber_detail", lid=lid))


# ── Products & Plans ──────────────────────────────────────────────────────────

@app.route("/products")
@login_required
@admin_required
def products():
    prods = Product.query.order_by(Product.name).all()
    return render_template("products.html", products=prods)

@app.route("/products/new", methods=["GET","POST"])
@login_required
@admin_required
def product_new():
    if request.method == "POST":
        f = request.form
        p = Product(name=f["name"], slug=f["slug"], description=f.get("description",""),
                    icon=f.get("icon","📦"), color=f.get("color","#2E7D5E"))
        db.session.add(p)
        db.session.commit()
        flash(f"Product {p.name} created.", "success")
        return redirect(url_for("products"))
    return render_template("product_form.html", product=None)

@app.route("/products/<int:pid>/edit", methods=["GET","POST"])
@login_required
@admin_required
def product_edit(pid):
    p = Product.query.get_or_404(pid)
    if request.method == "POST":
        f = request.form
        p.name = f["name"]; p.slug = f["slug"]
        p.description = f.get("description","")
        p.icon = f.get("icon","📦"); p.color = f.get("color","#2E7D5E")
        db.session.commit()
        flash("Product updated.", "success")
        return redirect(url_for("products"))
    return render_template("product_form.html", product=p)

@app.route("/products/<int:pid>/plan/new", methods=["GET","POST"])
@login_required
@admin_required
def plan_new(pid):
    product = Product.query.get_or_404(pid)
    if request.method == "POST":
        f = request.form
        pl = Plan(product_id=pid, name=f["name"], price=float(f["price"]),
                  cadence=f.get("cadence","monthly"), features=f.get("features",""))
        db.session.add(pl)
        db.session.commit()
        flash(f"Plan {pl.name} added to {product.name}.", "success")
        return redirect(url_for("products"))
    return render_template("plan_form.html", product=product, plan=None)

@app.route("/plans/<int:plid>/edit", methods=["GET","POST"])
@login_required
@admin_required
def plan_edit(plid):
    pl = Plan.query.get_or_404(plid)
    if request.method == "POST":
        f = request.form
        pl.name = f["name"]; pl.price = float(f["price"])
        pl.cadence = f.get("cadence","monthly"); pl.features = f.get("features","")
        db.session.commit()
        flash("Plan updated.", "success")
        return redirect(url_for("products"))
    return render_template("plan_form.html", product=pl.product, plan=pl)

@app.route("/plans/<int:plid>/delete", methods=["POST"])
@login_required
@admin_required
def plan_delete(plid):
    pl = Plan.query.get_or_404(plid)
    db.session.delete(pl)
    db.session.commit()
    flash("Plan removed.", "warning")
    return redirect(url_for("products"))

# ── Square Webhook ───────────────────────────────────────────────────────────

@app.route("/webhooks/square", methods=["POST"])
def square_webhook():
    """
    Receive Square webhook events.
    invoice.payment_made → mark invoice Paid in Revfi.
    Configure this URL in Square Developer Dashboard → Webhooks.
    """
    import hmac, hashlib
    payload = request.get_data()
    sig     = request.headers.get("x-square-hmacsha256-signature", "")

    # Verify signature if webhook_secret is configured
    cfg = get_square_settings()
    if cfg.webhook_secret:
        expected = hmac.new(cfg.webhook_secret.encode(), payload, hashlib.sha256).digest()
        import base64
        expected_b64 = base64.b64encode(expected).decode()
        if not hmac.compare_digest(sig, expected_b64):
            return jsonify({"error": "invalid signature"}), 403

    event = request.get_json(silent=True) or {}
    event_type = event.get("type", "")

    if event_type == "invoice.payment_made":
        sq_invoice = event.get("data", {}).get("object", {}).get("invoice", {})
        sq_id      = sq_invoice.get("id")
        if sq_id:
            inv = Invoice.query.filter_by(square_invoice_id=sq_id).first()
            if inv and inv.status != "Paid":
                inv.status  = "Paid"
                inv.paid_at = datetime.utcnow()
                db.session.commit()

    return jsonify({"ok": True}), 200


@app.route("/invoices/<int:iid>/send-square", methods=["POST"])
@login_required
def invoice_send_square(iid):
    """Send an existing invoice via Square."""
    inv = Invoice.query.get_or_404(iid)
    pub_url, sq_id, err = square_send_invoice(inv)
    if err:
        flash(f"Square send failed: {err}", "danger")
    else:
        inv.square_invoice_id = sq_id
        inv.square_public_url = pub_url
        db.session.commit()
        flash(f"Invoice {inv.ref} sent via Square ✓", "success")
    return redirect(url_for("invoices"))

# ── Email Reply Webhook (IMAP polling) ───────────────────────────────────────

import imaplib, email as email_lib, threading, time as time_mod

# Confirmation — any of these anywhere in the reply body triggers auto-sign
CONFIRM_KEYWORDS = {
    "agreed", "agree", "i agree", "i agree to", "i agree to the terms",
    "confirmed", "confirm", "i confirm", "i accept", "accepted",
    "approve", "approved", "yes i agree", "happy to proceed",
    "please proceed", "proceed", "sounds good", "looks good",
    "signed", "i sign", "accept the terms", "accept terms",
}

# Decline — any of these triggers auto-decline
DECLINE_KEYWORDS = {
    "declined", "i decline", "do not agree",
    "don't agree", "i reject", "i reject this", "not interested",
    "no thanks", "not proceeding", "do not proceed",
}

def check_email_replies():
    """
    Poll inbox every 5 minutes for replies to agreement emails.
    If subject contains an AGR ref and body contains a confirm keyword → mark signed.
    Runs in a background thread.
    """
    while True:
        try:
            _poll_inbox()
        except Exception as e:
            print(f"[Email poll error] {e}")
        time_mod.sleep(300)   # poll every 5 minutes

def _decode_header(raw):
    """Safely decode email header (handles encoded-words like =?UTF-8?...)."""
    from email.header import decode_header as _dh
    parts = []
    for b, enc in _dh(raw or ""):
        if isinstance(b, bytes):
            parts.append(b.decode(enc or "utf-8", errors="ignore"))
        else:
            parts.append(b)
    return " ".join(parts)


def _poll_inbox():
    """
    Poll inbox for agreement confirmation replies.
    Only looks at emails whose subject contains "Subscription Agreement AGR".
    Marks agreement signed if body contains a confirmation keyword.
    """
    import re
    with app.app_context():
        cfg = get_email_settings()
        if not cfg.smtp_user or not cfg.smtp_pass:
            return 0, "Email not configured."

        raw_host = cfg.smtp_host.lower()
        if "office365" in raw_host or "outlook" in raw_host:
            imap_host = "outlook.office365.com"
        elif raw_host.startswith("smtp."):
            imap_host = raw_host.replace("smtp.", "imap.", 1)
        else:
            imap_host = raw_host

        try:
            mail = imaplib.IMAP4_SSL(imap_host, timeout=20)
            mail.login(cfg.smtp_user, cfg.smtp_pass)
        except Exception as e:
            return 0, f"IMAP login failed: {e}"

        mail.select("INBOX")

        # Search only emails whose subject contains our agreement phrase
        _, data = mail.search(None, '(SUBJECT "Agreement AGR")')
        nums = data[0].split()

        signed_count = 0
        errors = []
        for num in nums:
            try:
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email_lib.message_from_bytes(msg_data[0][1])

                subject   = _decode_header(msg.get("Subject", ""))
                body      = _get_body(msg)
                body_low  = body.lower()
                # AGR ref can be in subject OR body; keywords must be in BODY only
                # (subject always contains "Agreement" which would false-positive on "agree")
                refs_text = subject + " " + body

                # Determine intent from body only
                is_decline = any(kw in body_low for kw in DECLINE_KEYWORDS)
                is_confirm = any(kw in body_low for kw in CONFIRM_KEYWORDS)

                if not is_decline and not is_confirm:
                    continue

                # Extract AGR ref from subject/body
                refs = re.findall(r"AGR-[0-9]{4}-[0-9]{4}", refs_text)
                for ref in refs:
                    agr = Agreement.query.filter_by(ref=ref).first()
                    if not agr or agr.status == "Signed":
                        continue  # Already fully signed — nothing to do

                    if is_decline and agr.status != "Signed":
                        agr.status = "Declined"
                        db.session.commit()
                        # Note: lead stays as-is for manual follow-up
                    elif is_confirm:  # Confirm overrides a prior Declined
                        agr.status    = "Signed"
                        agr.signed_at = datetime.utcnow()
                        if agr.lead:
                            agr.lead.status     = "Closed Won"
                            agr.lead.updated_at = datetime.utcnow()
                            _create_commissions(agr.lead)
                        db.session.commit()
                        signed_count += 1

                mail.store(num, "+FLAGS", "(\\Seen)")

            except Exception as e:
                errors.append(str(e))

        mail.logout()
        return signed_count, ("; ".join(errors) if errors else None)

def _strip_quoted(text):
    """
    Remove quoted/replied content from an email body, keeping only the
    fresh reply written by the sender.

    Strips:
    - Lines starting with > (standard quote marker)
    - "On ... wrote:" blocks (Gmail/Outlook reply headers)
    - "From:", "Sent:", "To:", "Subject:" forwarded header blocks
    - Anything after common dividers like "-----Original Message-----"
    - Our own instruction block ("Simply reply to this email with")
    """
    import re as _re

    lines = text.splitlines()
    clean = []
    skip = False

    for line in lines:
        stripped = line.strip()

        # Hard stop markers — everything after these is the original message
        if _re.match(
            r'^(-{3,}\s*(Original Message|Forwarded Message|Reply)\s*-{3,})',
            stripped, _re.IGNORECASE
        ):
            break
        if _re.match(r'^_{5,}', stripped):   # Outlook underline divider
            break

        # "On Mon, 12 Sep 2026, Shan wrote:" — Gmail reply header (single or multi-line)
        if _re.match(r'^On .{5,}wrote:$', stripped, _re.IGNORECASE):
            break
        if _re.match(r'^On .{5,}$', stripped, _re.IGNORECASE) and stripped.endswith(">"):
            break

        # Forwarded header block
        if _re.match(r'^(From|Sent|To|Subject|Date)\s*:', stripped, _re.IGNORECASE):
            break

        # Our own instruction text from the agreement email
        if any(phrase in stripped.lower() for phrase in [
            "simply reply to this email",
            "to accept",
            "to decline",
            "reply with",
            "agreement reference",
            "automatically detected",
        ]):
            skip = True
        if skip:
            continue

        # Standard quoted lines
        if stripped.startswith(">"):
            continue

        clean.append(line)

    # Also trim trailing whitespace/signature separators
    result = "\n".join(clean)
    # Remove "-- " signature block (email signature)
    result = _re.split(r"\n--\s*\n", result)[0]
    return result.strip()


def _get_body(msg):
    """
    Extract ONLY the fresh reply text from an email message —
    strips all quoted/original content so keyword matching works
    on what the sender actually typed, not the thread history.
    """
    import html as _html
    import re as _re

    def _strip_html(h):
        # Remove style/script blocks first
        h = _re.sub(r'<(style|script)[^>]*>.*?</(style|script)>', ' ', h, flags=_re.DOTALL|_re.IGNORECASE)
        # Remove blockquote (quoted reply sections in HTML emails)
        h = _re.sub(r'<blockquote[^>]*>.*?</blockquote>', ' ', h, flags=_re.DOTALL|_re.IGNORECASE)
        # Strip remaining tags
        h = _re.sub(r"<[^>]+>", " ", h)
        return _html.unescape(h)

    def _decode_part(part):
        try:
            return part.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            try:
                return str(part.get_payload())
            except Exception:
                return ""

    plain_parts = []
    html_parts  = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                continue
            if ct == "text/plain":
                plain_parts.append(_decode_part(part))
            elif ct == "text/html":
                html_parts.append(_decode_part(part))
    else:
        ct = msg.get_content_type()
        if ct == "text/plain":
            plain_parts.append(_decode_part(msg))
        elif ct == "text/html":
            html_parts.append(_decode_part(msg))
        else:
            plain_parts.append(_decode_part(msg))

    if plain_parts:
        return _strip_quoted(" ".join(plain_parts))
    if html_parts:
        return _strip_quoted(_strip_html(" ".join(html_parts)))
    return ""

def start_reply_watcher():
    """Start background IMAP polling thread (only if email is configured)."""
    t = threading.Thread(target=check_email_replies, daemon=True)
    t.start()
    print("📬  Email reply watcher started.")

@app.route("/agreements/check-replies", methods=["POST"])
@login_required
@admin_required
def agreements_check_replies():
    """Manually trigger an IMAP poll for agreement reply emails."""
    try:
        count, err = _poll_inbox()
        if count:
            flash(f"{count} agreement(s) confirmed and marked Signed ✓", "success")
        elif err:
            flash(f"Inbox checked — {err}", "warning")
        else:
            flash("Inbox checked — no new confirmations found.", "info")
    except Exception as e:
        flash(f"Could not check inbox: {e}", "danger")
    return redirect(url_for("agreements"))

@app.route("/settings/agreement-template", methods=["GET", "POST"])
@login_required
@admin_required
def agreement_template():
    tmpl = get_agreement_template()
    if request.method == "POST":
        upload = request.files.get("docx")
        if "reset" in request.form:
            tmpl.body = default_agreement_terms()
            tmpl.title = "Subscription & Services Agreement"
            tmpl.company_name = "Karyva.ai"
            flash("Restored the Karyva.AI Subscription & Services Agreement.", "success")
        elif upload and upload.filename and upload.filename.lower().endswith(".docx"):
            try:
                tmpl.body = extract_docx_text(io.BytesIO(upload.read()))
                first = next((ln.strip() for ln in tmpl.body.splitlines() if ln.strip()), "")
                if first:
                    tmpl.title = first[:200]
                tmpl.company_name = (request.form.get("company_name") or tmpl.company_name or "Karyva.ai").strip()
                flash("Word document imported. Review the text, then Save if you edit further.", "success")
            except Exception as e:
                flash(f"Could not read that .docx: {e}", "danger")
                return redirect(url_for("agreement_template"))
        else:
            tmpl.title = (request.form.get("title") or "Subscription & Services Agreement").strip()
            tmpl.company_name = (request.form.get("company_name") or "Karyva.ai").strip() or "Karyva.ai"
            tmpl.body = request.form.get("body") or ""
            flash("Agreement template saved. New and resent agreements will use this wording.", "success")
        tmpl.updated_at = datetime.utcnow()
        db.session.commit()
        return redirect(url_for("agreement_template"))
    return render_template("agreement_template.html", tmpl=tmpl)

# ── Email Settings ───────────────────────────────────────────────────────────

@app.route("/settings/email", methods=["GET","POST"])
@login_required
@admin_required
def email_settings():
    cfg = get_email_settings()
    if request.method == "POST":
        f = request.form
        cfg.smtp_host  = f.get("smtp_host",  "smtp.gmail.com")
        cfg.smtp_port  = int(f.get("smtp_port", 587))
        cfg.smtp_user  = f.get("smtp_user",  "")
        cfg.smtp_pass  = f.get("smtp_pass",  "") or cfg.smtp_pass  # keep old if blank
        cfg.from_name  = f.get("from_name",  "Karyva.ai Team")
        cfg.from_email = f.get("from_email", "")
        cfg.use_tls    = "use_tls" in f
        cfg.updated_at = datetime.utcnow()
        db.session.commit()
        # Send a test email if requested
        if "test" in f:
            test_to = f.get("test_to", cfg.smtp_user)
            ok, err = send_email(test_to, "Karyva.ai SMTP Test",
                "<p>Your Karyva.ai email settings are working correctly ✓</p>")
            if ok:
                flash(f"Settings saved. Test email sent to {test_to} ✓", "success")
            else:
                flash(f"Settings saved but test email failed: {err}", "warning")
        else:
            flash("Email settings saved.", "success")
        return redirect(url_for("email_settings"))
    return render_template("email_settings.html", cfg=cfg)

# ── Square Settings ──────────────────────────────────────────────────────────

@app.route("/settings/square", methods=["GET","POST"])
@login_required
@admin_required
def square_settings():
    cfg = get_square_settings()
    if request.method == "POST":
        f = request.form
        cfg.environment    = f.get("environment", "sandbox")
        cfg.access_token   = f.get("access_token", "").strip() or cfg.access_token
        cfg.location_id    = f.get("location_id",  "").strip()
        cfg.webhook_secret = f.get("webhook_secret","").strip() or cfg.webhook_secret
        cfg.updated_at     = datetime.utcnow()
        db.session.commit()
        # Test connection
        if "test" in f:
            data, err = square_api("GET", f"/v2/locations/{cfg.location_id}")
            if err:
                flash(f"Settings saved but connection test failed: {err}", "warning")
            else:
                loc_name = data.get("location", {}).get("name", cfg.location_id)
                flash(f"Connected to Square ✓ — Location: {loc_name}", "success")
        else:
            flash("Square settings saved.", "success")
        return redirect(url_for("square_settings"))
    return render_template("square_settings.html", cfg=cfg)

# ── Users (Admin) ─────────────────────────────────────────────────────────────

@app.route("/users")
@login_required
@admin_required
def users():
    return render_template("users.html", users=User.query.order_by(User.name).all())

@app.route("/users/new", methods=["GET","POST"])
@login_required
@admin_required
def user_new():
    if request.method == "POST":
        f = request.form
        if User.query.filter_by(email=f["email"]).first():
            flash("Email already exists.", "danger")
        else:
            u = User(name=f["name"], email=f["email"],
                     password=generate_password_hash(f["password"]),
                     role=f.get("role","sales"))
            db.session.add(u)
            db.session.commit()
            flash(f"User {u.name} created.", "success")
            return redirect(url_for("users"))
    return render_template("user_form.html", user=None)

@app.route("/users/<int:uid>/edit", methods=["GET","POST"])
@login_required
@admin_required
def user_edit(uid):
    u = User.query.get_or_404(uid)
    if request.method == "POST":
        f = request.form
        u.name = f["name"]; u.email = f["email"]
        u.role = f.get("role","sales")
        u.active = "active" in f
        if f.get("password"):
            u.password = generate_password_hash(f["password"])
        db.session.commit()
        flash("User updated.", "success")
        return redirect(url_for("users"))
    return render_template("user_form.html", user=u)

# ── DB Init & Seed ────────────────────────────────────────────────────────────

def seed_db():
    if User.query.first():
        return  # already seeded
    # Admin
    admin = User(name="Shan Kumar", email="shan@revfi.ai",
                 password=generate_password_hash("Admin@123"), role="admin")
    db.session.add(admin)
    # Sales reps
    for name, email in [("Priya Nair","priya@revfi.ai"),("Ravi Sharma","ravi@revfi.ai"),
                         ("Meena Patel","meena@revfi.ai"),("Arjun Das","arjun@revfi.ai")]:
        db.session.add(User(name=name, email=email,
                            password=generate_password_hash("Sales@123"), role="sales"))
    # Products
    freshfi = Product(name="FreshFi", slug="freshfi", icon="🌿", color="#2E7D5E",
                      description="Fresh produce financing platform")
    rezfi   = Product(name="RezFi",   slug="rezfi",   icon="🏨", color="#1E3A8A",
                      description="Restaurant financing platform")
    db.session.add_all([freshfi, rezfi])
    db.session.flush()
    # Plans — FreshFi
    for name, price, features in [
        ("Starter",    299,  "Up to 50 transactions, Basic reporting, Email support"),
        ("Pro",        799,  "Unlimited transactions, Advanced analytics, API access, Priority support"),
        ("Enterprise", 1999, "Custom limits, Dedicated CSM, White-label, SLA guarantee"),
    ]:
        db.session.add(Plan(product_id=freshfi.id, name=name, price=price, features=features))
    # Plans — RezFi
    for name, price, features in [
        ("Starter",    249,  "Up to 3 locations, Basic POS sync, Standard reports"),
        ("Pro",        699,  "Up to 15 locations, Full integrations, Advanced reports"),
        ("Enterprise", 1799, "Unlimited locations, Custom SLA, Dedicated CSM, White-label"),
    ]:
        db.session.add(Plan(product_id=rezfi.id, name=name, price=price, features=features))
    db.session.commit()
    # Ensure settings rows exist
    if not EmailSettings.query.first():
        db.session.add(EmailSettings())
        db.session.commit()
    if not SquareSettings.query.first():
        db.session.add(SquareSettings())
        db.session.commit()
    get_agreement_template()
    print("✅  Database seeded.")



def safe_init_db():
    """
    Create tables if they don't exist.
    If the existing DB has a stale schema (missing columns), wipe and recreate.
    SQLAlchemy engine is disposed so the connection pool sees the new file.
    """
    import sqlite3
    db_path = os.path.join(DATA_DIR, "revfi.db")
    checks = {
        "users":       "SELECT id, name, email, password, role, active, created FROM users LIMIT 1",
        "products":    "SELECT id, name, slug FROM products LIMIT 1",
        "plans":       "SELECT id, product_id, name, price, cadence FROM plans LIMIT 1",
        "leads":       "SELECT id, company, assignee_id, referral_id, referral_pct, referral_months, discount_type, discount_value, setup_fee FROM leads LIMIT 1",
        "agreements":  "SELECT id, ref, lead_id, signer_name, signer_title, discount_type, final_monthly, setup_fee FROM agreements LIMIT 1",
        "invoices":    "SELECT id, ref, lead_id, amount, cadence, due_date FROM invoices LIMIT 1",
        "commissions": "SELECT id, user_id, lead_id, kind, rate, amount, period, notes FROM commissions LIMIT 1",
        "email_settings":  "SELECT id, smtp_host, smtp_port, smtp_user, from_name, from_email FROM email_settings LIMIT 1",
        "square_settings": "SELECT id, access_token, location_id, environment FROM square_settings LIMIT 1",
        "inv_square_cols": "SELECT square_invoice_id, square_public_url FROM invoices LIMIT 1",
    }
    stale = False
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            for table, sql in checks.items():
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as e:
                    print(f"⚠️  Schema mismatch on '{table}': {e}")
                    stale = True
                    break
            conn.close()
        except Exception as e:
            print(f"DB check error: {e}")
            stale = True

    if stale:
        print("🔄  Dropping stale database and recreating …")
        db.session.remove()
        db.engine.dispose()
        os.remove(db_path)
        print("✅  Stale DB removed.")

    db.create_all()
    _migrate_agreement_template_schema(db_path)
    _migrate_billing_schema(db_path)


def _migrate_agreement_template_schema(db_path):
    """Add template table / terms snapshot without wiping live data."""
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agreement_templates (
                id INTEGER PRIMARY KEY,
                title VARCHAR(200),
                company_name VARCHAR(200),
                body TEXT,
                updated_at DATETIME
            )"""
        )
        agr_cols = [r[1] for r in conn.execute("PRAGMA table_info(agreements)").fetchall()]
        if "terms_body" not in agr_cols:
            conn.execute("ALTER TABLE agreements ADD COLUMN terms_body TEXT")
            print("✅  Added agreements.terms_body")
        tmpl_cols = [r[1] for r in conn.execute("PRAGMA table_info(agreement_templates)").fetchall()]
        if tmpl_cols and "company_name" not in tmpl_cols:
            conn.execute("ALTER TABLE agreement_templates ADD COLUMN company_name VARCHAR(200) DEFAULT 'Karyva.ai'")
            conn.execute("UPDATE agreement_templates SET company_name = 'Karyva.ai' WHERE company_name IS NULL OR company_name = ''")
            print("✅  Added agreement_templates.company_name")
        conn.commit()
    finally:
        conn.close()


def _migrate_billing_schema(db_path):
    """Add unsubscribe / activity tracking without wiping live data."""
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    try:
        lead_cols = [r[1] for r in conn.execute("PRAGMA table_info(leads)").fetchall()]
        if lead_cols and "billing_status" not in lead_cols:
            conn.execute("ALTER TABLE leads ADD COLUMN billing_status VARCHAR(20) DEFAULT 'active'")
            conn.execute("UPDATE leads SET billing_status = 'active' WHERE billing_status IS NULL")
            print("✅  Added leads.billing_status")
        if lead_cols and "unsubscribed_at" not in lead_cols:
            conn.execute("ALTER TABLE leads ADD COLUMN unsubscribed_at DATETIME")
        if lead_cols and "resubscribed_at" not in lead_cols:
            conn.execute("ALTER TABLE leads ADD COLUMN resubscribed_at DATETIME")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS subscription_activities (
                id INTEGER PRIMARY KEY,
                lead_id INTEGER NOT NULL,
                action VARCHAR(30) NOT NULL,
                channel VARCHAR(30),
                notes TEXT,
                happened_at DATE,
                user_id INTEGER,
                created_at DATETIME,
                FOREIGN KEY(lead_id) REFERENCES leads(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )"""
        )
        conn.commit()
    finally:
        conn.close()


with app.app_context():
    safe_init_db()
    seed_db()

# Start IMAP watcher (works under both flask dev server and gunicorn)
_watcher_started = False
def _start_watcher_once():
    global _watcher_started
    if not _watcher_started:
        _watcher_started = True
        start_reply_watcher()

import atexit
try:
    _start_watcher_once()
except Exception as e:
    print(f"Watcher start error: {e}")

if __name__ == "__main__":
    start_reply_watcher()
    app.run(debug=True, port=5050)
