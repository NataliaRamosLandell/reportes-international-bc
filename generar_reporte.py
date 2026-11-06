"""
Genera el dashboard HTML de International BC a partir de datos reales de Meta Ads.
Junta los datos de TODAS las cuentas publicitarias listadas en
config.json ("account_ids").

El token se lee de la variable de entorno META_ACCESS_TOKEN.
"""

import json
import os
import sys
import base64
import re
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from jinja2 import Template

GRAPH_API_VERSION = "v25.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
WA_ACTION = "onsite_conversion.messaging_conversation_started_7d"


def get_access_token():
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        print("ERROR: no encontre la variable de entorno META_ACCESS_TOKEN.")
        sys.exit(1)
    return token


def get_account_ids(config):
    ids = config.get("account_ids") or [config["account_id"]]
    return [i if str(i).startswith("act_") else f"act_{i}" for i in ids]


def as_list(x):
    return [x] if isinstance(x, str) else list(x)


def chunked(seq, n=50):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def graph_get_all(url, params, max_pages=20):
    """Hace el GET y sigue la paginacion. Si Meta responde con error
    (ej. falta de permisos sobre una cuenta) lo muestra claro y detiene
    el reporte, para no publicar numeros incompletos."""
    rows = []
    next_url, next_params = url, params
    page = 1
     while next_url:
        resp = requests.get(next_url, params=next_params, timeout=60)
        if not resp.ok:
