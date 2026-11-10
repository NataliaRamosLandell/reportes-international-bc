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
            print(f"ERROR de la API de Meta ({resp.status_code}) en {url}: {resp.text[:300]}")
            resp.raise_for_status()
        payload = resp.json()
        rows.extend(payload.get("data", []))
        next_url = payload.get("paging", {}).get("next")
        next_params = None
        page += 1
        if page > max_pages:
            break
    return rows


def count_actions(actions):
    """Usa solo el action_type canonico de cada metrica (Meta reporta los
    leads bajo varios nombres y si se suman todos se duplican)."""
    wa, leads = 0, 0
    for a in actions or []:
        t = a.get("action_type", "")
        if t == WA_ACTION:
            wa = int(float(a.get("value", 0)))
        if t == "lead":
            leads = int(float(a.get("value", 0)))
    return wa, leads


CALL_PRIMARY = "click_to_call_native_call_placed"
CALL_FALLBACK = "click_to_call_call_confirm"


def count_calls(actions):
    """Llamadas realizadas (campañas con objetivo de llamadas)."""
    primary, fallback = 0, 0
    for a in actions or []:
        t = a.get("action_type", "")
        if t == CALL_PRIMARY:
            primary = int(float(a.get("value", 0)))
        elif t == CALL_FALLBACK:
            fallback = int(float(a.get("value", 0)))
    return primary or fallback


def objective_from_name(name):
    """Objetivo de la campaña segun su nombre (None si el nombre no lo dice)."""
    n = (name or "").lower()
    if "reconocimiento" in n or "awareness" in n:
        return "reconocimiento"
    if "llamada" in n:
        return "llamadas"
    if "whatsapp" in n:
        return "whatsapp"
    if "formulario" in n or "lead" in n:
        return "formulario"
    return None


_diag_seen = set()


def diag_calls(name, actions):
    """Deja en el log las acciones de las campañas de llamadas, para poder
    verificar que se esta leyendo el tipo de accion correcto."""
    if "llamada" in (name or "").lower() and name not in _diag_seen:
        _diag_seen.add(name)
        found = {a.get("action_type"): a.get("value") for a in (actions or [])
                 if "call" in a.get("action_type", "")
                 or "messaging_conversation_started" in a.get("action_type", "")}
        print(f"  [DIAG llamadas] {name}: {found}")


def allocate_results(name, actions, spend):
    """Asigna los resultados y el gasto de UNA campaña a un solo canal segun
    su objetivo (como la columna "Resultados" de Ads Manager). Si el nombre
    no dice el objetivo, se reparte proporcional a lo que genero.
    Regresa (whatsapp, leads, llamadas, gasto_wa, gasto_leads, gasto_llamadas)."""
    wa, leads = count_actions(actions)
    calls = count_calls(actions)
    obj = objective_from_name(name)
    if obj == "reconocimiento":
        return 0, 0, 0, 0.0, 0.0, 0.0
    if obj == "llamadas":
        return 0, 0, calls, 0.0, 0.0, (spend if calls else 0.0)
    if obj == "whatsapp":
        return wa, 0, 0, (spend if wa else 0.0), 0.0, 0.0
    if obj == "formulario":
        return 0, leads, 0, 0.0, (spend if leads else 0.0), 0.0
    total = wa + leads + calls
    if total == 0:
        return 0, 0, 0, 0.0, 0.0, 0.0
    return (wa, leads, calls,
            spend * wa / total, spend * leads / total, spend * calls / total)


def fetch_range_insights(account_ids, token, since, until):
    """KPIs de un rango exacto, sumando todas las cuentas. Alcance, gasto y
    frecuencia salen del total de la cuenta; los resultados (whatsapps,
    formularios, llamadas) y su costo salen campaña por campaña segun su
    objetivo. El alcance entre cuentas es una suma aproximada."""
    tot = {"reach": 0, "spend": 0.0, "impressions": 0,
           "wa": 0, "leads": 0, "calls": 0,
           "sp_wa": 0.0, "sp_leads": 0.0, "sp_calls": 0.0}
    for account_id in as_list(account_ids):
        url = f"{GRAPH_API_BASE}/{account_id}/insights"
        rows = graph_get_all(url, {
            "fields": "reach,spend,impressions,frequency",
            "time_range": json.dumps({"since": since, "until": until}),
            "access_token": token,
        })
        if rows:
            row = rows[0]
            tot["reach"] += int(float(row.get("reach", 0)))
            tot["spend"] += float(row.get("spend", 0))
            tot["impressions"] += int(float(row.get("impressions", 0)))

        camp_rows = graph_get_all(url, {
            "level": "campaign",
            "fields": "campaign_name,spend,actions",
            "time_range": json.dumps({"since": since, "until": until}),
            "limit": 200,
            "access_token": token,
        })
        for row in camp_rows:
            name = row.get("campaign_name", "")
            diag_calls(name, row.get("actions"))
            w, l, c, sw, sl, sc = allocate_results(name, row.get("actions"), float(row.get("spend", 0)))
            tot["wa"] += w
            tot["leads"] += l
            tot["calls"] += c
            tot["sp_wa"] += sw
            tot["sp_leads"] += sl
            tot["sp_calls"] += sc

    freq = (tot["impressions"] / tot["reach"]) if tot["reach"] else 0.0
    return {
        "reach": tot["reach"],
        "spend": round(tot["spend"], 2),
        "freq": round(freq, 1),
        "whatsapp": tot["wa"],
        "leads": tot["leads"],
        "calls": tot["calls"],
        "spend_wa": tot["sp_wa"],
        "spend_leads": tot["sp_leads"],
        "spend_calls": tot["sp_calls"],
    }


def classify_objective(name, wa_count, lead_count, reach, call_count=0):
    name_lower = name.lower()
    if "reconocimiento" in name_lower or "awareness" in name_lower:
        return "Reconocimiento", reach, True
    if "llamada" in name_lower:
        return "Llamadas", call_count, False
    if "whatsapp" in name_lower:
        return "Whatsapp", wa_count, False
    if "formulario" in name_lower or "lead" in name_lower:
        return "Formulario", lead_count, False
    if call_count > max(wa_count, lead_count):
        return "Llamadas", call_count, False
    if lead_count > wa_count:
        return "Formulario", lead_count, False
    if wa_count > 0:
        return "Whatsapp", wa_count, False
    return "Otro", reach, True


def fetch_campaigns_breakdown(account_ids, token, since, until):
    """KPI por campaña. Si una campaña tiene el mismo nombre en las dos
    cuentas, se muestra como una sola fila (suma)."""
    merged = {}
    for account_id in as_list(account_ids):
        url = f"{GRAPH_API_BASE}/{account_id}/insights"
        params = {
            "level": "campaign",
            "fields": "campaign_name,reach,spend,impressions,actions",
            "time_range": json.dumps({"since": since, "until": until}),
            "limit": 200,
            "access_token": token,
        }
        for row in graph_get_all(url, params):
            name = row.get("campaign_name", "")
            m = merged.setdefault(name, {"reach": 0, "spend": 0.0, "impr": 0, "wa": 0, "leads": 0, "calls": 0})
            wa, leads = count_actions(row.get("actions"))
            m["reach"] += int(float(row.get("reach", 0)))
            m["spend"] += float(row.get("spend", 0))
            m["impr"] += int(float(row.get("impressions", 0)))
            m["wa"] += wa
            m["leads"] += leads
            m["calls"] += count_calls(row.get("actions"))

    campaigns = []
    for name, m in merged.items():
        reach = m["reach"]
        spend = round(m["spend"], 2)
        freq = round(m["impr"] / reach, 1) if reach else 0.0
        objetivo, resultado, es_awareness = classify_objective(name, m["wa"], m["leads"], reach, m["calls"])
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


def fetch_monthly_insights(account_ids, token, since, until):
    """Historico mes a mes, sumando todas las cuentas. Alcance, gasto y
    frecuencia del total de la cuenta; resultados por objetivo de campaña."""
    by_month = {}

    def month_bucket(key):
        return by_month.setdefault(
            key, {"reach": 0, "spend": 0.0, "impr": 0, "wa": 0, "leads": 0, "calls": 0})

    for account_id in as_list(account_ids):
        url = f"{GRAPH_API_BASE}/{account_id}/insights"
        time_range = json.dumps({"since": since, "until": until})
        for row in graph_get_all(url, {
            "fields": "reach,spend,impressions,frequency",
            "time_increment": "monthly",
            "time_range": time_range,
            "access_token": token,
        }):
            m = month_bucket(row["date_start"])
            m["reach"] += int(float(row.get("reach", 0)))
            m["spend"] += float(row.get("spend", 0))
            m["impr"] += int(float(row.get("impressions", 0)))

        for row in graph_get_all(url, {
            "level": "campaign",
            "fields": "campaign_name,spend,actions",
            "time_increment": "monthly",
            "time_range": time_range,
            "limit": 500,
            "access_token": token,
        }):
            m = month_bucket(row["date_start"])
            w, l, c, _, _, _ = allocate_results(
                row.get("campaign_name", ""), row.get("actions"), float(row.get("spend", 0)))
            m["wa"] += w
            m["leads"] += l
            m["calls"] += c

    months, reach, spend, freq, whatsapp, leads, calls = [], [], [], [], [], [], []
    for key in sorted(by_month):
        m = by_month[key]
        months.append(datetime.strptime(key, "%Y-%m-%d").strftime("%b"))
        reach.append(m["reach"])
        spend.append(round(m["spend"], 2))
        freq.append(round(m["impr"] / m["reach"], 1) if m["reach"] else 0.0)
        whatsapp.append(m["wa"])
        leads.append(m["leads"])
        calls.append(m["calls"])

    return {"months": months, "reach": reach, "spend": spend,
            "freq": freq, "whatsapp": whatsapp, "leads": leads, "calls": calls}


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
    """Descarga la imagen y la incrusta en base64 para que no se rompa
    cuando el link de Meta expire."""
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


def fetch_ad_level_metrics(account_ids, token, date_preset=None, since=None, until=None,
                           attribution_windows=None):
    """Una fila por anuncio, juntando todas las cuentas."""
    rows = []
    for account_id in as_list(account_ids):
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
        rows.extend(graph_get_all(url, params))
    return rows


def fetch_current_campaign_names(token, campaign_ids):
    if not campaign_ids:
        return {}
    result = {}
    for chunk in chunked(campaign_ids, 50):
        url = f"{GRAPH_API_BASE}/"
        params = {
            "ids": ",".join(str(c) for c in chunk),
            "fields": "name",
            "access_token": token,
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        result.update({cid: info.get("name", cid) for cid, info in data.items()})
    return result


def fetch_ads_creative_info(account_ids, token, ad_ids):
    if not ad_ids:
        return {}
    result = {}
    for chunk in chunked(ad_ids, 50):
        url = f"{GRAPH_API_BASE}/"
        params = {
            "ids": ",".join(chunk),
            "fields": (
                "name,creative{id,thumbnail_url.width(1080).height(1350),body,"
                "object_type,image_url,object_story_spec,effective_object_story_id,"
                "asset_feed_spec{images{url},videos{video_id,thumbnail_url}}}"
            ),
            "access_token": token,
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        result.update(resp.json())
    return result


def enrich_ad(ad, creative_by_id, prefix, i, token):
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
    ad["calls"] = f"{ad.get('calls', 0):,}"
    return ad


def fetch_top_ad_per_campaign(account_ids, token, since=None, until=None,
                              date_preset="last_month", attribution_windows=None,
                              top_n=3):
    rows = fetch_ad_level_metrics(
        account_ids, token, date_preset=date_preset, since=since, until=until,
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
        wa_count, lead_count = count_actions(row.get("actions"))
        call_count = count_calls(row.get("actions"))

        ct = campaign_totals.setdefault(
            campaign_id, {"reach": 0, "spend": 0.0, "whatsapp": 0, "leads": 0, "calls": 0, "nombres": {}}
        )
        ct["reach"] += reach
        ct["spend"] += spend
        ct["whatsapp"] += wa_count
        ct["leads"] += lead_count
        ct["calls"] += call_count
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
                "calls": 0,
                "best_ad_id": row.get("ad_id"),
                "best_reach": -1,
            }
        g = grouped[key]
        g["reach"] += reach
        g["spend"] += spend
        g["whatsapp"] += wa_count
        g["leads"] += lead_count
        g["calls"] += call_count
        if reach > g["best_reach"]:
            g["best_reach"] = reach
            g["best_ad_id"] = row.get("ad_id")

    campaign_display_names = fetch_current_campaign_names(token, list(campaign_totals.keys()))

    winners_by_campaign = {}
    for campaign_id, totals in campaign_totals.items():
        current_name = campaign_display_names.get(campaign_id) or max(totals["nombres"], key=totals["nombres"].get)

        objetivo, _, es_awareness = classify_objective(
            current_name, totals["whatsapp"], totals["leads"], totals["reach"], totals["calls"]
        )
        if es_awareness:
            metric_key = "reach"
        elif objetivo == "Whatsapp":
            metric_key = "whatsapp"
        elif objetivo == "Llamadas":
            metric_key = "calls"
        else:
            metric_key = "leads"

        ads_in_campaign = [dict(a) for a in grouped.values() if a["campaign_id"] == campaign_id]
        ads_in_campaign.sort(key=lambda a: a[metric_key], reverse=True)
        top = ads_in_campaign[:top_n]
        for ad in top:
            ad["objetivo"] = objetivo
            ad["metric_key"] = metric_key
        winners_by_campaign[campaign_id] = top

    all_winners = [ad for ads in winners_by_campaign.values() for ad in ads]
    winner_ids = list({a["best_ad_id"] for a in all_winners if a.get("best_ad_id")})
    creative_by_id = fetch_ads_creative_info(account_ids, token, winner_ids)

    result_by_theme = {}
    i = 1
    for campaign_id, ads in winners_by_campaign.items():
        campaign_name = campaign_display_names.get(campaign_id, str(campaign_id))
        enriched = [enrich_ad(ad, creative_by_id, "campaign", i + idx, token) for idx, ad in enumerate(ads)]
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


def fetch_top_ads(account_ids, token, date_preset="last_month", since=None, until=None,
                  limit=3, attribution_windows=None):
    rows = fetch_ad_level_metrics(
        account_ids, token, date_preset=date_preset, since=since, until=until,
        attribution_windows=attribution_windows,
    )

    grouped = {}
    for row in rows:
        reach = int(float(row.get("reach", 0)))
        spend = round(float(row.get("spend", 0)), 2)
        ad_name = row.get("ad_name", "")
        ad_id = row.get("ad_id")
        wa_count, lead_count = count_actions(row.get("actions"))

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
            }
        g = grouped[key]
        g["reach"] += reach
        g["spend"] += spend
        g["whatsapp"] += wa_count
        g["leads"] += lead_count
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
    creative_by_id = fetch_ads_creative_info(account_ids, token, winner_ids)

    top_whatsapp = [enrich_ad(ad, creative_by_id, "wa", i, token) for i, ad in enumerate(candidatos_wa, start=1)]
    top_leads = [enrich_ad(ad, creative_by_id, "leads", i, token) for i, ad in enumerate(candidatos_leads, start=1)]

    print("  Top Whatsapps:", ", ".join(f"{a['name'][:30]} ({a['whatsapp']})" for a in top_whatsapp) or "(ninguno)")
    print("  Top Formularios:", ", ".join(f"{a['name'][:30]} ({a['leads']})" for a in top_leads) or "(ninguno)")

    return {"whatsapp": top_whatsapp, "leads": top_leads}


def build_insights(current, previous, top_ads=None):
    insights = []
    r_prev, r_cur = previous["reach"], current["reach"]
    s_prev, s_cur = previous["spend"], current["spend"]
    leads_prev = previous["whatsapp"] + previous["leads"] + previous.get("calls", 0)
    leads_cur = current["whatsapp"] + current["leads"] + current.get("calls", 0)

    if r_prev:
        pct = round((r_cur - r_prev) / r_prev * 100, 1)
        direction = "subió" if pct >= 0 else "cayó"
        insights.append(f"El alcance {direction} {abs(pct)}% respecto a la semana anterior ({r_prev:,} → {r_cur:,}).")
    elif r_cur:
        insights.append(f"Se alcanzaron {r_cur:,} personas en esta semana (no hay semana anterior comparable).")

    if leads_prev:
        pct = round((leads_cur - leads_prev) / leads_prev * 100, 1)
        direction = "aumentaron" if pct >= 0 else "cayeron"
        insights.append(f"Los resultados totales (whatsapp + formularios + llamadas) {direction} {abs(pct)}% respecto a la semana anterior ({leads_prev} → {leads_cur}).")
    elif leads_cur:
        insights.append(f"Se generaron {leads_cur} resultados totales (whatsapp + formularios + llamadas) en esta semana.")

    if leads_cur and s_cur:
        cpl_cur = s_cur / leads_cur
        line = f"El costo promedio por resultado (whatsapp, formulario o llamada) fue de ${cpl_cur:,.2f} MXN"
        if leads_prev and s_prev:
            cpl_prev = s_prev / leads_prev
            pct = round((cpl_cur - cpl_prev) / cpl_prev * 100, 1)
            direction = "más caro" if pct >= 0 else "más barato"
            line += f", {abs(pct)}% {direction} que la semana anterior (${cpl_prev:,.2f} MXN)."
        else:
            line += "."
        insights.append(line)

    if leads_cur:
        canales = [("Whatsapp", current["whatsapp"]), ("formularios", current["leads"]),
                   ("llamadas", current.get("calls", 0))]
        canales = [(n, v) for n, v in canales if v > 0]
        partes = [f"{round(v / leads_cur * 100)}% de {n}" for n, v in canales]
        dominante = max(canales, key=lambda x: x[1])[0]
        insights.append("Los resultados se repartieron así: " + ", ".join(partes)
                        + f". El canal dominante esta semana fue {dominante}.")

    if top_ads:
        wa_winner = top_ads.get("whatsapp") or []
        leads_winner = top_ads.get("leads") or []
        if wa_winner:
            insights.append(f'El anuncio con más conversaciones de Whatsapp fue "{wa_winner[0]["name"][:60]}" ({wa_winner[0]["whatsapp"]} conversaciones).')
        if leads_winner:
            insights.append(f'El anuncio con más formularios fue "{leads_winner[0]["name"][:60]}" ({leads_winner[0]["leads"]} formularios).')

    return insights or ["Sin cambios significativos respecto a la semana anterior."]


def fix_archive_links(archive_dir):
    """Las copias guardadas en archivo/ viven una carpeta mas adentro que el
    reporte principal, asi que sus links relativos hay que ajustarlos.
    Es seguro correrla varias veces (no cambia lo que ya esta arreglado)."""
    for f in archive_dir.glob("*.html"):
        if f.name == "index.html" or f.name.startswith("_"):
            continue
        text = f.read_text(encoding="utf-8")
        fixed = (
            text.replace('href="archivo/index.html"', 'href="index.html"')
                .replace('href="international-bc-detalle.html"', 'href="../international-bc-detalle.html"')
        )
        if fixed != text:
            f.write_text(fixed, encoding="utf-8")


MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
            "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def week_label(start, end):
    """Texto de una semana: '2 al 8 de noviembre 2026' o
    '26 de octubre al 1 de noviembre 2026'."""
    if start.month == end.month and start.year == end.year:
        return f"{start.day} al {end.day} de {MESES_ES[end.month - 1]} {end.year}"
    start_txt = f"{start.day} de {MESES_ES[start.month - 1]}"
    if start.year != end.year:
        start_txt += f" {start.year}"
    return f"{start_txt} al {end.day} de {MESES_ES[end.month - 1]} {end.year}"


def last_full_week(today):
    """Ultima semana completa (lunes a domingo) antes de hoy."""
    last_sunday = today - timedelta(days=today.weekday() + 1)
    return last_sunday - timedelta(days=6), last_sunday


def build_archive_index(archive_dir, client_name, agency_name, main_filename):
    archivos = sorted(archive_dir.glob("*.html"), reverse=True)
    archivos = [f for f in archivos if f.name != "index.html" and not f.name.startswith("_")]

    semanales, mensuales = [], []
    for f in archivos:
        slug = f.stem
        partes = slug.split("-")
        try:
            if len(partes) == 3:
                start = datetime.strptime(slug, "%Y-%m-%d").date()
                label = "Semana del " + week_label(start, start + timedelta(days=6))
                semanales.append(f'<li><a href="{f.name}">{label}</a></li>')
                continue
            year, month = partes
            label = f"{MESES_ES[int(month) - 1].capitalize()} {year}"
        except (ValueError, IndexError):
            label = slug
        mensuales.append(f'<li><a href="{f.name}">{label}</a></li>')

    bloques = ""
    if semanales:
        bloques += f'<h2>Reportes semanales</h2><ul>{"".join(semanales)}</ul>'
    if mensuales:
        bloques += f'<h2>Reportes mensuales anteriores</h2><ul>{"".join(mensuales)}</ul>'
    if not bloques:
        bloques = "<ul><li>Aun no hay reportes archivados.</li></ul>"

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Historial de reportes — {client_name}</title>
<style>
  body{{font-family:Calibri,Arial,sans-serif;background:#FAFAF8;color:#1B2A3B;max-width:600px;margin:60px auto;padding:0 20px;}}
  h1{{font-size:22px;}}
  h2{{font-size:16px;color:#63707D;margin-top:28px;}}
  ul{{list-style:none;padding:0;}}
  li{{margin-bottom:10px;}}
  a{{color:#E15241;font-size:16px;text-decoration:none;}}
  a:hover{{text-decoration:underline;}}
  .back{{margin-top:30px;display:inline-block;font-size:13px;color:#63707D;}}
</style>
</head>
<body>
  <h1>Historial de reportes — {client_name}</h1>
  {bloques}
  <a class="back" href="../{main_filename}">← Volver al reporte mas reciente</a>
</body>
</html>"""
    (archive_dir / "index.html").write_text(html, encoding="utf-8")


def build_report(config, token, account_ids, since, chart_until,
                 kpi_start, kpi_until, kpi_period_label, write_main=True):
    """Genera el reporte de la semana kpi_start (lunes) -> kpi_until (domingo).
    Las graficas siguen siendo mensuales (desde 'since' hasta chart_until).
    write_main=False solo guarda la copia en archivo/."""
    monthly = fetch_monthly_insights(account_ids, token, since, chart_until)

    range_data = fetch_range_insights(account_ids, token, kpi_start, kpi_until)
    last_reach = range_data["reach"]
    last_spend = range_data["spend"]
    last_freq = range_data["freq"]
    last_wa = range_data["whatsapp"]
    last_leads = range_data["leads"]
    last_calls = range_data["calls"]

    top_ads = None
    if not config.get("top_ads_per_campaign"):
        top_ads = fetch_top_ads(
            account_ids, token,
            since=kpi_start, until=kpi_until,
            attribution_windows=config.get("attribution_windows"),
        )

    cost_per_wa = (range_data["spend_wa"] / last_wa) if last_wa else 0
    cost_per_lead = (range_data["spend_leads"] / last_leads) if last_leads else 0
    cost_per_call = (range_data["spend_calls"] / last_calls) if last_calls else 0

    kpi_since_date = datetime.strptime(kpi_start, "%Y-%m-%d")
    kpi_until_date = datetime.strptime(kpi_until, "%Y-%m-%d")
    period_len_days = (kpi_until_date - kpi_since_date).days + 1
    prev_until_date = kpi_since_date - timedelta(days=1)
    prev_since_date = prev_until_date - timedelta(days=period_len_days - 1)
    previous_data = fetch_range_insights(
        account_ids, token,
        prev_since_date.strftime("%Y-%m-%d"),
        prev_until_date.strftime("%Y-%m-%d"),
    )
    current_data = {"reach": last_reach, "spend": last_spend, "whatsapp": last_wa, "leads": last_leads, "calls": last_calls}
    insights = build_insights(current_data, previous_data, top_ads=top_ads)

    campaigns_breakdown = None
    if config.get("show_campaign_breakdown"):
        campaigns_breakdown = fetch_campaigns_breakdown(account_ids, token, kpi_start, kpi_until)

    top_ads_by_campaign = None
    if config.get("top_ads_per_campaign"):
        top_n_campaign = config.get("top_ads_per_campaign_count", 3)
        top_ads_by_campaign = fetch_top_ad_per_campaign(
            account_ids, token, since=kpi_start, until=kpi_until,
            attribution_windows=config.get("attribution_windows"),
            top_n=top_n_campaign,
        )

    template_path = Path(__file__).parent / "template.html"
    template = Template(template_path.read_text(encoding="utf-8"))

    archive_url = "archivo/index.html" if config.get("enable_history", True) else None

    html = template.render(
        agency_name=config.get("agency_name", "Quattro Marketing"),
        client_name=config["client_name"],
        period_label=config.get("period_label", chart_until),
        kpi_period_label=kpi_period_label,
        kpi_reach=f"{last_reach:,}",
        kpi_spend=f"${last_spend:,.2f} MXN",
        kpi_whatsapp=f"{last_wa:,}",
        kpi_leads=f"{last_leads:,}",
        kpi_frequency=f"{last_freq:.1f}",
        kpi_cost_per_wa=f"${cost_per_wa:,.2f}" if last_wa else "N/A",
        kpi_cost_per_lead=f"${cost_per_lead:,.2f}" if last_leads else "N/A",
        kpi_calls=f"{last_calls:,}",
        kpi_cost_per_call=f"${cost_per_call:,.2f}" if last_calls else "N/A",
        show_calls=last_calls > 0,
        show_calls_chart=any(monthly["calls"]),
        calls_json=json.dumps(monthly["calls"]),
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
        whatsapp_json=json.dumps(monthly["whatsapp"]),
        leads_json=json.dumps(monthly["leads"]),
        generated_date=date.today().strftime("%d/%m/%Y"),
    )

    output_dir = Path(__file__).parent / config.get("output_dir", "output")
    output_dir.mkdir(exist_ok=True, parents=True)
    main_filename = config.get("output_filename", "dashboard.html")
    if write_main:
        (output_dir / main_filename).write_text(html, encoding="utf-8")
        print(f"Listo! Dashboard generado en: {output_dir / main_filename}")

    if archive_url:
        archive_dir = output_dir / "archivo"
        archive_dir.mkdir(exist_ok=True, parents=True)
        slug = kpi_start  # lunes de la semana, ej. 2026-11-02
        (archive_dir / f"{slug}.html").write_text(html, encoding="utf-8")
        fix_archive_links(archive_dir)
        build_archive_index(archive_dir, config["client_name"], config.get("agency_name", ""), main_filename)


def main():
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        print("ERROR: no encontre config.json.")
        sys.exit(1)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    token = get_access_token()
    account_ids = get_account_ids(config)
    since = config["since"]
    until = config["until"]
    if str(until).strip().lower() == "today":
        until = date.today().strftime("%Y-%m-%d")
    until_date = datetime.strptime(until, "%Y-%m-%d").date()

    # Resumen semanal: ultima semana completa (lunes a domingo).
    # Si corre el lunes, es la semana que termino ayer domingo.
    week_start, week_end = last_full_week(until_date)
    kpi_start = week_start.strftime("%Y-%m-%d")
    kpi_until = week_end.strftime("%Y-%m-%d")
    label = week_label(week_start, week_end)

    print(f"Jalando datos de {', '.join(account_ids)}...")
    print(f"Semana del reporte: {label} ({kpi_start} a {kpi_until})")

    # Las graficas mensuales van desde 'since' hasta hoy.
    build_report(config, token, account_ids, since, until, kpi_start, kpi_until, label, write_main=True)


if __name__ == "__main__":
    main()
