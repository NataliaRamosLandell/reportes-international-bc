"""
Genera el dashboard de DETALLE por anuncio para International BC.
Muestra una tabla con todos los anuncios, mes a mes, con filtros
interactivos por mes y campaña — igual que el Sheet de Meta Ads.

USO:
    python generar_reporte_detalle.py

CONFIGURACION:
    Usa config_detalle.json para los parámetros.
    El token se lee de la variable de entorno META_ACCESS_TOKEN.
"""

import json
import os
import sys
from datetime import datetime, date
from pathlib import Path

import requests
from jinja2 import Template

GRAPH_API_VERSION = "v25.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

MESES_ES = {
    1:"Ene", 2:"Feb",
