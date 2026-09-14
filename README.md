# Revfi.ai SubsManager

Subscription management platform — Leads, Agreements, Invoices, Commissions.  
Built with Flask + SQLAlchemy + SQLite. Matches Raya Finance & Temple app stack.

## Stack
- Python 3.x / Flask 3.0
- SQLAlchemy + SQLite (`data/revfi.db`)
- Bootstrap 5.3 dark theme
- Gunicorn + Apache proxy (TMDHosting)

## Local Development

```bash
cd revfi
pip install -r requirements.txt
python app.py
# → http://localhost:5050
```

Default login: `info@karyva.ai` / `Admin@123`  
Sales reps default: `Sales@123`

## TMDHosting Deploy

On the host **never** run `git pull` or `git pull --rebase` (runtime logs/pid files and leftover rebases break the tree). Update with:

```bash
cd ~/revfi
./start.sh pull
./start.sh deps
./start.sh start
```

That fetches `origin/master`, aborts any stuck rebase, hard-resets code, and leaves `data/revfi.db` alone.

```bash
# First-time process start (local gunicorn helper)
bash deploy.sh
```

Configure Apache proxy in `.htaccess` (same as Raya app):
```apache
RewriteEngine On
RewriteRule ^(.*)$ http://127.0.0.1:5050/$1 [P,L]
```

## Modules

| Module | Path | Description |
|---|---|---|
| Dashboard | `/` | KPIs, pipeline chart, recent leads |
| Leads | `/leads` | Full CRUD, assign reps, kanban-style status |
| Subscribers | `/subscribers` | Active Closed Won accounts + quick invoice |
| Agreements | `/agreements` | Send + sign digital agreements |
| Invoices | `/invoices` | Manual + bulk invoice generation |
| Commissions | `/commissions` | Sales + referral commission tracking |
| Products | `/products` | FreshFi, RezFi + any future product |
| Users | `/users` | Sales reps and admin management |

## Adding a New Product (e.g. PropFi)

Go to **Products & Plans → Add Product**, enter:
- Name: PropFi
- Slug: propfi
- Icon: 🏠
- Color: pick brand color
- Description: ...

Then add plans under it. It will immediately appear in the lead form product dropdown.

## Commission Logic

- Sales reps earn 12% MRR by default (edit `SALES_COMMISSION_RATE` in `app.py`)
- Referral % is set per lead when creating/editing
- Commissions are auto-created when an agreement is marked Signed
- Admin can mark commissions Paid from the Commissions page

## Database

SQLite at `data/revfi.db`. Backup with:
```bash
cp data/revfi.db data/revfi.db.bak
```
