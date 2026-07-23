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
    """Calcula el costo real por whatsapp y por formulario, usando SOLO el
    gasto de las campañas que efectivamente generaron cada resultado --
    no el gasto total de la cuenta (que incluye campañas de awareness,
    trafico, etc. que no generan whatsapps ni leads).

    Si una campaña genero AMBOS tipos de resultado (raro, pero posible),
    su gasto se reparte proporcionalmente entre los dos segun cuantos
    resultados de cada tipo produjo.
    """
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
            continue  # campaña de awareness/trafico -- no cuenta para ninguno
        spend_wa_total += spend * (wa_count / total)
        spend_leads_total += spend * (lead_count / total)

    return spend_wa_total, spend_leads_total


def fetch_range_insights(account_id, token, since, until):
    """Trae reach, spend, frequency, whatsapp y leads para UN rango de
    fechas exacto (sin desglosar por mes) -- se usa para el resumen de
    "Resumen ejecutivo" cuando el cliente quiere periodos que no son mes
    calendario completo (ej. reportes quincenales)."""
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
    """Clasifica el objetivo de una campana (o anuncio) por palabras clave
    en su nombre -- reutilizado tanto por el desglose por campana como
    por el top de anuncios por campana."""
    name_lower = name.lower()
    if "reconocimiento" in name_lower or "awareness" in name_lower:
        return "Reconocimiento", reach, True
    if "whatsapp" in name_lower:
        return "Whatsapp", wa_count, False
    if "formulario" in name_lower or "lead" in name_lower:
        return "Formulario", lead_count, False
    # objetivo no identificado por el nombre -- usamos el que mas
    # resultados haya dado como mejor suposicion
    if lead_count > wa_count:
        return "Formulario", lead_count, False
    if wa_count > 0:
        return "Whatsapp", wa_count, False
    return "Otro", reach, True


def fetch_campaigns_breakdown(account_id, token, since, until):
    """Trae reach/spend/whatsapp/leads POR CAMPAÑA (no agregado a nivel
    cuenta) -- para clientes con muchas campañas de objetivos distintos
    donde interesa ver el KPI de cada una por separado, no solo un top 3
    general. Clasifica el objetivo de cada campaña por palabras clave en
    su nombre (whatsapp / formulario / reconocimiento)."""
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
    """Trae reach, spend, clicks, frequency y acciones (whatsapp/leads) por mes."""
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
            # OJO: Meta reporta el mismo conteo de leads bajo varios nombres
            # distintos (lead, onsite_conversion.lead_grouped,
            # offsite_complete_registration_add_meta_leads, etc). Si sumamos
            # todos los que contienen "lead" se duplica el conteo real.
            # Usamos solo el action_type canonico de cada metrica:
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
    """Los anuncios de video tienen su propio set de miniaturas por
    resolucion (distinto al thumbnail_url generico del creative). Aqui
    pedimos la lista y regresamos la de mayor ancho disponible."""
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
    """Cuando el anuncio impulsa una publicacion existente de la pagina
    (en vez de crear un creativo nuevo), ni object_story_spec ni
    asset_feed_spec traen el video/imagen -- hay que pedirselo directo
    al post original."""
    url = f"{GRAPH_API_BASE}/{post_id}"
    params = {
        "fields": "full_picture,attachments{media_type,media,url}",
        "access_token": token,
    }
    resp = requests.get(url, params=params, timeout=30)
    if not resp.ok:
        if os.environ.get("DEBUG_REPORTES"):
            print(f"[DEBUG] fetch_boosted_post_media fallo para post_id={post_id}: {resp.status_code} {resp.text[:200]}")
        return None

    data = resp.json()
    # attachments suele traer mejor resolucion que full_picture cuando existe
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
    """Descarga la imagen desde la URL de Meta y la convierte a base64
    incrustada directamente en el HTML.

    IMPORTANTE: los links de imagenes que da la Meta API (scontent-...
    fbcdn.net) tienen una fecha de vencimiento integrada (el parametro
    'oe' en la URL) -- despues de unas semanas, esos links dejan de
    funcionar y la imagen se ve como un cuadro gris/roto. Al descargar la
    imagen UNA VEZ (al momento de generar el reporte) y guardarla como
    base64 dentro del propio HTML, la imagen queda permanente sin
    importar cuanto tiempo pase."""
    if not url or url.startswith("data:"):
        return url  # ya es una imagen manual incrustada, no hay que hacer nada
    try:
        resp = requests.get(url, timeout=20)
        if not resp.ok:
            return url  # si falla, devolvemos la URL original como respaldo
        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        data = base64.b64encode(resp.content).decode("ascii")
        return f"data:{content_type};base64,{data}"
    except requests.RequestException:
        return url  # si falla la descarga, mejor la URL original que nada


def load_manual_image(prefix, index):
    """Si el usuario puso una imagen manual en
    images_manual/{prefix}_{index}.jpg (o .png/.jpeg), la usamos en vez de
    la que trajo la API -- para garantizar buena calidad en anuncios que
    la API no puede traer nitidos. prefix es 'wa' o 'leads' segun el
    ranking (ej. images_manual/wa_1.jpg, images_manual/leads_2.png)."""
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
    """Trae reach/spend/whatsapp/leads POR ANUNCIO usando el endpoint
    /insights?level=ad -- el MISMO endpoint y parametros que se usan para
    validar manualmente en el Graph API Explorer. Usamos este en vez de
    anidar insights dentro de /ads porque encontramos que ese segundo
    camino puede dar numeros ligeramente distintos a los que se ven en
    Ads Manager / Explorer."""
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
    """Consulta el nombre ACTUAL y real de cada campana directamente a
    Meta (no una suposicion basada en gasto historico). Es la unica forma
    confiable de saber el nombre correcto cuando una campana se renombro
    a medio periodo -- los insights historicos pueden seguir mostrando el
    nombre viejo para los dias anteriores al cambio."""
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
    """Para clientes con muchas campanas de objetivos distintos, arma el
    top de anuncios POR CAMPAÑA (no un ranking global de toda la cuenta)
    -- asi cada campana (Seminuevos, Formularios, Reconocimiento, etc.)
    muestra su propio mejor anuncio, en vez de que una sola campana con
    mucho presupuesto se lleve todo el ranking general.

    Rankea cada campana por la metrica de SU objetivo (whatsapp si es de
    whatsapp, leads si es de formulario, alcance si es de reconocimiento).
    """
    rows = fetch_ad_level_metrics(
        account_id, token, date_preset=date_preset, since=since, until=until,
        attribution_windows=attribution_windows,
    )

    # Agrupamos por (ID de campaña, nombre de anuncio sin sufijo "-Copia").
    # OJO: usamos el ID de la campaña, NO su nombre, como clave principal
    # -- si renombras una campaña a medio periodo, Meta puede seguir
    # reportando el nombre viejo para parte de los datos historicos,
    # aunque sea la misma campaña. El ID nunca cambia.
    grouped = {}
    campaign_totals = {}  # campaign_id -> {reach, spend, wa, leads, nombres:{nombre: spend}}
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
        # llevamos la cuenta de cuanto gasto acumula cada nombre visto
        # para esta campaña -- el nombre con mas gasto es el "actual"
        # (el viejo, de antes de un renombre, deja de acumular gasto)
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

    # Para cada campana, clasificamos su objetivo (por su nombre ACTUAL --
    # el que mas gasto acumula) usando el total acumulado de la campana,
    # y escogemos el/los anuncio(s) ganador(es) segun esa metrica.
    # Consultamos el nombre ACTUAL real de cada campaña directamente a
    # Meta -- no adivinamos por gasto historico, porque si la campaña se
    # renombro hace poco, la mayoria del gasto del periodo puede seguir
    # estando bajo el nombre viejo (eso rompia la logica anterior).
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

    # Pedimos el creativo (thumbnail/texto) solo para los anuncios ganadores
    all_winners = [ad for ads in winners_by_campaign.values() for ad in ads]
    winner_ids = list({a["best_ad_id"] for a in all_winners if a.get("best_ad_id")})
    creative_by_id = fetch_ads_creative_info(account_id, token, winner_ids)

    def enrich(ad, prefix, i):
        info = creative_by_id.get(ad["best_ad_id"], {})
        creative = info.get("creative", {})
        if os.environ.get("DEBUG_REPORTES"):
            print(f"[DEBUG] creative crudo para {ad['name'][:30]!r}: "
                  f"image_url={creative.get('image_url')!r} | "
                  f"thumbnail_url={(creative.get('thumbnail_url') or '')[:80]!r} | "
                  f"object_type={creative.get('object_type')!r} | "
                  f"story_id={creative.get('effective_object_story_id')!r}")
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
            # Intentamos el post original PRIMERO -- para muchos anuncios
            # modernos (incluso de formato PHOTO), la imagen de mejor
            # calidad vive en la publicacion original, no en el campo
            # clasico "image_url"/"thumbnail_url" del creative, que a
            # veces trae una version mas chica o comprimida.
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
        # Agrupamos por "tema" -- el nombre de la campaña sin la palabra
        # de objetivo (whatsapp/formulario/reconocimiento) -- asi
        # "Hombre Camion Whatsapp" y "Hombre Camion Formulario" quedan
        # juntos bajo "Hombre Camion" en vez de secciones separadas que
        # desperdician espacio horizontal.
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
    """Trae SOLO la info de creativo (thumbnail, texto, formato) para una
    lista puntual de ad_ids -- se usa solo para los pocos anuncios que
    ganaron el ranking, no para toda la cuenta."""
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
    """Trae los anuncios y arma DOS rankings separados: top por whatsapps
    generados y top por formularios generados (nunca se combinan, porque
    suelen ser objetivos de campanas distintas).

    Antes de rankear, agrupa por el NOMBRE del anuncio (sin sufijo
    "- Copia N") -- es comun que el MISMO anuncio corra duplicado en
    varias campanas (ej. una por ciudad), y si no se agrupan, cada copia
    cuenta por separado y se subestima el rendimiento real del anuncio.

    Se puede filtrar por date_preset (ej. "last_month") O por un rango
    personalizado pasando since/until (ej. para reportes quincenales).

    attribution_windows: lista opcional como ["7d_click","1d_view"] para
    forzar la misma ventana de atribucion que usa Ads Manager, si los
    numeros no cuadran contra la UI (ver README para como diagnosticarlo).
    """
    rows = fetch_ad_level_metrics(
        account_id, token, date_preset=date_preset, since=since, until=until,
        attribution_windows=attribution_windows,
    )

    grouped = {}  # key = nombre normalizado -> datos acumulados
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

        # OJO: agrupamos por el nombre del anuncio SIN el sufijo
        # "- Copia N" -- es la convencion que usa esta cuenta para
        # duplicar el mismo anuncio en varias campanas (ej. una por
        # ciudad). Ni el texto del creativo ni su ID son confiables para
        # esto (dos anuncios distintos pueden compartir texto casi
        # identico, y el ID del creativo cambia en cada copia).
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
                "best_ad_id": ad_id,   # ad_id representativo, para pedir su creativo despues
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

    if os.environ.get("DEBUG_REPORTES"):
        for a in all_ads:
            if a["whatsapp"] > 0 or a["leads"] > 0:
                print(f"[DEBUG] grupo {a['name'][:40]!r} -> wa={a['whatsapp']} leads={a['leads']} filas={a['n_campañas']}")
                for row in a["_rows"]:
                    print(f"         ad_id={row[0]} ad_name={row[1]!r} wa={row[2]} leads={row[3]}")

    # OJO: si un mismo anuncio aparece en los dos rankings (genero tanto
    # whatsapps como leads), NO podemos usar el mismo dict para ambos --
    # al formatear los numeros como texto en un ranking, se corrompe la
    # comparacion numerica del otro. Por eso sacamos copias independientes.
    candidatos_wa = sorted(
        [dict(a) for a in all_ads if a["whatsapp"] > 0],
        key=lambda a: a["whatsapp"], reverse=True,
    )[:limit]
    candidatos_leads = sorted(
        [dict(a) for a in all_ads if a["leads"] > 0],
        key=lambda a: a["leads"], reverse=True,
    )[:limit]

    # Pedimos la info de creativo SOLO para los ganadores de ambos
    # rankings (unos pocos anuncios, no toda la cuenta).
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
    """Genera lecturas automaticas comparando el periodo actual contra el
    periodo anterior COMPARABLE (mismo numero de dias) -- funciona igual
    de bien para reportes mensuales que para quincenales, porque recibe
    los datos ya resueltos para el periodo correcto (no asume "mes")."""
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
    """Genera una pagina sencilla que lista todos los reportes mensuales
    guardados en el historial, mas reciente primero, para que el cliente
    pueda navegar meses anteriores desde un solo link fijo."""
    meses_es = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    archivos = sorted(archive_dir.glob("*.html"), reverse=True)
    archivos = [f for f in archivos if f.name != "index.html"]

    filas = []
    for f in archivos:
        slug = f.stem  # ej. "2026-06"
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
        print("ERROR: no encontre config.json. Copia config.example.json y llenalo.")
        sys.exit(1)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    token = get_access_token()
    account_id = config["account_id"]
    since = config["since"]   # ej. "2026-01-01"
    until = config["until"]   # ej. "2026-06-30", o "today" para usar la fecha de hoy
    if str(until).strip().lower() == "today":
        until = date.today().strftime("%Y-%m-%d")

    print(f"Jalando datos de {account_id}...")
    monthly = fetch_monthly_insights(account_id, token, since, until)

    until_date = datetime.strptime(until, "%Y-%m-%d")
    meses_es = ["enero","febrero","marzo","abril","mayo","junio","julio",
                "agosto","septiembre","octubre","noviembre","diciembre"]

    period_days = config.get("period_days")  # ej. 15 para reportes quincenales
    if period_days:
        # Reporte por rango de N dias (ej. quincenal) -- el resumen
        # ejecutivo cubre exactamente esos N dias terminando en "until",
        # NO el mes calendario completo.
        kpi_month_start = (until_date - timedelta(days=period_days - 1)).strftime("%Y-%m-%d")
        kpi_since_date = datetime.strptime(kpi_month_start, "%Y-%m-%d")
        if kpi_since_date.month == until_date.month and kpi_since_date.year == until_date.year:
            # mismo mes en ambos extremos: "16 al 30 de junio 2026"
            kpi_period_label = (
                f"{kpi_since_date.day} al {until_date.day} de "
                f"{meses_es[until_date.month - 1]} {until_date.year}"
            )
        else:
            # el periodo cruza el limite del mes (o del año): mostramos
            # el mes de cada extremo para no dar una fecha ambigua/erronea
            # ej. "25 de junio al 9 de julio 2026"
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
        kpi_until = until  # en modo quincenal, el fin del periodo si es "hoy"
        if not config.get("top_ads_per_campaign"):
            top_ads = fetch_top_ads(
                account_id, token,
                since=kpi_month_start, until=until,
                attribution_windows=config.get("attribution_windows"),
            )
        else:
            top_ads = None
    else:
        # Default: MES CALENDARIO ANTERIOR COMPLETO -- pensado para
        # automatizacion que corre el dia 1 de cada mes y debe reportar
        # todo el mes que acaba de terminar, NO el mes en curso (que
        # apenas estaria empezando y tendria datos parciales). Esto
        # tambien evita que el reporte muestre datos parciales si se
        # corre a mitad de mes para pruebas manuales.
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

    # IMPORTANTE: el costo por whatsapp/formulario se calcula con el gasto
    # REAL de las campañas que generaron cada resultado -- no con el gasto
    # total de la cuenta (que incluye campañas de awareness/trafico que
    # no generan ninguno de los dos, e inflarian el costo si se incluyeran).
    spend_wa, spend_leads = fetch_campaign_cost_breakdown(
        account_id, token, kpi_month_start, kpi_until
    )
    cost_per_wa = (spend_wa / last_wa) if last_wa else 0
    cost_per_lead = (spend_leads / last_leads) if last_leads else 0

    # Periodo anterior COMPARABLE (misma duracion en dias que el periodo
    # actual) -- funciona igual para mes calendario (~30 dias vs ~30 dias
    # del mes previo) que para quincenas (15 dias vs los 15 dias previos).
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

    # Desglose por campaña individual -- solo para clientes que lo pidan
    # (cuentas con muchas campañas de objetivos distintos, donde interesa
    # ver el KPI de cada una por separado, ej. "Seminuevos Whatsapp",
    # "Unidades nuevas Formulario", etc.)
    campaigns_breakdown = None
    if config.get("show_campaign_breakdown"):
        campaigns_breakdown = fetch_campaigns_breakdown(account_id, token, kpi_month_start, kpi_until)

    # Top de anuncios POR CAMPAÑA -- alternativa al ranking global, para
    # cuentas con campañas de objetivos muy distintos (ej. Seminuevos,
    # Reconocimiento, Refacciones) donde un top 3 general no seria
    # representativo (siempre ganaria la campaña con mas presupuesto).
    top_ads_by_campaign = None
    if config.get("top_ads_per_campaign"):
        top_n_campaign = config.get("top_ads_per_campaign_count", 3)
        # OJO: usamos el mismo rango que ya se resolvio arriba para los
        # KPIs (kpi_month_start -> until) en AMBAS ramas -- antes, la
        # rama mensual usaba date_preset="last_month" (el mes calendario
        # ANTERIOR), lo que causaba inconsistencias: si una campaña se
        # renombro este mes, el reporte seguia mostrando datos/nombres
        # del mes pasado en vez del periodo que dice el resto del reporte.
        top_ads_by_campaign = fetch_top_ad_per_campaign(
            account_id, token, since=kpi_month_start, until=kpi_until,
            attribution_windows=config.get("attribution_windows"),
            top_n=top_n_campaign,
        )

    template_path = Path(__file__).parent / "template.html"
    template = Template(template_path.read_text(encoding="utf-8"))

    # El link al historial solo depende de si esta habilitado (no de si
    # el archivo ya existe fisicamente) -- lo calculamos antes de
    # renderizar para no tener que renderizar dos veces.
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

    # Historial navegable -- ademas del reporte principal (que siempre
    # muestra el mes mas reciente), guardamos una copia archivada de cada
    # mes para que el cliente pueda revisar meses anteriores completos.
    if archive_url:
        archive_dir = output_dir / "archivo"
        archive_dir.mkdir(exist_ok=True, parents=True)
        slug = kpi_month_start[:7]  # ej. "2026-06"
        (archive_dir / f"{slug}.html").write_text(html, encoding="utf-8")
        build_archive_index(archive_dir, config["client_name"], config.get("agency_name", ""), main_filename)

    print(f"Listo! Dashboard generado en: {output_file}")


if __name__ == "__main__":
    main()
