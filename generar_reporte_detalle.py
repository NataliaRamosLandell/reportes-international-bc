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
    1:"Ene", 2:"Feb", 3:"Mar", 4:"Abr", 5:"May", 6:"Jun",
    7:"Jul", 8:"Ago", 9:"Sep", 10:"Oct", 11:"Nov", 12:"Dic"
}


def get_access_token():
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        print("ERROR: no encontre la variable de entorno META_ACCESS_TOKEN.")
        sys.exit(1)
    return token


def fetch_ad_insights_monthly(account_id, token, since, until):
    url = f"{GRAPH_API_BASE}/{account_id}/insights"
    params = {
        "level": "ad",
        "fields": (
            "ad_name,campaign_name,adset_name,"
            "reach,impressions,frequency,spend,actions,"
            "cost_per_action_type,cpm,cpc,ctr,"
            "inline_link_clicks,website_ctr"
        ),
        "time_increment": "monthly",
        "time_range": json.dumps({"since": since, "until": until}),
        "limit": 500,
        "access_token": token,
    }

    rows = []
    next_url, next_params = url, params
    page = 1
    while next_url:
        resp = requests.get(next_url, params=next_params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        rows.extend(payload.get("data", []))
        next_url = payload.get("paging", {}).get("next")
        next_params = None
        page += 1
        if page > 50:
            break

    print(f"  {len(rows)} filas brutas obtenidas de la API.")
    return rows


def parse_rows(rows):
    parsed = []
    for row in rows:
        date_start = row.get("date_start", "")
        if not date_start:
            continue
        dt = datetime.strptime(date_start, "%Y-%m-%d")
        mes_label = f"{MESES_ES[dt.month]} {dt.year}"

        ad_name       = row.get("ad_name", "")
        campaign_name = row.get("campaign_name", "")
        adset_name    = row.get("adset_name", "")
        reach         = int(float(row.get("reach", 0)))
        impressions   = int(float(row.get("impressions", 0)))
        spend         = round(float(row.get("spend", 0)), 2)
        freq          = round(float(row.get("frequency", 0) or 0), 2)
        cpm           = round(float(row.get("cpm", 0) or 0), 2)
        ctr_all       = round(float(row.get("ctr", 0) or 0), 6)
        cpc_all       = round(float(row.get("cpc", 0) or 0), 2)
        inline_clicks = int(float(row.get("inline_link_clicks", 0) or 0))
        inline_ctr    = round(float((row.get("website_ctr") or [{}])[0].get("value", 0) or 0), 6) if row.get("website_ctr") else 0
        inline_cpc    = round(spend / inline_clicks, 2) if inline_clicks else 0

        actions  = row.get("actions", [])
        cost_per = row.get("cost_per_action_type", [])

        wa_count   = 0
        lead_count = 0
        lp_count   = 0

        for a in actions:
            atype = a.get("action_type", "")
            val   = int(float(a.get("value", 0)))
            if atype == "onsite_conversion.messaging_conversation_started_7d":
                wa_count = val
            elif atype == "lead":
                lead_count = val
            elif atype == "landing_page_view":
                lp_count = val

        if wa_count > 0:
            resultado = wa_count
            indicador = "Conversaciones de mensajería iniciadas"
            cpr_raw   = next((float(c.get("value", 0)) for c in cost_per
                              if c.get("action_type") == "onsite_conversion.messaging_conversation_started_7d"), 0)
        elif lead_count > 0:
            resultado = lead_count
            indicador = "Leads (formulario)"
            cpr_raw   = next((float(c.get("value", 0)) for c in cost_per
                              if c.get("action_type") == "lead"), 0)
        elif lp_count > 0:
            resultado = lp_count
            indicador = "Visitas a página de destino"
            cpr_raw   = round(spend / lp_count, 2) if lp_count else 0
        else:
            resultado = 0
            indicador = "Sin resultados registrados"
            cpr_raw   = 0

        parsed.append({
            "mes":          mes_label,
            "mes_sort":     date_start[:7],
            "campaña":      campaign_name,
            "conjunto":     adset_name,
            "anuncio":      ad_name,
            "resultados":   resultado,
            "indicador":    indicador,
            "alcance":      reach,
            "frecuencia":   freq,
            "cpr":          round(cpr_raw, 2),
            "gasto":        spend,
            "impresiones":  impressions,
            "cpm":          cpm,
            "clics_enlace": inline_clicks,
            "cpc_enlace":   inline_cpc,
            "ctr_enlace":   inline_ctr,
            "clics_todos":  int(float(row.get("clicks", 0) or 0)),
            "ctr_todos":    ctr_all,
            "cpc_todos":    cpc_all,
        })

    parsed.sort(key=lambda r: (r["mes_sort"], r["campaña"], r["anuncio"]), reverse=False)
    parsed.sort(key=lambda r: r["mes_sort"], reverse=True)
    return parsed


def main():
    config_path = Path(__file__).parent / "config_detalle.json"
    if not config_path.exists():
        print("ERROR: no encontre config_detalle.json.")
        sys.exit(1)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    token      = get_access_token()
    account_id = config["account_id"]
    since      = config["since"]
    until      = config["until"]
    if str(until).strip().lower() == "today":
        until = date.today().strftime("%Y-%m-%d")

    print(f"Jalando datos detalle de {account_id} ({since} → {until})...")
    raw_rows = fetch_ad_insights_monthly(account_id, token, since, until)
    rows     = parse_rows(raw_rows)
    print(f"  {len(rows)} filas procesadas.")

    meses        = sorted(set(r["mes_sort"] for r in rows), reverse=True)
    meses_labels = {r["mes_sort"]: r["mes"] for r in rows}
    campanas     = sorted(set(r["campaña"] for r in rows))

    template_path = Path(__file__).parent / "template_detalle.html"
    template      = Template(template_path.read_text(encoding="utf-8"))

    html = template.render(
        agency_name    = config.get("agency_name", "Quattro Marketing"),
        client_name    = config["client_name"],
        period_label   = config.get("period_label", "2026"),
        generated_date = date.today().strftime("%d/%m/%Y"),
        until_label    = datetime.strptime(until, "%Y-%m-%d").strftime("%d/%m/%Y"),
        rows_json      = json.dumps(rows, ensure_ascii=False),
        meses_json     = json.dumps([{"key": m, "label": meses_labels[m]} for m in meses], ensure_ascii=False),
        campanas_json  = json.dumps(campanas, ensure_ascii=False),
    )

    output_dir  = Path(__file__).parent / config.get("output_dir", "docs")
    output_dir.mkdir(exist_ok=True, parents=True)
    output_file = output_dir / config.get("output_filename", "international-bc-detalle.html")
    output_file.write_text(html, encoding="utf-8")
    print(f"Listo! Detalle generado en: {output_file}")


if __name__ == "__main__":
    main()
