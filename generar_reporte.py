"""
Genera el dashboard HTML de un cliente a partir de datos reales de Meta Ads.

USO:
    python generar_reporte.py

CONFIGURACION:
    Copia config.example.json a config.json y llena tus datos.
    El token de acceso NUNCA va en config.json -- se lee de la variable
    de entorno META_ACCESS_TOKEN (ver README.md).
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


def get_access_token():
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        print("ERROR: no encontre la variable de entorno META_ACCESS_TOKEN.")
        print("Ve el README.md para saber como configurarla.")
        sys.exit(1)
    return token


def fetch_campaign_cost_breakdown(account_id, token, since, until):
    url = f"{GRAPH_API_BASE}/{account_id}/insights"
    params = {
        "level": "campaign",
        "fields": "campaign_name,spend,actions",
        "time_range": json.dumps({"since": since, "until": until}),
        "limit": 200,
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("data", [])

    spend_wa_total = 0.0
    spend_leads_total = 0.0

    for row in rows:
        spend = float(row.get("spend", 0))
        wa_count, lead_count = 0, 0
        for action in row.get("actions", []):
            action_type = action.get("action_type", "")
            if action_type == "onsite_conversion.messaging_conversation_started_7d":
                wa_count = int(float(action.get("value", 0)))
            if action_type == "lead":
                lead_count = int(float(action.get("value", 0)))

        total = wa_count + lead_count
        if total == 0:
            continue
        spend_wa_total += spend * (wa_count / total)
        spend_leads_total += spend * (lead_count / total)

    return spend_wa_total, spend_leads_total


def fetch_range_insights(account_id, token, since, until):
    url = f"{GRAPH_API_BASE}/{account_id}/insights"
    params = {
        "fields": "reach,spend,frequency,actions",
        "time_range": json.dumps({"since": since, "until": until}),
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        return {"reach": 0, "spend": 0.0, "freq": 0.0, "whatsapp": 0, "leads": 0}

    row = data[0]
    wa_count, lead_count = 0, 0
    for action in row.get("actions", []):
        action_type = action.get("action_type", "")
        if action_type == "onsite_conversion.messaging_conversation_started_7d":
            wa_count = int(float(action.get("value", 0)))
        if action_type == "lead":
            lead_count = int(float(action.get("value", 0)))

    return {
        "reach": int(float(row.get("reach", 0))),
        "spend": round(float(row.get("spend", 0)), 2),
        "freq": round(float(row.get("frequency", 0)), 1),
        "whatsapp": wa_count,
        "leads": lead_count,
    }


def classify_objective(name, wa_count, lead_count, reach):
    name_lower = name.lower()
    if "reconocimiento" in name_lower or "awareness" in name_lower:
        return "Reconocimiento", reach, True
    if "whatsapp" in name_lower:
        return "Whatsapp", wa_count, False
    if "formulario" in name_lower or "lead" in name_lower:
        return "Formulario", lead_count, False
    if lead_count > wa_count:
        return "Formulario", lead_count, False
    if wa_count > 0:
        return "Whatsapp", wa_count, False
    return "Otro", reach, True


def fetch_campaigns_breakdown(account_id, token, since, until):
    url = f"{GRAPH_API_BASE}/{account_id}/insights"
    params = {
        "level": "campaign",
        "fields": "campaign_name,reach,spend,frequency,actions",
        "time_range": json.dumps({"since": since, "until": until}),
        "limit": 200,
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("data", [])

    campaigns = []
    for row in rows:
        name = row.get("campaign_name", "")
        reach = int(float(row.get("reach", 0)))
        spend = round(float(row.get("spend", 0)), 2)
        freq = round(float(row.get("frequency", 0)), 1)

        wa_count, lead_count = 0, 0
        for action in row.get("actions", []):
            action_type = action.get("action_type", "")
            if action_type == "onsite_conversion.messaging_conversation_started_7d":
                wa_count = int(float(action.get("value", 0)))
            if action_type == "lead":
                lead_count = int(float(action.get("value", 0)))

        objetivo, resultado, es_awareness = classify_objective(name, wa_count, lead_count, reach)
        cost_per_result = (spend / resultado) if (resultado and not es_awareness) else None

        campaigns.append({
            "name": name,
            "objetivo": objetivo,
            "es_awareness": es_awareness,
            "reach": f"{reach:,}",
            "spend": f"${spend:,.2f} MXN",
            "freq": f"{freq:.1f}",
            "resultado": f"{resultado:,}",
            "costo_resultado": f"${cost_per_result:,.2f}" if cost_per_result else "N/A",
            "_spend_raw": spend,
        })

    campaigns.sort(key=lambda c: c["_spend_raw"], reverse=True)
    return campaigns


def fetch_monthly_insights(account_id, token, since, until):
    url = f"{GRAPH_API_BASE}/{account_id}/insights"
    params = {
        "fields": "reach,spend,frequency,actions",
        "time_increment": "monthly",
        "time_range": json.dumps({"since": since, "until": until}),
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("data", [])

    months, reach, spend, freq, whatsapp, leads = [], [], [], [], [], []
    for row in data:
        month_label = datetime.strptime(row["date_start"], "%Y-%m-%d").strftime("%b")
        months.append(month_label)
        reach.append(int(float(row.get("reach", 0))))
        spend.append(round(float(row.get("spend", 0)), 2))
        freq.append(round(float(row.get("frequency", 0)), 1))

        wa_count, lead_count = 0, 0
        for action in row.get("actions", []):
            action_type = action.get("action_type", "")
            if action_type == "onsite_conversion.messaging_conversation_started_7d":
                wa_count = int(float(action.get("value", 0)))
            if action_type == "lead":
                lead_count = int(float(action.get("value", 0)))
        whatsapp.append(wa_count)
        leads.append(lead_count)

    return {
        "months": months,
        "reach": reach,
        "spend": spend,
        "freq": freq,
        "whatsapp": whatsapp,
        "leads": leads,
    }


def fetch_video_thumbnail(video_id, token):
    url = f"{GRAPH_API_BASE}/{video_id}"
    params = {"fields": "thumbnails", "access_token": token}
    resp = requests.get(url, params=params, timeout=30)
    if not resp.ok:
        return None
    thumbs = resp.json().get("thumbnails", {}).get("data", [])
    if not thumbs:
        return None
    best = max(thumbs, key=lambda t: t.get("width", 0))
    return best.get("uri")


def fetch_boosted_post_media(post_id, token):
    url = f"{GRAPH_API_BASE}/{post_id}"
    params = {
        "fields": "full_picture,attachments{media_type,media,url}",
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    if not resp.ok:
        return None

    data = resp.json()
    attachments = data.get("attachments", {}).get("data", [])
    for att in attachments:
        media = att.get("media", {})
        image = media.get("image", {})
        if image.get("src"):
            return image["src"]

    if data.get("full_picture"):
        return data["full_picture"]

    return None


def download_and_embed_image(url):
    if not url or url.startswith("data:"):
        return url
    try:
        resp = requests.get(url, timeout=20)
        if not resp.ok:
            return url
        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        data = base64.b64encode(resp.content).decode("ascii")
        return f"data:{content_type};base64,{data}"
    except requests.RequestException:
        return url


def load_manual_image(prefix, index):
    folder = Path(__file__).parent / "images_manual"
    for ext in ("jpg", "jpeg", "png", "webp"):
        candidate = folder / f"{prefix}_{index}.{ext}"
        if candidate.exists():
            mime = "jpeg" if ext == "jpg" else ext
            data = base64.b64encode(candidate.read_bytes()).decode("ascii")
            return f"data:image/{mime};base64,{data}"
    return None


def fetch_ad_level_metrics(account_id, token, date_preset=None, since=None, until=None,
                            attribution_windows=None):
    url = f"{GRAPH_API_BASE}/{account_id}/insights"
    params = {
        "level": "ad",
        "fields": "ad_id,ad_name,campaign_id,campaign_name,reach,spend,actions",
        "limit": 200,
        "access_token": token,
    }
    if since and until:
        params["time_range"] = json.dumps({"since": since, "until": until})
    else:
        params["date_preset"] = date_preset or "last_month"
    if attribution_windows:
        params["action_attribution_windows"] = ",".join(attribution_windows)

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
    return rows


def fetch_current_campaign_names(token, campaign_ids):
    if not campaign_ids:
        return {}
    url = f"{GRAPH_API_BASE}/"
    params = {
        "ids": ",".join(str(c) for c in campaign_ids),
        "fields": "name",
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {cid: info.get("name", cid) for cid, info in data.items()}


def fetch_top_ad_per_campaign(account_id, token, since=None, until=None,
                               date_preset="last_month", attribution_windows=None,
                               top_n=3):
    rows = fetch_ad_level_metrics(
        account_id, token, date_preset=date_preset, since=since, until=until,
        attribution_windows=attribution_windows,
    )

    grouped = {}
    campaign_totals = {}
    for row in rows:
        campaign_id = row.get("campaign_id") or row.get("campaign_name", "sin_campaña")
        campaign_name = row.get("campaign_name", "Sin campaña")
        ad_name = row.get("ad_name", "")
        reach = int(float(row.get("reach", 0)))
        spend = round(float(row.get("spend", 0)), 2)

        wa_count, lead_count = 0, 0
        for action in row.get("actions", []):
            action_type = action.get("action_type", "")
            if action_type == "onsite_conversion.messaging_conversation_started_7d":
                wa_count = int(float(action.get("value", 0)))
            if action_type == "lead":
                lead_count = int(float(action.get("value", 0)))

        ct = campaign_totals.setdefault(
            campaign_id, {"reach": 0, "spend": 0.0, "whatsapp": 0, "leads": 0, "nombres": {}}
        )
        ct["reach"] += reach
        ct["spend"] += spend
        ct["whatsapp"] += wa_count
        ct["leads"] += lead_count
        ct["nombres"][campaign_name] = ct["nombres"].get(campaign_name, 0) + spend

        normalized_name = re.sub(
            r"\s*-\s*copia(\s*\d+)?\s*$", "", ad_name, flags=re.IGNORECASE
        ).strip().lower()
        key = (campaign_id, normalized_name or ad_name.strip() or row.get("ad_id"))

        if key not in grouped:
            grouped[key] = {
                "campaign_id": campaign_id,
                "name": ad_name,
                "reach": 0,
                "spend": 0.0,
                "whatsapp": 0,
                "leads": 0,
                "best_ad_id": row.get("ad_id"),
                "best_reach": -1,
            }
        g = grouped[key]
        g["reach"] += reach
        g["spend"] += spend
        g["whatsapp"] += wa_count
        g["leads"] += lead_count
        if reach > g["best_reach"]:
            g["best_reach"] = reach
            g["best_ad_id"] = row.get("ad_id")

    campaign_display_names = fetch_current_campaign_names(token, list(campaign_totals.keys()))

    winners_by_campaign = {}
    for campaign_id, totals in campaign_totals.items():
        current_name = campaign_display_names.get(campaign_id) or max(totals["nombres"], key=totals["nombres"].get)

        objetivo, _, es_awareness = classify_objective(
            current_name, totals["whatsapp"], totals["leads"], totals["reach"]
        )
        metric_key = "reach" if es_awareness else ("whatsapp" if objetivo == "Whatsapp" else "leads")

        ads_in_campaign = [dict(a) for a in grouped.values() if a["campaign_id"] == campaign_id]
        ads_in_campaign.sort(key=lambda a: a[metric_key], reverse=True)
        top = ads_in_campaign[:top_n]
        for ad in top:
            ad["objetivo"] = objetivo
            ad["metric_key"] = metric_key
        winners_by_campaign[campaign_id] = top

    all_winners = [ad for ads in winners_by_campaign.values() for ad in ads]
    winner_ids = list({a["best_ad_id"] for a in all_winners if a.get("best_ad_id")})
    creative_by_id = fetch_ads_creative_info(account_id, token, winner_ids)

    def enrich(ad, prefix, i):
        info = creative_by_id.get(ad["best_ad_id"], {})
        creative = info.get("creative", {})
        image = creative.get("image_url") or creative.get("thumbnail_url", "")
        video_id = creative.get("object_story_spec", {}).get("video_data", {}).get("video_id")
        afs = creative.get("asset_feed_spec", {})
        if not video_id and afs.get("videos"):
            video_id = afs["videos"][0].get("video_id")
            if not image:
                image = afs["videos"][0].get("thumbnail_url") or image
        if afs.get("images") and (creative.get("object_type") or "").upper() != "VIDEO":
            image = afs["images"][0].get("url") or image
        story_id = creative.get("effective_object_story_id")

        manual = load_manual_image(prefix, i)
        if manual:
            image = manual
        elif video_id:
            better = fetch_video_thumbnail(video_id, token)
            if better:
                image = better
        elif story_id:
            better = fetch_boosted_post_media(story_id, token)
            if better:
                image = better

        ad["name"] = (creative.get("body") or ad["name"])[:90]
        ad["format"] = (creative.get("object_type") or "ANUNCIO").upper()
        ad["thumbnail_url"] = download_and_embed_image(image)
        ad["reach"] = f"{ad['reach']:,}"
        ad["spend"] = f"${ad['spend']:,.2f} MXN"
        ad["whatsapp"] = f"{ad['whatsapp']:,}"
        ad["leads"] = f"{ad['leads']:,}"
        return ad

    result_by_theme = {}
    i = 1
    for campaign_id, ads in winners_by_campaign.items():
        campaign_name = campaign_display_names.get(campaign_id, str(campaign_id))
        enriched = [enrich(ad, "campaign", i + idx) for idx, ad in enumerate(ads)]
        for idx, ad in enumerate(enriched):
            ad["_manual_index"] = i + idx
        i += len(ads)
        if not enriched:
            continue
        theme = re.sub(
            r"\b(whatsapp|formulario|reconocimiento|awareness|leads?)\b",
            "", campaign_name, flags=re.IGNORECASE,
        )
        theme = re.sub(r"[/\-]+", " ", theme)
        theme = re.sub(r"\s+", " ", theme).strip(" /-")
        theme_key = theme.lower() or campaign_name.lower()
        if theme_key not in result_by_theme:
            result_by_theme[theme_key] = {"campaign_name": theme or campaign_name, "ads": []}
        result_by_theme[theme_key]["ads"].extend(enriched)

    final = list(result_by_theme.values())
    print("  Top por campaña:")
    for group in final:
        for ad in group["ads"]:
            idx = ad.get("_manual_index", "?")
            print(f"    [{group['campaign_name']}] campaign_{idx} | {ad['format']} | {ad['name'][:40]}")
    return final


def fetch_ads_creative_info(account_id, token, ad_ids):
    if not ad_ids:
        return {}
    url = f"{GRAPH_API_BASE}/"
    params = {
        "ids": ",".join(ad_ids),
        "fields": (
            "name,creative{id,thumbnail_url.width(1080).height(1350),body,"
            "object_type,image_url,object_story_spec,effective_object_story_id,"
            "asset_feed_spec{images{url},videos{video_id,thumbnail_url}}}"
        ),
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_top_ads(account_id, token, date_preset="last_month", since=None, until=None,
                   limit=3, attribution_windows=None):
    rows = fetch_ad_level_metrics(
        account_id, token, date_preset=date_preset, since=since, until=until,
        attribution_windows=attribution_windows,
    )

    grouped = {}
    for row in rows:
        reach = int(float(row.get("reach", 0)))
        spend = round(float(row.get("spend", 0)), 2)
        ad_name = row.get("ad_name", "")
        ad_id = row.get("ad_id")

        wa_count, lead_count = 0, 0
        for action in row.get("actions", []):
            action_type = action.get("action_type", "")
            if action_type == "onsite_conversion.messaging_conversation_started_7d":
                wa_count = int(float(action.get("value", 0)))
            if action_type == "lead":
                lead_count = int(float(action.get("value", 0)))

        normalized_name = re.sub(
            r"\s*-\s*copia(\s*\d+)?\s*$", "", ad_name, flags=re.IGNORECASE
        ).strip().lower()
        key = normalized_name or ad_name.strip() or ad_id

        if key not in grouped:
            grouped[key] = {
                "name": ad_name,
                "reach": 0,
                "spend": 0.0,
                "whatsapp": 0,
                "leads": 0,
                "best_ad_id": ad_id,
                "best_reach": -1,
                "n_campañas": 0,
                "_rows": [],
            }
        g = grouped[key]
        g["reach"] += reach
        g["spend"] += spend
        g["whatsapp"] += wa_count
        g["leads"] += lead_count
        g["n_campañas"] += 1
        g["_rows"].append((ad_id, ad_name, wa_count, lead_count))
        if reach > g["best_reach"]:
            g["best_reach"] = reach
            g["best_ad_id"] = ad_id

    all_ads = list(grouped.values())

    candidatos_wa = sorted(
        [dict(a) for a in all_ads if a["whatsapp"] > 0],
        key=lambda a: a["whatsapp"], reverse=True,
    )[:limit]
    candidatos_leads = sorted(
        [dict(a) for a in all_ads if a["leads"] > 0],
        key=lambda a: a["leads"], reverse=True,
    )[:limit]

    winner_ids = list({a["best_ad_id"] for a in (candidatos_wa + candidatos_leads) if a["best_ad_id"]})
    creative_by_id = fetch_ads_creative_info(account_id, token, winner_ids)

    def enrich_with_creative(ad, prefix, i):
        info = creative_by_id.get(ad["best_ad_id"], {})
        creative = info.get("creative", {})
        image = creative.get("image_url") or creative.get("thumbnail_url", "")
        video_id = creative.get("object_story_spec", {}).get("video_data", {}).get("video_id")
        afs = creative.get("asset_feed_spec", {})
        if not video_id and afs.get("videos"):
            video_id = afs["videos"][0].get("video_id")
            if not image:
                image = afs["videos"][0].get("thumbnail_url") or image
        if afs.get("images") and (creative.get("object_type") or "").upper() != "VIDEO":
            image = afs["images"][0].get("url") or image
        story_id = creative.get("effective_object_story_id")

        manual = load_manual_image(prefix, i)
        if manual:
            image = manual
        elif video_id:
            better = fetch_video_thumbnail(video_id, token)
            if better:
                image = better
        elif story_id:
            better = fetch_boosted_post_media(story_id, token)
            if better:
                image = better

        ad["name"] = (creative.get("body") or ad["name"])[:90]
        ad["format"] = (creative.get("object_type") or "ANUNCIO").upper()
        ad["thumbnail_url"] = download_and_embed_image(image)
        ad["reach"] = f"{ad['reach']:,}"
        ad["spend"] = f"${ad['spend']:,.2f} MXN"
        ad["whatsapp"] = f"{ad['whatsapp']:,}"
        ad["leads"] = f"{ad['leads']:,}"
        return ad

    top_whatsapp = [enrich_with_creative(ad, "wa", i) for i, ad in enumerate(candidatos_wa, start=1)]
    top_leads = [enrich_with_creative(ad, "leads", i) for i, ad in enumerate(candidatos_leads, start=1)]

    print("  Top Whatsapps:", ", ".join(f"{a['name'][:30]} ({a['whatsapp']})" for a in top_whatsapp) or "(ninguno)")
    print("  Top Formularios:", ", ".join(f"{a['name'][:30]} ({a['leads']})" for a in top_leads) or "(ninguno)")

    return {"whatsapp": top_whatsapp, "leads": top_leads}


def build_insights(current, previous, top_ads=None):
    insights = []
    r_prev, r_cur = previous["reach"], current["reach"]
    s_prev, s_cur = previous["spend"], current["spend"]
    leads_prev = previous["whatsapp"] + previous["leads"]
    leads_cur = current["whatsapp"] + current["leads"]

    if r_prev:
        pct = round((r_cur - r_prev) / r_prev * 100, 1)
        direction = "subió" if pct >= 0 else "cayó"
        insights.append(f"El alcance {direction} {abs(pct)}% respecto al periodo anterior ({r_prev:,} → {r_cur:,}).")
    elif r_cur:
        insights.append(f"Se alcanzaron {r_cur:,} personas en este periodo (no hay periodo anterior comparable).")

    if leads_prev:
        pct = round((leads_cur - leads_prev) / leads_prev * 100, 1)
        direction = "aumentaron" if pct >= 0 else "cayeron"
        insights.append(f"Los resultados totales (whatsapp + formularios) {direction} {abs(pct)}% respecto al periodo anterior ({leads_prev} → {leads_cur}).")
    elif leads_cur:
        insights.append(f"Se generaron {leads_cur} resultados totales (whatsapp + formularios) en este periodo.")

    if leads_cur and s_cur:
        cpl_cur = s_cur / leads_cur
        line = f"El costo promedio por resultado (whatsapp o formulario) fue de ${cpl_cur:,.2f} MXN"
        if leads_prev and s_prev:
            cpl_prev = s_prev / leads_prev
            pct = round((cpl_cur - cpl_prev) / cpl_prev * 100, 1)
            direction = "más caro" if pct >= 0 else "más barato"
            line += f", {abs(pct)}% {direction} que el periodo anterior (${cpl_prev:,.2f} MXN)."
        else:
            line += "."
        insights.append(line)

    if leads_cur:
        pct_wa = round(current["whatsapp"] / leads_cur * 100)
        pct_leads = 100 - pct_wa
        canal = "Whatsapp" if pct_wa >= pct_leads else "los formularios"
        insights.append(f"{pct_wa}% de los resultados vinieron de Whatsapp y {pct_leads}% de formularios -- el canal dominante este periodo fue {canal}.")

    if top_ads:
        wa_winner = top_ads.get("whatsapp") or []
        leads_winner = top_ads.get("leads") or []
        if wa_winner:
            insights.append(f'El anuncio con más conversaciones de Whatsapp fue "{wa_winner[0]["name"][:60]}" ({wa_winner[0]["whatsapp"]} conversaciones).')
        if leads_winner:
            insights.append(f'El anuncio con más formularios fue "{leads_winner[0]["name"][:60]}" ({leads_winner[0]["leads"]} formularios).')

    return insights or ["Sin cambios significativos respecto al periodo anterior."]


def build_archive_index(archive_dir, client_name, agency_name, main_filename):
    meses_es = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    archivos = sorted(archive_dir.glob("*.html"), reverse=True)
    archivos = [f for f in archivos if f.name != "index.html"]

    filas = []
    for f in archivos:
        slug = f.stem
        try:
            year, month = slug.split("-")
            label = f"{meses_es[int(month) - 1].capitalize()} {year}"
        except (ValueError, IndexError):
            label = slug
        filas.append(f'<li><a href="{f.name}">{label}</a></li>')

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Historial de reportes — {client_name}</title>
<style>
  body{{font-family:Calibri,Arial,sans-serif;background:#FAFAF8;color:#1B2A3B;max-width:600px;margin:60px auto;padding:0 20px;}}
  h1{{font-size:22px;}}
  ul{{list-style:none;padding:0;}}
  li{{margin-bottom:10px;}}
  a{{color:#E15241;font-size:16px;text-decoration:none;}}
  a:hover{{text-decoration:underline;}}
  .back{{margin-top:30px;display:inline-block;font-size:13px;color:#63707D;}}
</style>
</head>
<body>
  <h1>Historial de reportes — {client_name}</h1>
  <ul>
    {"".join(filas) if filas else "<li>Aun no hay reportes archivados.</li>"}
  </ul>
  <a class="back" href="../{main_filename}">← Volver al reporte mas reciente</a>
</body>
</html>"""
    (archive_dir / "index.html").write_text(html, encoding="utf-8")


def main():
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        print("ERROR: no encontre config.json.")
        sys.exit(1)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    token = get_access_token()
    account_id = config["account_id"]
    since = config["since"]
    until = config["until"]
    if str(until).strip().lower() == "today":
        until = date.today().strftime("%Y-%m-%d")

    print(f"Jalando datos de {account_id}...")
    monthly = fetch_monthly_insights(account_id, token, since, until)

    until_date = datetime.strptime(until, "%Y-%m-%d")
    meses_es = ["enero","febrero","marzo","abril","mayo","junio","julio",
                "agosto","septiembre","octubre","noviembre","diciembre"]

    period_days = config.get("period_days")
    if period_days:
        kpi_month_start = (until_date - timedelta(days=period_days - 1)).strftime("%Y-%m-%d")
        kpi_since_date = datetime.strptime(kpi_month_start, "%Y-%m-%d")
        if kpi_since_date.month == until_date.month and kpi_since_date.year == until_date.year:
            kpi_period_label = (
                f"{kpi_since_date.day} al {until_date.day} de "
                f"{meses_es[until_date.month - 1]} {until_date.year}"
            )
        else:
            same_year = kpi_since_date.year == until_date.year
            start_txt = f"{kpi_since_date.day} de {meses_es[kpi_since_date.month - 1]}"
            if not same_year:
                start_txt += f" {kpi_since_date.year}"
            end_txt = f"{until_date.day} de {meses_es[until_date.month - 1]} {until_date.year}"
            kpi_period_label = f"{start_txt} al {end_txt}"
        range_data = fetch_range_insights(account_id, token, kpi_month_start, until)
        last_reach = range_data["reach"]
        last_spend = range_data["spend"]
        last_freq = range_data["freq"]
        last_wa = range_data["whatsapp"]
        last_leads = range_data["leads"]
        kpi_until = until
        if not config.get("top_ads_per_campaign"):
            top_ads = fetch_top_ads(
                account_id, token,
                since=kpi_month_start, until=until,
                attribution_windows=config.get("attribution_windows"),
            )
        else:
            top_ads = None
    else:
        first_of_this_month = until_date.replace(day=1)
        last_month_end_date = first_of_this_month - timedelta(days=1)
        kpi_month_start = last_month_end_date.replace(day=1).strftime("%Y-%m-%d")
        kpi_until = last_month_end_date.strftime("%Y-%m-%d")
        kpi_period_label = f"{meses_es[last_month_end_date.month - 1]} {last_month_end_date.year}"

        range_data = fetch_range_insights(account_id, token, kpi_month_start, kpi_until)
        last_reach = range_data["reach"]
        last_spend = range_data["spend"]
        last_freq = range_data["freq"]
        last_wa = range_data["whatsapp"]
        last_leads = range_data["leads"]
        if not config.get("top_ads_per_campaign"):
            top_ads = fetch_top_ads(
                account_id, token,
                since=kpi_month_start, until=kpi_until,
                attribution_windows=config.get("attribution_windows"),
            )
        else:
            top_ads = None

    spend_wa, spend_leads = fetch_campaign_cost_breakdown(
        account_id, token, kpi_month_start, kpi_until
    )
    cost_per_wa = (spend_wa / last_wa) if last_wa else 0
    cost_per_lead = (spend_leads / last_leads) if last_leads else 0

    kpi_since_date = datetime.strptime(kpi_month_start, "%Y-%m-%d")
    kpi_until_date = datetime.strptime(kpi_until, "%Y-%m-%d")
    period_len_days = (kpi_until_date - kpi_since_date).days + 1
    prev_until_date = kpi_since_date - timedelta(days=1)
    prev_since_date = prev_until_date - timedelta(days=period_len_days - 1)
    previous_data = fetch_range_insights(
        account_id, token,
        prev_since_date.strftime("%Y-%m-%d"),
        prev_until_date.strftime("%Y-%m-%d"),
    )
    current_data = {"reach": last_reach, "spend": last_spend, "whatsapp": last_wa, "leads": last_leads}
    insights = build_insights(current_data, previous_data, top_ads=top_ads)

    campaigns_breakdown = None
    if config.get("show_campaign_breakdown"):
        campaigns_breakdown = fetch_campaigns_breakdown(account_id, token, kpi_month_start, kpi_until)

    top_ads_by_campaign = None
    if config.get("top_ads_per_campaign"):
        top_n_campaign = config.get("top_ads_per_campaign_count", 3)
        top_ads_by_campaign = fetch_top_ad_per_campaign(
            account_id, token, since=kpi_month_start, until=kpi_until,
            attribution_windows=config.get("attribution_windows"),
            top_n=top_n_campaign,
        )

    template_path = Path(__file__).parent / "template.html"
    template = Template(template_path.read_text(encoding="utf-8"))

    archive_url = "archivo/index.html" if config.get("enable_history", True) else None

    html = template.render(
        agency_name=config.get("agency_name", "Quattro Marketing"),
        client_name=config["client_name"],
        period_label=config.get("period_label", until),
        kpi_period_label=kpi_period_label,
        kpi_reach=f"{last_reach:,}",
        kpi_spend=f"${last_spend:,.2f} MXN",
        kpi_whatsapp=f"{last_wa:,}",
        kpi_leads=f"{last_leads:,}",
        kpi_frequency=f"{last_freq:.1f}",
        kpi_cost_per_wa=f"${cost_per_wa:,.2f}" if last_wa else "N/A",
        kpi_cost_per_lead=f"${cost_per_lead:,.2f}" if last_leads else "N/A",
        alert_tag=None,
        alert_text=None,
        top_ads_whatsapp=(top_ads["whatsapp"] if top_ads else []),
        top_ads_leads=(top_ads["leads"] if top_ads else []),
        top_ads_by_campaign=top_ads_by_campaign,
        campaigns_breakdown=campaigns_breakdown,
        insights=insights,
        archive_url=archive_url,
        months_json=json.dumps(monthly["months"]),
        reach_json=json.dumps(monthly["reach"]),
        cost_json=json.dumps(monthly["spend"]),
        freq_json=json.dumps(monthly["freq"]),
        whatsapp_json=json.dumps(monthly["whatsapp"]),
        leads_json=json.dumps(monthly["leads"]),
        generated_date=date.today().strftime("%d/%m/%Y"),
    )

    output_dir = Path(__file__).parent / config.get("output_dir", "output")
    output_dir.mkdir(exist_ok=True, parents=True)
    main_filename = config.get("output_filename", "dashboard.html")
    output_file = output_dir / main_filename
    output_file.write_text(html, encoding="utf-8")

    if archive_url:
        archive_dir = output_dir / "archivo"
        archive_dir.mkdir(exist_ok=True, parents=True)
        slug = kpi_month_start[:7]
        (archive_dir / f"{slug}.html").write_text(html, encoding="utf-8")
        build_archive_index(archive_dir, config["client_name"], config.get("agency_name", ""), main_filename)

    print(f"Listo! Dashboard generado en: {output_file}")


if __name__ == "__main__":
    main()
