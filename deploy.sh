#!/bin/bash
# Karyva.ai SubsManager — deploy script for TMDHosting
# Same pattern as Raya Finance app
# Usage: bash deploy.sh

APP_DIR=~/revfi

cd "$APP_DIR" || exit 1

# Generate secret key on first deploy
if [ ! -f data/.secret_key ]; then
  python3 -c "import secrets; print(secrets.token_hex(32))" > data/.secret_key
  echo "✅ Secret key generated."
fi

# Kill existing gunicorn
pkill -f "gunicorn.*revfi" 2>/dev/null || true
sleep 2

# Start gunicorn on port 5050
SECRET=$(cat data/.secret_key)
FLASK_SECRET_KEY="$SECRET" nohup ~/.local/bin/gunicorn \
  -w 1 \
  -b 127.0.0.1:5050 \
  --timeout 300 \
  --log-file gunicorn.log \
  app:app >> gunicorn.log 2>&1 &

echo "✅ Revfi SubsManager started on port 5050"
echo "   Tail logs: tail -f $APP_DIR/gunicorn.log"
