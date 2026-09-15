#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tracker_precandidatos.py
========================
Auditoría digital de precandidaturas para Consensus Estrategia.

Recibe la configuración de monitoreo que exporta el CRM del tablero
(`monitoreo_precandidatos.json`) y consulta las APIs oficiales para traer
gasto en pauta, anuncios activos, audiencia e interacción reales.

Principio de operación: **este script nunca inventa un número.** Si una API
no responde, si falta un token o si una cuenta no es accesible, el campo se
entrega como `null` con el motivo declarado en `errores`. Un hueco honesto
es utilizable; un número inventado contamina todo el análisis que se
construya encima.

Fuentes que consulta
--------------------
  Meta Ad Library (/ads_archive)   gasto declarado y anuncios activos.
                                   Requiere token con `ads_read` y cuenta
                                   verificada para anuncios de tema político.
  Meta Graph API (Page Insights)   seguidores, alcance e interacción de
                                   páginas propias. Requiere el page access
                                   token de cada página.
  Instagram Business Discovery     seguidores y métricas públicas de cuentas
                                   de empresa o creador, consultadas desde
                                   una cuenta propia vinculada.

Sobre X y TikTok
----------------
Ninguna de las dos ofrece acceso gratuito a métricas de terceros. X requiere
un plan de pago de la API v2 y TikTok exige aprobación caso por caso de la
Research API. El script deja los conectores declarados y devuelve un motivo
explícito en lugar de estimar, porque raspar esas plataformas viola sus
términos de servicio y expone al cliente.

Uso
---
    export META_ACCESS_TOKEN="EAAG..."          # Ad Library
    export META_PAGE_TOKENS='{"1234567890":"EAAG..."}'   # Page Insights
    export IG_BUSINESS_ACCOUNT_ID="1784..."     # para Business Discovery

    python3 tracker_precandidatos.py --config monitoreo_precandidatos.json \\
        --salida auditoria_precandidatos.json --dias 30

    # Verificar qué se puede consultar antes de gastar llamadas
    python3 tracker_precandidatos.py --config monitoreo_precandidatos.json --diagnostico

El resultado se puede inyectar al paquete del tablero con:

    python3 etl_electoral_sync.py --config configuracion.json \\
        --auditoria auditoria_precandidatos.json

Requisitos: Python 3.9+. Sin dependencias externas.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

#: Pausa entre llamadas. Meta aplica límites por hora y por aplicación; ir
#: despacio sale más barato que agotar la cuota a media auditoría.
PAUSA_ENTRE_LLAMADAS_S = 0.6

#: Reintentos ante errores transitorios (429, 500, 503).
REINTENTOS = 3
CODIGOS_REINTENTABLES = (429, 500, 502, 503, 504)


# =====================================================================
# 1. TRANSPORTE
# =====================================================================

class ErrorAPI(RuntimeError):
    """Falla al consultar una API externa, con el motivo legible."""

    def __init__(self, mensaje: str, codigo: Optional[int] = None):
        super().__init__(mensaje)
        self.codigo = codigo


def peticion_json(url: str, parametros: Dict[str, Any],
                  timeout: int = 40) -> Dict[str, Any]:
    """GET con reintento exponencial ante errores transitorios.

    Los errores de la propia API (token inválido, permiso faltante, página
    inexistente) no se reintentan: se propagan con el mensaje que devuelve
    Meta, que suele decir exactamente qué falta.
    """
    consulta = urllib.parse.urlencode(
        {k: v for k, v in parametros.items() if v is not None}, doseq=True)
    destino = f"{url}?{consulta}"

    ultimo_error: Optional[Exception] = None
    for intento in range(REINTENTOS):
        peticion = urllib.request.Request(
            destino, headers={"User-Agent": "ConsensusEstrategia-Tracker/1.0"})
        try:
            with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
                return json.loads(respuesta.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            cuerpo = exc.read().decode("utf-8", errors="replace")
            detalle = _mensaje_de_meta(cuerpo) or cuerpo[:300]
            if exc.code in CODIGOS_REINTENTABLES and intento < REINTENTOS - 1:
                time.sleep(2 ** intento)
                ultimo_error = ErrorAPI(detalle, exc.code)
                continue
            raise ErrorAPI(f"HTTP {exc.code}: {detalle}", exc.code) from exc
        except urllib.error.URLError as exc:
            if intento < REINTENTOS - 1:
                time.sleep(2 ** intento)
                ultimo_error = ErrorAPI(f"Sin conexión: {exc.reason}")
                continue
            raise ErrorAPI(f"Sin conexión con {url}: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ErrorAPI(f"Respuesta ilegible de {url}: {exc}") from exc

    raise ultimo_error or ErrorAPI(f"No se pudo consultar {url}")


def _mensaje_de_meta(cuerpo: str) -> Optional[str]:
    """Extrae el mensaje legible del sobre de error de Graph API."""
    try:
        datos = json.loads(cuerpo)
    except json.JSONDecodeError:
        return None
    error = datos.get("error") or {}
    partes = [error.get("message"), error.get("error_user_msg")]
    mensaje = " · ".join(p for p in partes if p)
    return mensaje or None


def a_numero(valor: Any, defecto: float = 0.0) -> float:
    if valor is None:
        return defecto
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).replace(",", "").replace("$", "").strip()
    try:
        return float(texto)
    except ValueError:
        return defecto


def redondea(valor: float, decimales: int = 2) -> float:
    return round(float(valor or 0.0), decimales)


# =====================================================================
# 2. NORMALIZACION DE IDENTIFICADORES
# =====================================================================

def extrae_page_id(valor: Optional[str]) -> Optional[str]:
    """Acepta un Page ID numérico o una URL de Facebook y devuelve lo que
    sirve para consultar Graph API.

    Una URL con nombre de usuario (facebook.com/mi.pagina) no es un Page ID;
    Graph API la resuelve solo si el nombre sigue siendo válido, así que se
    devuelve tal cual y se deja que la API decida.
    """
    if not valor:
        return None
    texto = str(valor).strip()
    if texto.isdigit():
        return texto

    coincidencia = re.search(r"facebook\.com/(?:profile\.php\?id=)?([^/?&#]+)", texto)
    if coincidencia:
        candidato = coincidencia.group(1)
        return candidato if candidato not in ("pages", "pg") else None
    return texto.lstrip("@") or None


def normaliza_handle(valor: Optional[str]) -> Optional[str]:
    if not valor:
        return None
    return str(valor).strip().lstrip("@").split("/")[-1] or None


# =====================================================================
# 3. CONECTORES
# =====================================================================

@dataclass
class Credenciales:
    """Tokens disponibles en el entorno."""
    ad_library: Optional[str] = None
    page_tokens: Dict[str, str] = field(default_factory=dict)
    ig_business_account: Optional[str] = None
    ig_token: Optional[str] = None

    @classmethod
    def desde_entorno(cls) -> "Credenciales":
        tokens: Dict[str, str] = {}
        crudo = os.environ.get("META_PAGE_TOKENS")
        if crudo:
            try:
                cargado = json.loads(crudo)
                if isinstance(cargado, dict):
                    tokens = {str(k): str(v) for k, v in cargado.items()}
            except json.JSONDecodeError:
                print("META_PAGE_TOKENS no es un JSON válido; se ignora.",
                      file=sys.stderr)

        # Un token único de página también es válido para un solo cliente.
        unico = os.environ.get("META_PAGE_TOKEN")
        if unico:
            tokens.setdefault("__unico__", unico)

        return cls(
            ad_library=os.environ.get("META_ACCESS_TOKEN"),
            page_tokens=tokens,
            ig_business_account=os.environ.get("IG_BUSINESS_ACCOUNT_ID"),
            ig_token=os.environ.get("IG_ACCESS_TOKEN") or os.environ.get("META_ACCESS_TOKEN"),
        )

    def token_de_pagina(self, page_id: Optional[str]) -> Optional[str]:
        if page_id and page_id in self.page_tokens:
            return self.page_tokens[page_id]
        return self.page_tokens.get("__unico__")


class AdLibrary:
    """Gasto declarado y anuncios activos en la Ad Library de Meta."""

    CAMPOS = (
        "id", "page_id", "page_name", "ad_delivery_start_time",
        "ad_delivery_stop_time", "spend", "impressions", "currency",
        "publisher_platforms", "ad_creative_bodies", "ad_creative_link_titles",
    )

    def __init__(self, token: Optional[str], pais: str = "MX"):
        self.token = token
        self.pais = pais

    def consulta(self, termino: Optional[str] = None,
                 page_ids: Optional[Sequence[str]] = None,
                 limite: int = 200) -> List[Dict[str, Any]]:
        if not self.token:
            raise ErrorAPI("Falta META_ACCESS_TOKEN para consultar la Ad Library.")

        parametros = {
            "access_token": self.token,
            "ad_reached_countries": json.dumps([self.pais]),
            "ad_type": "POLITICAL_AND_ISSUE_ADS",
            "ad_active_status": "ALL",
            "fields": ",".join(self.CAMPOS),
            "limit": min(limite, 250),
        }
        if page_ids:
            parametros["search_page_ids"] = json.dumps(list(page_ids))
        elif termino:
            parametros["search_terms"] = termino
        else:
            raise ErrorAPI("Se requiere un Page ID o un término de búsqueda.")

        anuncios: List[Dict[str, Any]] = []
        url = f"{GRAPH_API_BASE}/ads_archive"
        while url and len(anuncios) < limite:
            datos = peticion_json(url, parametros)
            anuncios.extend(datos.get("data", []))
            siguiente = (datos.get("paging") or {}).get("next")
            if not siguiente:
                break
            # La URL de paginación ya trae todos los parámetros firmados.
            url, parametros = siguiente, {}
            time.sleep(PAUSA_ENTRE_LLAMADAS_S)

        return anuncios[:limite]

    @staticmethod
    def resume(anuncios: Sequence[Dict[str, Any]], dias: int) -> Dict[str, Any]:
        """Consolida el gasto de la ventana solicitada.

        La Ad Library publica rangos, no cifras exactas. Se conservan piso y
        techo, y se reporta el punto medio como estimador declarado: es lo
        más preciso que la fuente permite y así queda asentado.
        """
        desde = date.today() - timedelta(days=dias)
        piso = techo = 0.0
        impresiones_piso = impresiones_techo = 0.0
        activos = 0
        temas: Dict[str, int] = {}
        plataformas: Dict[str, int] = {}

        for anuncio in anuncios:
            inicio = str(anuncio.get("ad_delivery_start_time") or "")[:10]
            if inicio:
                try:
                    if date.fromisoformat(inicio) < desde:
                        continue
                except ValueError:
                    pass

            gasto = anuncio.get("spend") or {}
            piso += a_numero(gasto.get("lower_bound"))
            techo += a_numero(gasto.get("upper_bound"))

            impresiones = anuncio.get("impressions") or {}
            impresiones_piso += a_numero(impresiones.get("lower_bound"))
            impresiones_techo += a_numero(impresiones.get("upper_bound"))

            if not anuncio.get("ad_delivery_stop_time"):
                activos += 1

            for plataforma in anuncio.get("publisher_platforms") or []:
                plataformas[plataforma] = plataformas.get(plataforma, 0) + 1

            for titulo in (anuncio.get("ad_creative_link_titles") or [])[:1]:
                clave = str(titulo)[:60]
                temas[clave] = temas.get(clave, 0) + 1

        principales = sorted(temas.items(), key=lambda par: par[1], reverse=True)

        return {
            "ventana_dias": dias,
            "anuncios_en_ventana": len(anuncios),
            "anuncios_activos": activos,
            "gasto_piso_mxn": redondea(piso, 0),
            "gasto_techo_mxn": redondea(techo, 0),
            "gasto_estimado_mxn": redondea((piso + techo) / 2.0, 0),
            "impresiones_estimadas": int((impresiones_piso + impresiones_techo) / 2.0),
            "plataformas": plataformas,
            "temas_pautados": [nombre for nombre, _ in principales[:5]],
            "nota": ("La Ad Library publica rangos de gasto; el estimado es el "
                     "punto medio entre piso y techo."),
        }


class PaginaFacebook:
    """Audiencia e interacción de una página propia, vía Page Insights."""

    METRICAS = (
        "page_impressions_unique",
        "page_post_engagements",
        "page_fans",
        "page_fan_adds",
    )

    def __init__(self, page_id: str, token: Optional[str]):
        self.page_id = page_id
        self.token = token

    def perfil(self) -> Dict[str, Any]:
        if not self.token:
            raise ErrorAPI(
                f"No hay page access token para la página {self.page_id}. "
                f"Declara META_PAGE_TOKENS con el formato "
                f'{{"{self.page_id}": "EAAG..."}}.')
        datos = peticion_json(f"{GRAPH_API_BASE}/{self.page_id}", {
            "access_token": self.token,
            "fields": "id,name,username,fan_count,followers_count,link",
        })
        return {
            "page_id": datos.get("id"),
            "nombre": datos.get("name"),
            "usuario": datos.get("username"),
            "seguidores": int(a_numero(datos.get("followers_count")
                                       or datos.get("fan_count"))),
            "enlace": datos.get("link"),
        }

    def serie(self, dias: int = 30) -> List[Dict[str, Any]]:
        if not self.token:
            raise ErrorAPI(f"No hay page access token para {self.page_id}.")
        desde = (date.today() - timedelta(days=dias)).isoformat()
        datos = peticion_json(f"{GRAPH_API_BASE}/{self.page_id}/insights", {
            "access_token": self.token,
            "metric": ",".join(self.METRICAS),
            "period": "day",
            "since": desde,
            "until": date.today().isoformat(),
        })
        return self._aplana(datos.get("data", []))

    @staticmethod
    def _aplana(bloques: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        por_fecha: Dict[str, Dict[str, Any]] = {}
        for bloque in bloques:
            nombre = bloque.get("name")
            for valor in bloque.get("values", []):
                fecha = str(valor.get("end_time", ""))[:10]
                if not fecha:
                    continue
                por_fecha.setdefault(fecha, {"fecha": fecha})[nombre] = a_numero(
                    valor.get("value"))

        serie: List[Dict[str, Any]] = []
        for fecha in sorted(por_fecha):
            fila = por_fecha[fecha]
            seguidores = fila.get("page_fans", 0.0)
            interacciones = fila.get("page_post_engagements", 0.0)
            serie.append({
                "fecha": fecha,
                "audiencia": int(seguidores),
                "nuevos_seguidores": int(fila.get("page_fan_adds", 0.0)),
                "alcance": int(fila.get("page_impressions_unique", 0.0)),
                "interacciones": int(interacciones),
                "engagement_rate": redondea(
                    interacciones / seguidores * 100.0 if seguidores else 0.0),
            })
        return serie

    def publicaciones(self, dias: int = 30, limite: int = 100) -> List[Dict[str, Any]]:
        """Publicaciones con hora y formato, insumo del mapa de mejores horas
        y del análisis de rendimiento por formato."""
        if not self.token:
            raise ErrorAPI(f"No hay page access token para {self.page_id}.")
        desde = int((datetime.now(timezone.utc) - timedelta(days=dias)).timestamp())
        datos = peticion_json(f"{GRAPH_API_BASE}/{self.page_id}/published_posts", {
            "access_token": self.token,
            "since": desde,
            "limit": min(limite, 100),
            "fields": ("id,created_time,attachments{media_type},"
                       "insights.metric(post_impressions_unique,post_engaged_users)"),
        })

        salida: List[Dict[str, Any]] = []
        for publicacion in datos.get("data", []):
            creado = publicacion.get("created_time")
            if not creado:
                continue
            momento = datetime.fromisoformat(creado.replace("Z", "+00:00"))

            alcance = interacciones = 0.0
            for bloque in ((publicacion.get("insights") or {}).get("data") or []):
                valores = bloque.get("values") or [{}]
                valor = a_numero(valores[0].get("value"))
                if bloque.get("name") == "post_impressions_unique":
                    alcance = valor
                elif bloque.get("name") == "post_engaged_users":
                    interacciones = valor

            adjuntos = ((publicacion.get("attachments") or {}).get("data") or [{}])
            salida.append({
                "id": publicacion.get("id"),
                "fecha": momento.date().isoformat(),
                "dia": momento.weekday(),
                "hora": momento.hour,
                "formato": _traduce_formato(adjuntos[0].get("media_type")),
                "alcance": int(alcance),
                "interacciones": int(interacciones),
            })
        return salida


def _traduce_formato(media_type: Optional[str]) -> str:
    """Homologa los tipos de Graph API con las categorías del tablero."""
    mapa = {
        "video": "Video largo",
        "video_inline": "Reel",
        "photo": "Imagen",
        "album": "Carrusel",
        "link": "Imagen",
    }
    return mapa.get(str(media_type or "").lower(), "Sin clasificar")


class InstagramBusiness:
    """Métricas públicas de una cuenta de empresa o creador.

    Business Discovery permite consultar cuentas de terceros desde una cuenta
    propia vinculada a una página de Facebook. Solo funciona con cuentas de
    tipo empresa o creador: las personales no son consultables por API.
    """

    def __init__(self, cuenta_propia: Optional[str], token: Optional[str]):
        self.cuenta_propia = cuenta_propia
        self.token = token

    def descubre(self, usuario: str) -> Dict[str, Any]:
        if not (self.cuenta_propia and self.token):
            raise ErrorAPI(
                "Business Discovery requiere IG_BUSINESS_ACCOUNT_ID y un token "
                "con permisos de Instagram.")
        campos = (f"business_discovery.username({usuario})"
                  "{username,followers_count,media_count,"
                  "media.limit(12){timestamp,media_product_type,like_count,comments_count}}")
        datos = peticion_json(f"{GRAPH_API_BASE}/{self.cuenta_propia}", {
            "access_token": self.token,
            "fields": campos,
        })
        descubierto = datos.get("business_discovery") or {}
        publicaciones = (descubierto.get("media") or {}).get("data") or []

        interacciones = sum(
            a_numero(m.get("like_count")) + a_numero(m.get("comments_count"))
            for m in publicaciones)
        seguidores = int(a_numero(descubierto.get("followers_count")))

        return {
            "usuario": descubierto.get("username"),
            "seguidores": seguidores,
            "publicaciones_totales": int(a_numero(descubierto.get("media_count"))),
            "publicaciones_analizadas": len(publicaciones),
            "interacciones_recientes": int(interacciones),
            # Sobre las últimas publicaciones disponibles, no sobre una
            # ventana fija: Business Discovery no permite filtrar por fecha.
            "engagement_rate": redondea(
                interacciones / len(publicaciones) / seguidores * 100.0
                if publicaciones and seguidores else 0.0),
        }


def conector_no_disponible(plataforma: str) -> Dict[str, Any]:
    """X y TikTok no ofrecen acceso abierto a métricas de terceros."""
    motivos = {
        "x": ("La API v2 de X requiere un plan de pago para leer métricas de "
              "cuentas de terceros. Sin ese plan no hay dato disponible."),
        "tiktok": ("La Research API de TikTok exige aprobación caso por caso y "
                   "está restringida a instituciones académicas en varias "
                   "regiones. Sin acceso aprobado no hay dato disponible."),
    }
    return {
        "disponible": False,
        "motivo": motivos.get(plataforma, "Conector no configurado."),
    }


# =====================================================================
# 4. AUDITORIA POR PRECANDIDATURA
# =====================================================================

def audita_precandidato(registro: Dict[str, Any],
                        credenciales: Credenciales,
                        dias: int = 30,
                        con_publicaciones: bool = True) -> Dict[str, Any]:
    """Consulta todas las fuentes disponibles para una precandidatura.

    Devuelve un bloque con lo que sí se pudo obtener y una lista `errores`
    con lo que no, por fuente. Nunca rellena un hueco con una estimación.
    """
    cuentas = registro.get("cuentas") or {}
    page_id = extrae_page_id(cuentas.get("facebook_page"))
    ad_page_id = extrae_page_id(cuentas.get("meta_ad_library_page_id")) or page_id
    instagram = normaliza_handle(cuentas.get("instagram"))

    resultado: Dict[str, Any] = {
        "id": registro.get("id"),
        "nombre": registro.get("nombre"),
        "cargo": registro.get("cargo"),
        "territorio": registro.get("territorio"),
        "partido": registro.get("partido"),
        "eleccion_id": registro.get("eleccion_id"),
        "consultado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ventana_dias": dias,
        "facebook": None,
        "instagram": None,
        "pauta": None,
        "publicaciones": [],
        "x": conector_no_disponible("x") if cuentas.get("x") else None,
        "tiktok": conector_no_disponible("tiktok") if cuentas.get("tiktok") else None,
        "errores": [],
    }

    # --- Pauta declarada -------------------------------------------------
    if ad_page_id or registro.get("nombre"):
        try:
            cliente = AdLibrary(credenciales.ad_library)
            anuncios = cliente.consulta(
                page_ids=[ad_page_id] if ad_page_id else None,
                termino=None if ad_page_id else registro.get("nombre"),
            )
            resultado["pauta"] = AdLibrary.resume(anuncios, dias)
            resultado["pauta"]["consultado_por"] = (
                "page_id" if ad_page_id else "termino_de_busqueda")
            time.sleep(PAUSA_ENTRE_LLAMADAS_S)
        except ErrorAPI as exc:
            resultado["errores"].append({"fuente": "ad_library", "motivo": str(exc)})

    # --- Página de Facebook ----------------------------------------------
    if page_id:
        cliente = PaginaFacebook(page_id, credenciales.token_de_pagina(page_id))
        try:
            perfil = cliente.perfil()
            serie = cliente.serie(dias)
            resultado["facebook"] = {
                "perfil": perfil,
                "serie": serie,
                "seguidores": perfil.get("seguidores"),
                "crecimiento_7d": sum(p["nuevos_seguidores"] for p in serie[-7:]),
                "interacciones_7d": sum(p["interacciones"] for p in serie[-7:]),
                "alcance_ventana": sum(p["alcance"] for p in serie),
                "engagement_rate": redondea(
                    sum(p["interacciones"] for p in serie[-7:])
                    / perfil["seguidores"] * 100.0 if perfil.get("seguidores") else 0.0),
            }
            time.sleep(PAUSA_ENTRE_LLAMADAS_S)
        except ErrorAPI as exc:
            resultado["errores"].append({"fuente": "page_insights", "motivo": str(exc)})

        if con_publicaciones and resultado["facebook"]:
            try:
                resultado["publicaciones"] = cliente.publicaciones(dias)
                time.sleep(PAUSA_ENTRE_LLAMADAS_S)
            except ErrorAPI as exc:
                resultado["errores"].append(
                    {"fuente": "published_posts", "motivo": str(exc)})

    # --- Instagram --------------------------------------------------------
    if instagram:
        try:
            cliente_ig = InstagramBusiness(credenciales.ig_business_account,
                                           credenciales.ig_token)
            resultado["instagram"] = cliente_ig.descubre(instagram)
            time.sleep(PAUSA_ENTRE_LLAMADAS_S)
        except ErrorAPI as exc:
            resultado["errores"].append({"fuente": "instagram", "motivo": str(exc)})

    return resultado


def diagnostica(registros: Sequence[Dict[str, Any]],
                credenciales: Credenciales) -> Dict[str, Any]:
    """Revisa qué se puede consultar antes de gastar llamadas a la API."""
    listo: List[str] = []
    incompleto: List[Dict[str, Any]] = []

    for registro in registros:
        cuentas = registro.get("cuentas") or {}
        faltantes: List[str] = []
        if not extrae_page_id(cuentas.get("facebook_page")):
            faltantes.append("Facebook Page ID")
        if not (extrae_page_id(cuentas.get("meta_ad_library_page_id"))
                or cuentas.get("facebook_page")):
            faltantes.append("Page ID de Ad Library")
        if not normaliza_handle(cuentas.get("instagram")):
            faltantes.append("Instagram")

        if faltantes:
            incompleto.append({"nombre": registro.get("nombre"),
                               "faltantes": faltantes})
        else:
            listo.append(registro.get("nombre"))

    return {
        "credenciales": {
            "ad_library": bool(credenciales.ad_library),
            "page_tokens": len(credenciales.page_tokens),
            "instagram_business": bool(credenciales.ig_business_account),
        },
        "precandidaturas_completas": listo,
        "precandidaturas_incompletas": incompleto,
        "total": len(registros),
    }


# =====================================================================
# 5. CLI
# =====================================================================

def carga_configuracion(ruta: str) -> List[Dict[str, Any]]:
    with open(ruta, "r", encoding="utf-8") as fh:
        datos = json.load(fh)
    if isinstance(datos, list):
        return datos
    registros = datos.get("precandidatos")
    if not isinstance(registros, list):
        raise ValueError(
            f"{ruta}: se esperaba una lista en `precandidatos`. "
            f"Exporta la configuración desde el tablero, en Precandidatos → "
            f"Exportar configuración de monitoreo.")
    return registros


def construye_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracker_precandidatos",
        description="Auditoría digital de precandidaturas contra APIs oficiales.")
    parser.add_argument("--config", required=True,
                        help="JSON de monitoreo exportado por el tablero.")
    parser.add_argument("--salida", default="auditoria_precandidatos.json",
                        help="Archivo de resultados.")
    parser.add_argument("--dias", type=int, default=30,
                        help="Ventana de análisis en días (por omisión, 30).")
    parser.add_argument("--sin-publicaciones", action="store_true",
                        help="Omite el detalle de publicaciones, que es la "
                             "consulta más costosa en cuota de API.")
    parser.add_argument("--diagnostico", action="store_true",
                        help="Solo revisa credenciales y cuentas capturadas, "
                             "sin consultar las APIs.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = construye_parser().parse_args(argv)
    credenciales = Credenciales.desde_entorno()

    try:
        registros = carga_configuracion(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error al leer la configuración: {exc}", file=sys.stderr)
        return 2

    if not registros:
        print("La configuración no tiene precandidaturas registradas.", file=sys.stderr)
        return 1

    if args.diagnostico:
        informe = diagnostica(registros, credenciales)
        print(json.dumps(informe, ensure_ascii=False, indent=2))
        return 0

    if not credenciales.ad_library and not credenciales.page_tokens:
        print("Sin credenciales de Meta no hay nada que consultar. Define "
              "META_ACCESS_TOKEN y META_PAGE_TOKENS.", file=sys.stderr)
        return 2

    resultados: List[Dict[str, Any]] = []
    for registro in registros:
        nombre = registro.get("nombre", "sin nombre")
        print(f"Auditando {nombre}…", file=sys.stderr)
        resultados.append(audita_precandidato(
            registro, credenciales, dias=args.dias,
            con_publicaciones=not args.sin_publicaciones))

    paquete = {
        "meta": {
            "generado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "generador": "tracker_precandidatos.py",
            "ventana_dias": args.dias,
            "fuentes": ["Meta Ad Library API", "Meta Graph API (Page Insights)",
                        "Instagram Business Discovery"],
            "nota": ("Los campos nulos corresponden a fuentes que no "
                     "respondieron o que no están disponibles; el motivo se "
                     "detalla en `errores` de cada precandidatura."),
        },
        "auditorias": resultados,
    }

    with open(args.salida, "w", encoding="utf-8") as fh:
        json.dump(paquete, fh, ensure_ascii=False, indent=2)

    con_error = sum(1 for r in resultados if r["errores"])
    print(f"\nAuditoría escrita en: {args.salida}")
    print(f"  {len(resultados)} precandidaturas consultadas, "
          f"{con_error} con al menos una fuente sin responder.")
    for resultado in resultados:
        for error in resultado["errores"]:
            print(f"  - {resultado['nombre']} · {error['fuente']}: {error['motivo']}",
                  file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
