"""
Genera el dashboard de DETALLE por campaña para International BC.
Muestra leads acumulados, leads mes a mes y CPL mes a mes
agrupados por proyecto (Unidades Nuevas, Seminuevos, CT, etc.)
y por canal (Whatsapp / Formulario).

USO:
    python generar_reporte_detalle.py

CONFIGURACION:
    Usa config_detalle.json para los parámetros.
    El token se lee de la variable de entorno META_ACCESS_TOKEN.
"""

import json
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict

import requests
from jinja2 import Template

GRAPH_API_VERSION = "v25.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

MESES_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
            "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

MESES_FULL = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
              "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def get_access_token():
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        print("ERROR: no encontre la variable de entorno META_ACCESS_TOKEN.")
        sys.exit(1)
    return token


def classify_canal(name):
    """Clasifica el canal de una campaña por palabras clave en su nombre."""
    name_lower = name.lower()
    if "reconocimiento" in name_lower or "awareness" in name_lower:
        return "reconocimiento"
    if "whatsapp" in name_lower:
        return "whatsapp"
    if "formulario" in name_lower or "lead" in name_lower or "llamada" in name_lower:
        return "formulario"
    return None  # ignorar campañas sin canal claro (trafico, landing, etc.)


def classify_proyecto(name):
    """
    Clasifica el proyecto de una campaña.
    Devuelve una clave corta y un nombre de display.
    """
    name_lower = name.lower()
    if "seminuevo" in name_lower:
        return "seminuevos", "Seminuevos"
    if "unidades nuevas" in name_lower or "l13" in name_lower or "lts13" in name_lower:
        return "unidades_nuevas", "Unidades Nuevas"
    if "ct" in name_lower and ("whatsapp" in name_lower or "formulario" in name_lower):
        return "ct", "CT"
    if "refaccion" in name_lower:
        return "refacciones", "Refacciones"
    if "mediano" in name_lower:
        return "medianos", "Medianos"
    # Fallback: usar el nombre completo de la campaña sin la palabra de canal
    return "otro", name


def fetch_monthly_by_campaign(account_id, token, since, until):
    """
    Trae resultados (whatsapp o leads) y gasto por campaña, desglosado
    mes a mes usando time_increment=monthly.
    Devuelve lista de dicts: {campaign_name, month_label, month_num, year, wa, leads, spend}
    """
    url = f"{GRAPH_API_BASE}/{account_id}/insights"
    params = {
        "level": "campaign",
        "fields": "campaign_name,spend,actions",
        "time_increment": "monthly",
        "time_range": json.dumps({"since": since, "until": until}),
        "limit": 500,
        "access_token": token,
    }

    rows = []
    next_url, next_params = url, params
    page = 1
    while next_url:
        resp = requests.get(next_url, params=next_params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        rows.extend(payload.get("data", []))
        next_url = payload.get("paging", {}).get("next")
        next_params = None
        page += 1
        if page > 20:
            break

    result = []
    for row in rows:
        name = row.get("campaign_name", "")
        spend = round(float(row.get("spend", 0)), 2)
        date_start = row.get("date_start", "")
        if not date_start:
            continue
        dt = datetime.strptime(date_start, "%Y-%m-%d")
        month_num = dt.month
        year = dt.year
        month_label = f"{MESES_ES[month_num - 1]} {str(year)[-2:]}"

        wa_count, lead_count = 0, 0
        for action in row.get("actions", []):
            atype = action.get("action_type", "")
            if atype == "onsite_conversion.messaging_conversation_started_7d":
                wa_count = int(float(action.get("value", 0)))
            if atype == "lead":
                lead_count = int(float(action.get("value", 0)))

        canal = classify_canal(name)
        if canal is None or canal == "reconocimiento":
            continue  # excluir campañas de alcance/awareness/landing

        proyecto_key, proyecto_display = classify_proyecto(name)

        result.append({
            "campaign_name": name,
            "proyecto_key": proyecto_key,
            "proyecto_display": proyecto_display,
            "canal": canal,
            "month_num": month_num,
            "year": year,
            "month_label": month_label,
            "wa": wa_count,
            "leads": lead_count,
            "spend": spend,
        })

    return result


def build_months_range(since, until):
    """Genera la lista ordenada de meses entre since y until."""
    since_dt = datetime.strptime(since, "%Y-%m-%d")
    until_dt = datetime.strptime(until, "%Y-%m-%d")
    months = []
    current = since_dt.replace(day=1)
    while current <= until_dt.replace(day=1):
        months.append({
            "key": f"{current.year}-{current.month:02d}",
            "label": f"{MESES_ES[current.month - 1]} {str(current.year)[-2:]}",
            "month_num": current.month,
            "year": current.year,
        })
        # avanzar al mes siguiente
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def build_tables(rows, months):
    """
    A partir de las filas de datos construye las 3 estructuras de tabla:

    1. tabla_acumulados: por proyecto+canal, columnas = meses + total
    2. tabla_leads_canal: por canal (WA/Form), columnas = meses + total
    3. tabla_cpl: por proyecto+canal, columnas = meses (CPL)
    """

    # ── índice base: (proyecto_key, canal) -> {month_key -> {wa, leads, spend}}
    idx = defaultdict(lambda: defaultdict(lambda: {"wa": 0, "leads": 0, "spend": 0.0}))
    proyecto_display_map = {}

    for row in rows:
        pk = row["proyecto_key"]
        canal = row["canal"]
        mkey = f"{row['year']}-{row['month_num']:02d}"
        idx[(pk, canal)][mkey]["wa"] += row["wa"]
        idx[(pk, canal)][mkey]["leads"] += row["leads"]
        idx[(pk, canal)][mkey]["spend"] += row["spend"]
        proyecto_display_map[pk] = row["proyecto_display"]

    month_keys = [m["key"] for m in months]

    def results(cell, canal):
        return cell["wa"] if canal == "whatsapp" else cell["leads"]

    def cpl(cell, canal):
        r = results(cell, canal)
        return round(cell["spend"] / r, 2) if r > 0 else None

    # ── TABLA 1: acumulados por proyecto+canal
    tabla_acumulados = []
    for (pk, canal), months_data in sorted(idx.items()):
        display = proyecto_display_map.get(pk, pk)
        row_data = []
        total = 0
        for mkey in month_keys:
            cell = months_data.get(mkey, {"wa": 0, "leads": 0, "spend": 0.0})
            val = results(cell, canal)
            row_data.append(val if val > 0 else None)
            total += val
        tabla_acumulados.append({
            "proyecto": display,
            "canal": canal,
            "meses": row_data,
            "total": total,
        })

    # ordenar: por proyecto alfabético, luego canal
    tabla_acumulados.sort(key=lambda r: (r["proyecto"], r["canal"]))

    # subtotales y total general
    subtotales = {}
    for row in tabla_acumulados:
        pk = row["proyecto"]
        if pk not in subtotales:
            subtotales[pk] = [0] * len(month_keys)
        for i, v in enumerate(row["meses"]):
            subtotales[pk][i] += v or 0

    total_general = [0] * len(month_keys)
    for vals in subtotales.values():
        for i, v in enumerate(vals):
            total_general[i] += v

    # ── TABLA 2: leads por canal mes a mes
    canal_totals = defaultdict(lambda: defaultdict(int))
    for (pk, canal), months_data in idx.items():
        for mkey, cell in months_data.items():
            canal_totals[canal][mkey] += results(cell, canal)

    tabla_canal = []
    for canal in ["whatsapp", "formulario"]:
        row_data = [canal_totals[canal].get(mkey, 0) or None for mkey in month_keys]
        total = sum(v or 0 for v in row_data)
        tabla_canal.append({
            "canal": canal,
            "meses": row_data,
            "total": total,
        })
    total_canal = [
        sum((tabla_canal[i]["meses"][j] or 0) for i in range(len(tabla_canal)))
        for j in range(len(month_keys))
    ]

    # ── TABLA 3: CPL por proyecto+canal
    tabla_cpl = []
    for (pk, canal), months_data in sorted(idx.items()):
        display = proyecto_display_map.get(pk, pk)
        row_data = []
        for mkey in month_keys:
            cell = months_data.get(mkey, {"wa": 0, "leads": 0, "spend": 0.0})
            row_data.append(cpl(cell, canal))
        tabla_cpl.append({
            "proyecto": display,
            "canal": canal,
            "meses": row_data,
        })
    tabla_cpl.sort(key=lambda r: (r["proyecto"], r["canal"]))

    return {
        "meses": months,
        "tabla_acumulados": tabla_acumulados,
        "subtotales": subtotales,
        "total_general": total_general,
        "tabla_canal": tabla_canal,
        "total_canal": total_canal,
        "tabla_cpl": tabla_cpl,
    }


def main():
    config_path = Path(__file__).parent / "config_detalle.json"
    if not config_path.exists():
        print("ERROR: no encontre config_detalle.json.")
        sys.exit(1)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    token = get_access_token()
    account_id = config["account_id"]
    since = config["since"]
    until = config["until"]
    if str(until).strip().lower() == "today":
        until = date.today().strftime("%Y-%m-%d")

    print(f"Jalando datos detalle de {account_id} ({since} → {until})...")
    rows = fetch_monthly_by_campaign(account_id, token, since, until)
    print(f"  {len(rows)} filas de datos obtenidas.")

    months = build_months_range(since, until)
    tables = build_tables(rows, months)

    template_path = Path(__file__).parent / "template_detalle.html"
    template = Template(template_path.read_text(encoding="utf-8"))

    html = template.render(
        agency_name=config.get("agency_name", "Quattro Marketing"),
        client_name=config["client_name"],
        period_label=config.get("period_label", "2026"),
        generated_date=date.today().strftime("%d/%m/%Y"),
        until_label=datetime.strptime(until, "%Y-%m-%d").strftime("%d/%m/%Y"),
        **tables,
    )

    output_dir = Path(__file__).parent / config.get("output_dir", "docs")
    output_dir.mkdir(exist_ok=True, parents=True)
    output_file = output_dir / config.get("output_filename", "international-bc-detalle.html")
    output_file.write_text(html, encoding="utf-8")
    print(f"Listo! Detalle generado en: {output_file}")


if __name__ == "__main__":
    main()
