#!/usr/bin/env bash
set -euo pipefail

# ---- Pre-set env (edit only REPO_URL and SA path if needed)
export GOOGLE_APPLICATION_CREDENTIALS="/absolute/path/to/service-account.json"
export GCP_PROJECT="proh-c5886"
export GCLOUD_PROJECT="proh-c5886"
export GCS_BUCKET="proh-c5886.appspot.com"
export REPO_URL="https://github.com/<your>/<android-repo>.git"   # <-- change to your repo
export APP_DIR="app"
export TELEGRAM_BOT_TOKEN="7451661904:AAE05YaujmpJQHNqc67lTBsXczL3qosBZSY"

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
