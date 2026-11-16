# PLACEHUB — Oracle Cloud VM deployment

This package preserves the existing Flask routes, templates, role hierarchy,
login flows, SQLite schema, and application workflows. It does not initialize
a new database or create replacement accounts.

## 1. VM layout

The service files assume this layout:

- Application: `/opt/placehub/app`
- Virtual environment: `/opt/placehub/venv`
- SQLite database: `/var/lib/placehub/data/database.db`
- Uploaded files: `/var/lib/placehub/uploads`
- Environment file: `/etc/placehub/placehub.env`

Use a persistent boot volume or block volume for `/var/lib/placehub`.
Do not put the database or uploads in a temporary deployment directory.

## 2. Install OS packages

On Ubuntu 24.04:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip build-essential     nginx sqlite3 libffi-dev libssl-dev
```

Create service directories and account:

```bash
sudo useradd --system --create-home --home-dir /opt/placehub     --shell /usr/sbin/nologin placehub
sudo mkdir -p /opt/placehub/app /var/lib/placehub/data     /var/lib/placehub/uploads /etc/placehub
sudo chown -R placehub:www-data /opt/placehub /var/lib/placehub
sudo chmod 750 /var/lib/placehub /var/lib/placehub/data     /var/lib/placehub/uploads
```

## 3. Copy the existing project

Copy the existing project source into `/opt/placehub/app`. Keep the existing
`ADMIN/`, templates, static assets, database module, requirements file,
and all other project modules/directories unchanged.

Add `wsgi.py` and the `deploy/` configuration files from this deployment update.
Do not replace the existing application source or initialize a new database.



## 4. Configure environment variables

Create `/etc/placehub/placehub.env` with these values:

```ini
SECRET_KEY=YOUR_EXISTING_SECRET_KEY_VALUE
DATABASE_PATH=/var/lib/placehub/data/database.db
UPLOADS_DIR=/var/lib/placehub/uploads
```

Use the same existing SECRET_KEY value if you already have one. Do not paste
it into chat or commit this file. Secure it:

```bash
sudo chown root:www-data /etc/placehub/placehub.env
sudo chmod 640 /etc/placehub/placehub.env
```

## 5. Preserve and transfer the existing data

Before starting the service, copy the verified backup database to:

`/var/lib/placehub/data/database.db`

Copy the contents of the existing `static/uploads/` directory into:

`/var/lib/placehub/uploads/`

Preserve the subdirectories (`resumes`, `profile_pictures`, `profiles`) and
their file names. Do not run `database.py`, `initialize_production_db.py`,
or any schema migration script as a way to create a fresh production DB.

After copying:

```bash
sudo chown -R placehub:www-data /var/lib/placehub
sudo chmod 750 /var/lib/placehub/data /var/lib/placehub/uploads
sudo chmod 640 /var/lib/placehub/data/database.db
sqlite3 /var/lib/placehub/data/database.db 'PRAGMA integrity_check;'
sqlite3 /var/lib/placehub/data/database.db 'PRAGMA foreign_key_check;'
```

The integrity check should print `ok`; the foreign-key check should print no
rows. Keep the original backup untouched until production login and file
access have been tested.

## 6. Python environment

```bash
sudo -u placehub python3 -m venv /opt/placehub/venv
sudo -u placehub /opt/placehub/venv/bin/pip install --upgrade pip
sudo -u placehub /opt/placehub/venv/bin/pip install -r /opt/placehub/app/requirements.txt
```

The current requirements include scikit-learn for the TF-IDF fallback.
SentenceTransformer remains optional and lazily loaded by the existing app.
No large transformer/PyTorch dependency is added by this deployment package.
If the optional transformer package/model is not available, existing TF-IDF
fallback behavior remains in place.

## 7. Configure systemd and Nginx

```bash
sudo cp /opt/placehub/app/deploy/systemd/placehub.service /etc/systemd/system/placehub.service
sudo cp /opt/placehub/app/deploy/nginx/placehub /etc/nginx/sites-available/placehub
sudo ln -s /etc/nginx/sites-available/placehub /etc/nginx/sites-enabled/placehub
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now placehub
sudo systemctl restart nginx
```

Edit `server_name` in the Nginx file to your public IP or domain before
enabling it. If the default Nginx site conflicts, disable that site only after
checking your VM's existing Nginx configuration.

## 8. Oracle Cloud network rules

Allow inbound TCP 80 and 443 in the Oracle Cloud VCN security list or network
security group. Keep Gunicorn bound to `127.0.0.1:8000`; do not expose port
8000 publicly. Add HTTPS with a certificate after DNS/domain setup.

## 9. Verify service

```bash
sudo systemctl status placehub --no-pager
sudo journalctl -u placehub -n 100 --no-pager
curl -I http://127.0.0.1
```

Then test the existing Super Admin login, existing dashboards and roles,
student application flow, and access to existing resumes/profile pictures.
Do not create a new Super Admin if the existing account is present.

## 10. GitHub update safety

From your Windows project folder, copy the deployment files into the existing
repository. Review the changes:

```powershell
git status --short
git diff -- .gitignore app.py database.py requirements.txt
git check-ignore -v .env database/database.db static/uploads/
```

Do not add `.env`, SQLite databases, uploads, backups, virtual environments,
or model caches. Commit and push only the deployment source/configuration files
after reviewing the staged file list:

```powershell
git add app.py database.py requirements.txt .gitignore wsgi.py .env.example deploy/
git diff --cached --name-only
git diff --cached
```

Only then commit and push to the existing `origin` remote.
