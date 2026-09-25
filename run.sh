#!/bin/bash
source .venv/bin/activate
# Chaque page ouverte garde un flux /api/changes qui ne se termine jamais : sans
# délai, --reload attend indéfiniment qu'il se ferme, et le serveur ne répond
# plus dès qu'une page est ouverte sur l'iPad.
uvicorn main:app --reload --timeout-graceful-shutdown 2 --host 0.0.0.0 --port 8000
