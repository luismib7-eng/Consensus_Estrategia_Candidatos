#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
etl_electoral_sync.py
=====================
Pipeline de extraccion, normalizacion y calculo de indicadores electorales
para la plataforma modular de Consensus Estrategia.

Cubre tres bloques:

  1. Ingesta y estandarizacion de computos y resultados electorales
     (formatos INE / IEPC Jalisco) a nivel Seccion Electoral.
  2. Conectores digitales: Meta Ad Library API (gasto declarado en pauta
     politica) y Meta Graph API / Page Insights (metricas organicas).
  3. Serializacion del JSON maestro `electoral_master_data.json` que
     consume el frontend sin ninguna transformacion adicional.

Indicadores calculados por seccion:
  - FTN  (Fuerza Territorial Neta): participacion historica de la fuerza
    politica ponderada por el nivel de participacion de cada eleccion.
  - Swing Index (voto blando): desviacion estandar del margen entre el
    primero y el segundo lugar en las ultimas tres elecciones.
  - Target de Movilizacion: votos minimos necesarios a partir de la lista
    nominal y el umbral de abstencion estimado.

Requisitos: Python 3.9+. Sin dependencias externas (solo biblioteca estandar).

Uso rapido
----------
    # Genera un paquete de demostracion listo para el dashboard
    python3 etl_electoral_sync.py --demo --salida electoral_master_data.json

    # Procesa configuracion real con archivos de computos descargados
    python3 etl_electoral_sync.py --config configuracion_tonala.json

    # Consulta de gasto en pauta (requiere token de Meta)
    export META_ACCESS_TOKEN="EAAG..."
    python3 etl_electoral_sync.py --config configuracion_tonala.json --con-red
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import statistics
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

VERSION_ESQUEMA = "1.1"
GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


# =====================================================================
# 1. UTILIDADES DE NORMALIZACION
# =====================================================================

def normaliza_texto(valor: Any) -> str:
    """Quita acentos, espacios sobrantes y pasa a mayusculas para comparar
    encabezados de archivos heterogeneos (INE, IEPC, OPLs estatales)."""
    texto = "" if valor is None else str(valor)
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"\s+", " ", texto).strip().upper()
    return texto


def a_numero(valor: Any, defecto: float = 0.0) -> float:
    """Convierte celdas de CSV a numero tolerando comas, espacios, guiones
    y marcas de 'sin dato' usadas por los OPLs."""
    if valor is None:
        return defecto
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if texto in ("", "-", "--", "S/D", "N/A", "NA", "ND", "null", "None"):
        return defecto
    texto = texto.replace(",", "").replace("$", "").replace("%", "").replace(" ", "")
    try:
        return float(texto)
    except ValueError:
        return defecto


def clave_seccion(valor: Any) -> str:
    """La seccion electoral se maneja como cadena de 4 digitos con ceros a la
    izquierda para evitar que Excel/pandas la degrade a entero."""
    numero = int(a_numero(valor, 0))
    return f"{numero:04d}"


def redondea(valor: float, decimales: int = 2) -> float:
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return 0.0
    return round(float(valor), decimales)


def hoy_iso() -> str:
    return date.today().isoformat()


def ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =====================================================================
# 2. CONFIGURACION DE INGESTA
# =====================================================================

@dataclass
class MapeoColumnas:
    """Describe como se llaman las columnas relevantes en un dataset concreto.

    Se comparan normalizadas (sin acentos, mayusculas), asi que basta con
    declarar los alias mas comunes de cada proveedor de datos.
    """
    seccion: Sequence[str] = ("SECCION", "SECCION ELECTORAL", "SECCION_ELECTORAL")
    lista_nominal: Sequence[str] = (
        "LISTA_NOMINAL", "LISTA NOMINAL", "LISTA_NOMINAL_CASILLA",
        "LISTA NOMINAL CASILLA", "PADRON", "PADRON_ELECTORAL",
    )
    total_votos: Sequence[str] = (
        "TOTAL_VOTOS", "TOTAL VOTOS", "TOTAL_VOTOS_CALCULADOS",
        "TOTAL_VOTOS_CALCULADO", "VOTACION_TOTAL_EMITIDA", "TOTAL",
    )
    distrito_local: Sequence[str] = ("ID_DISTRITO_LOCAL", "DISTRITO_LOCAL", "DISTRITO L")
    distrito_federal: Sequence[str] = ("ID_DISTRITO_FEDERAL", "DISTRITO_FEDERAL", "DISTRITO F")
    municipio: Sequence[str] = ("MUNICIPIO", "NOMBRE_MUNICIPIO", "ID_MUNICIPIO")
    casillas: Sequence[str] = ("CASILLAS", "NUM_CASILLAS", "TOTAL_CASILLAS")
    # Columnas que nunca deben interpretarse como votacion de un partido.
    excluir: Sequence[str] = (
        "CLAVE_CASILLA", "CLAVE_ACTA", "ID_ESTADO", "ESTADO", "NOMBRE_ESTADO",
        "TIPO_CASILLA", "EXT_CONTIGUA", "UBICACION_CASILLA", "TIPO_ACTA",
        "NUM_ACTA_IMPRESO", "OBSERVACIONES", "MECANISMOS_TRASLADO",
        "NO_REGISTRADOS", "NO_REGISTRADAS", "CANDIDATOS_NO_REGISTRADOS",
        "NULOS", "VOTOS_NULOS", "VOTOS NULOS", "FECHA_HORA",
    )


#: Mapeos predefinidos. `INE` cubre los computos de siceen21 y datos abiertos;
#: `IEPC_JALISCO` cubre los computos distritales/municipales del OPL local.
MAPEOS = {
    "INE": MapeoColumnas(),
    "IEPC_JALISCO": MapeoColumnas(
        lista_nominal=("LISTA_NOMINAL", "LISTA NOMINAL", "LISTA_NOMINAL_CASILLA"),
        total_votos=("TOTAL_VOTOS", "VOTACION_TOTAL", "TOTAL_VOTOS_CALCULADOS"),
    ),
}


@dataclass
class FuenteEleccion:
    """Un archivo de computos correspondiente a un anio electoral."""
    anio: int
    ruta: str
    mapeo: str = "INE"
    #: Partidos o siglas que integran la coalicion del cliente en esa eleccion.
    coalicion: Sequence[str] = field(default_factory=list)
    #: Filtro opcional por municipio o distrito dentro de un archivo estatal.
    filtro_columna: Optional[str] = None
    filtro_valor: Optional[str] = None


@dataclass
class ParametrosCalculo:
    """Parametros de negocio del modelo electoral."""
    abstencion_estimada_pct: float = 52.0
    margen_seguridad_pct: float = 3.0
    umbral_swing_pct: float = 6.0
    umbral_margen_competido_pct: float = 5.0
    #: Peso extra que se da a la eleccion mas reciente al calcular la FTN.
    factor_recencia: float = 1.25


# =====================================================================
# 3. INGESTA Y ESTANDARIZACION ELECTORAL
# =====================================================================

def _resuelve_columna(encabezados_norm: Dict[str, str], alias: Sequence[str]) -> Optional[str]:
    """Devuelve el nombre original de la primera columna que coincida con los
    alias declarados en el mapeo."""
    for candidato in alias:
        clave = normaliza_texto(candidato)
        if clave in encabezados_norm:
            return encabezados_norm[clave]
    return None


def _detecta_columnas_partido(
    encabezados: Sequence[str],
    mapeo: MapeoColumnas,
    columnas_estructurales: Iterable[Optional[str]],
) -> List[str]:
    """Toda columna que no sea estructural ni este excluida se considera
    votacion por partido, coalicion o candidatura comun."""
    estructurales = {c for c in columnas_estructurales if c}
    excluidas = {normaliza_texto(c) for c in mapeo.excluir}
    partidos: List[str] = []
    for col in encabezados:
        if col in estructurales:
            continue
        if normaliza_texto(col) in excluidas:
            continue
        partidos.append(col)
    return partidos


def lee_computos(fuente: FuenteEleccion) -> Dict[str, Dict[str, Any]]:
    """Lee un archivo de computos y lo agrega a nivel seccion electoral.

    Devuelve un diccionario {seccion: {lista_nominal, total_votos, votos_por_partido,
    distrito_local, distrito_federal, casillas}}.
    Los computos vienen por casilla, de modo que se suman las casillas de cada
    seccion. Soporta CSV separado por coma, punto y coma o tabulador.
    """
    mapeo = MAPEOS.get(fuente.mapeo.upper(), MAPEOS["INE"])

    with open(fuente.ruta, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        muestra = fh.read(8192)
        fh.seek(0)
        try:
            dialecto = csv.Sniffer().sniff(muestra, delimiters=",;|\t")
        except csv.Error:
            dialecto = csv.excel
        lector = csv.DictReader(fh, dialect=dialecto)
        encabezados = [h for h in (lector.fieldnames or []) if h]
        encabezados_norm = {normaliza_texto(h): h for h in encabezados}

        col_seccion = _resuelve_columna(encabezados_norm, mapeo.seccion)
        if not col_seccion:
            raise ValueError(
                f"{fuente.ruta}: no se encontro la columna de seccion electoral. "
                f"Encabezados disponibles: {', '.join(encabezados[:15])}"
            )
        col_lista = _resuelve_columna(encabezados_norm, mapeo.lista_nominal)
        col_total = _resuelve_columna(encabezados_norm, mapeo.total_votos)
        col_dlocal = _resuelve_columna(encabezados_norm, mapeo.distrito_local)
        col_dfederal = _resuelve_columna(encabezados_norm, mapeo.distrito_federal)
        col_municipio = _resuelve_columna(encabezados_norm, mapeo.municipio)
        col_casillas = _resuelve_columna(encabezados_norm, mapeo.casillas)

        columnas_partido = _detecta_columnas_partido(
            encabezados,
            mapeo,
            [col_seccion, col_lista, col_total, col_dlocal, col_dfederal,
             col_municipio, col_casillas],
        )

        filtro_col = None
        if fuente.filtro_columna:
            filtro_col = _resuelve_columna(encabezados_norm, [fuente.filtro_columna])
        filtro_val = normaliza_texto(fuente.filtro_valor) if fuente.filtro_valor else None

        agregado: Dict[str, Dict[str, Any]] = {}
        for fila in lector:
            if filtro_col and filtro_val:
                if normaliza_texto(fila.get(filtro_col)) != filtro_val:
                    continue

            seccion = clave_seccion(fila.get(col_seccion))
            if seccion == "0000":
                continue

            registro = agregado.setdefault(seccion, {
                "seccion": seccion,
                "lista_nominal": 0.0,
                "total_votos": 0.0,
                "casillas": 0,
                "distrito_local": None,
                "distrito_federal": None,
                "municipio": None,
                "votos": {},
            })

            registro["lista_nominal"] += a_numero(fila.get(col_lista)) if col_lista else 0.0
            registro["casillas"] += int(a_numero(fila.get(col_casillas), 1)) if col_casillas else 1

            if registro["distrito_local"] is None and col_dlocal:
                registro["distrito_local"] = int(a_numero(fila.get(col_dlocal))) or None
            if registro["distrito_federal"] is None and col_dfederal:
                registro["distrito_federal"] = int(a_numero(fila.get(col_dfederal))) or None
            if registro["municipio"] is None and col_municipio:
                registro["municipio"] = str(fila.get(col_municipio) or "").strip() or None

            suma_partidos = 0.0
            for col in columnas_partido:
                votos = a_numero(fila.get(col))
                if votos <= 0:
                    continue
                sigla = normaliza_texto(col)
                registro["votos"][sigla] = registro["votos"].get(sigla, 0.0) + votos
                suma_partidos += votos

            # Si el archivo no trae total, se reconstruye sumando partidos.
            registro["total_votos"] += (
                a_numero(fila.get(col_total)) if col_total else suma_partidos
            )

    return agregado


def votos_coalicion(votos: Dict[str, float], siglas: Sequence[str]) -> float:
    """Suma la votacion de las siglas que integran la coalicion del cliente,
    incluyendo las columnas de candidatura comun tipo 'PAN_PRI_PRD'."""
    objetivo = {normaliza_texto(s) for s in siglas}
    if not objetivo:
        return 0.0
    total = 0.0
    for columna, valor in votos.items():
        partes = {p for p in re.split(r"[_\-\s/]+", columna) if p}
        if partes & objetivo:
            total += valor
    return total


# =====================================================================
# 4. INDICADORES ELECTORALES
# =====================================================================

def calcula_ftn(
    historico: Sequence[Tuple[int, float, float]],
    factor_recencia: float = 1.25,
) -> float:
    """Fuerza Territorial Neta.

    historico: lista de tuplas (anio, porcentaje_de_votacion, participacion_pct).
    Cada eleccion pesa segun su nivel de participacion y un factor de recencia
    que privilegia la eleccion mas reciente. El resultado es el porcentaje de
    votacion historica ponderada de la fuerza politica en esa seccion.
    """
    if not historico:
        return 0.0
    anios = sorted({h[0] for h in historico})
    numerador = 0.0
    denominador = 0.0
    for anio, porcentaje, participacion in historico:
        posicion = anios.index(anio)  # 0 = mas antigua
        recencia = factor_recencia ** posicion
        peso = max(participacion, 1.0) * recencia
        numerador += porcentaje * peso
        denominador += peso
    return redondea(numerador / denominador if denominador else 0.0)


def calcula_swing_index(margenes: Sequence[float]) -> float:
    """Voto blando / Swing Index.

    margenes: margen porcentual entre el primer y el segundo lugar en cada una
    de las ultimas elecciones disponibles (maximo tres). Un valor alto implica
    una seccion volatil, donde la ventaja cambia de eleccion a eleccion.
    """
    serie = [m for m in margenes if m is not None][-3:]
    if len(serie) < 2:
        return 0.0
    return redondea(statistics.pstdev(serie))


def calcula_target_movilizacion(
    lista_nominal: float,
    share_objetivo_pct: float,
    abstencion_estimada_pct: float,
) -> int:
    """Votos minimos necesarios en la seccion.

    Proyecta la votacion emitida a partir de la lista nominal y el umbral de
    abstencion estimado, y sobre esa base aplica el porcentaje objetivo
    (primer lugar historico mas el margen de seguridad definido).
    """
    participacion = max(0.0, min(100.0, 100.0 - abstencion_estimada_pct)) / 100.0
    votacion_proyectada = max(lista_nominal, 0.0) * participacion
    return int(math.ceil(votacion_proyectada * max(share_objetivo_pct, 0.0) / 100.0))


def clasifica_seccion(
    margen_ultima_pct: float,
    swing_index: float,
    parametros: ParametrosCalculo,
) -> str:
    """Clasificacion operativa de la seccion para el tablero.

    - ganada : se gano la ultima eleccion con margen estable.
    - swing  : competida o volatil; es donde se decide la eleccion.
    - riesgo : se perdio la ultima eleccion y el comportamiento es estable.
    """
    if abs(margen_ultima_pct) <= parametros.umbral_margen_competido_pct:
        return "swing"
    if swing_index >= parametros.umbral_swing_pct:
        return "swing"
    return "ganada" if margen_ultima_pct > 0 else "riesgo"


def construye_secciones(
    fuentes: Sequence[FuenteEleccion],
    parametros: ParametrosCalculo,
) -> List[Dict[str, Any]]:
    """Cruza los computos de todas las elecciones cargadas y devuelve la lista
    de secciones ya enriquecida con FTN, Swing Index y target."""
    lecturas: Dict[int, Dict[str, Dict[str, Any]]] = {}
    coaliciones: Dict[int, Sequence[str]] = {}
    for fuente in sorted(fuentes, key=lambda f: f.anio):
        lecturas[fuente.anio] = lee_computos(fuente)
        coaliciones[fuente.anio] = fuente.coalicion

    anios = sorted(lecturas.keys())
    if not anios:
        return []

    secciones_totales = sorted({s for datos in lecturas.values() for s in datos})
    salida: List[Dict[str, Any]] = []

    for seccion in secciones_totales:
        historico: List[Tuple[int, float, float]] = []
        margenes: List[float] = []
        resultados_por_anio: Dict[str, Dict[str, float]] = {}
        lista_nominal_ref = 0.0
        distrito_local = None
        distrito_federal = None
        municipio = None
        casillas = 0

        for anio in anios:
            registro = lecturas[anio].get(seccion)
            if not registro:
                continue

            lista_nominal = registro["lista_nominal"]
            total = registro["total_votos"]
            votos = registro["votos"]

            lista_nominal_ref = lista_nominal or lista_nominal_ref
            distrito_local = distrito_local or registro.get("distrito_local")
            distrito_federal = distrito_federal or registro.get("distrito_federal")
            municipio = municipio or registro.get("municipio")
            casillas = max(casillas, registro.get("casillas", 0))

            participacion = (total / lista_nominal * 100.0) if lista_nominal else 0.0
            propios = votos_coalicion(votos, coaliciones.get(anio, []))
            share_propio = (propios / total * 100.0) if total else 0.0

            ordenados = sorted(votos.values(), reverse=True)
            primero = ordenados[0] if ordenados else 0.0
            segundo = ordenados[1] if len(ordenados) > 1 else 0.0
            share_primero = (primero / total * 100.0) if total else 0.0
            share_segundo = (segundo / total * 100.0) if total else 0.0

            # Margen propio: positivo si la coalicion del cliente va arriba.
            if propios >= primero:
                margen = share_propio - share_segundo
            else:
                margen = share_propio - share_primero

            historico.append((anio, share_propio, participacion))
            margenes.append(margen)
            resultados_por_anio[str(anio)] = {
                "participacion_pct": redondea(participacion),
                "share_propio_pct": redondea(share_propio),
                "share_primer_lugar_pct": redondea(share_primero),
                "margen_pct": redondea(margen),
                "votos_propios": int(propios),
                "votos_emitidos": int(total),
            }

        if not historico:
            continue

        ftn = calcula_ftn(historico, parametros.factor_recencia)
        swing = calcula_swing_index(margenes)
        margen_ultima = margenes[-1] if margenes else 0.0
        share_primero_ultima = max(
            (r["share_primer_lugar_pct"] for r in resultados_por_anio.values()), default=0.0
        )
        share_objetivo = min(share_primero_ultima + parametros.margen_seguridad_pct, 85.0)
        target = calcula_target_movilizacion(
            lista_nominal_ref, share_objetivo, parametros.abstencion_estimada_pct
        )
        participacion_media = redondea(
            statistics.fmean([h[2] for h in historico]) if historico else 0.0
        )

        salida.append({
            "seccion": seccion,
            "municipio": municipio,
            "distrito_local": distrito_local,
            "distrito_federal": distrito_federal,
            "casillas": casillas or 1,
            "lista_nominal": int(lista_nominal_ref),
            "participacion_media_pct": participacion_media,
            "ftn": ftn,
            "swing_index": swing,
            "margen_ultima_pct": redondea(margen_ultima),
            "target_movilizacion": target,
            "clasificacion": clasifica_seccion(margen_ultima, swing, parametros),
            "historico": resultados_por_anio,
        })

    return salida


def resume_indicadores(secciones: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Agregados de seccion que alimentan las tarjetas de KPI del tablero."""
    if not secciones:
        return {
            "ftn_promedio": 0.0, "swing_promedio": 0.0,
            "casillas_ganadas": 0, "casillas_swing": 0, "casillas_riesgo": 0,
            "target_total": 0, "lista_nominal_total": 0,
            "cobertura_prioritarias_pct": 0.0,
        }

    conteo = {"ganada": 0, "swing": 0, "riesgo": 0}
    for s in secciones:
        conteo[s["clasificacion"]] = conteo.get(s["clasificacion"], 0) + 1

    prioritarias = [s for s in secciones if s["clasificacion"] in ("swing", "riesgo")]
    ftn_ponderada = sum(s["ftn"] * max(s["lista_nominal"], 1) for s in secciones)
    lista_total = sum(max(s["lista_nominal"], 1) for s in secciones)

    return {
        "ftn_promedio": redondea(ftn_ponderada / lista_total if lista_total else 0.0),
        "swing_promedio": redondea(statistics.fmean([s["swing_index"] for s in secciones])),
        "casillas_ganadas": conteo.get("ganada", 0),
        "casillas_swing": conteo.get("swing", 0),
        "casillas_riesgo": conteo.get("riesgo", 0),
        "secciones_prioritarias": len(prioritarias),
        "target_total": int(sum(s["target_movilizacion"] for s in secciones)),
        "lista_nominal_total": int(sum(s["lista_nominal"] for s in secciones)),
        "cobertura_prioritarias_pct": redondea(
            len(prioritarias) / len(secciones) * 100.0 if secciones else 0.0
        ),
    }


# =====================================================================
# 5. CONECTORES DIGITALES
# =====================================================================

def _peticion_json(url: str, parametros: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    """GET contra un endpoint REST que responde JSON. Lanza RuntimeError con el
    cuerpo del error para que el operador vea el mensaje real de la API."""
    consulta = urllib.parse.urlencode(
        {k: v for k, v in parametros.items() if v is not None}, doseq=True
    )
    peticion = urllib.request.Request(
        f"{url}?{consulta}",
        headers={"User-Agent": "ConsensusEstrategia-ETL/1.0"},
    )
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            return json.loads(respuesta.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} en {url}: {detalle}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Sin conexion con {url}: {exc.reason}") from exc


class MetaAdLibraryClient:
    """Consulta el gasto declarado en anuncios de tema politico o social.

    Requiere un token de acceso con permiso `ads_read` y la cuenta verificada
    para publicidad de temas politicos. Endpoint: /ads_archive.
    """

    def __init__(self, access_token: Optional[str] = None, pais: str = "MX"):
        self.access_token = access_token or os.environ.get("META_ACCESS_TOKEN")
        self.pais = pais

    @property
    def habilitado(self) -> bool:
        return bool(self.access_token)

    def busca_anuncios(self, termino: str, limite: int = 100) -> List[Dict[str, Any]]:
        if not self.habilitado:
            raise RuntimeError(
                "Falta META_ACCESS_TOKEN. Exporta la variable de entorno o pasa "
                "el token al construir MetaAdLibraryClient."
            )
        campos = [
            "id", "page_name", "ad_delivery_start_time", "ad_delivery_stop_time",
            "spend", "impressions", "currency", "publisher_platforms",
            "ad_creative_bodies", "demographic_distribution",
        ]
        datos = _peticion_json(f"{GRAPH_API_BASE}/ads_archive", {
            "access_token": self.access_token,
            "search_terms": termino,
            "ad_reached_countries": f'["{self.pais}"]',
            "ad_type": "POLITICAL_AND_ISSUE_ADS",
            "ad_active_status": "ALL",
            "fields": ",".join(campos),
            "limit": limite,
        })
        return datos.get("data", [])

    @staticmethod
    def resume_gasto(anuncios: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """La Ad Library reporta rangos (lower_bound / upper_bound). Se usa el
        punto medio como estimador y se conservan los extremos para auditoria."""
        piso = techo = 0.0
        impresiones = 0.0
        for anuncio in anuncios:
            gasto = anuncio.get("spend") or {}
            piso += a_numero(gasto.get("lower_bound"))
            techo += a_numero(gasto.get("upper_bound"))
            impr = anuncio.get("impressions") or {}
            impresiones += (
                a_numero(impr.get("lower_bound")) + a_numero(impr.get("upper_bound"))
            ) / 2.0
        return {
            "anuncios_activos": len(anuncios),
            "gasto_estimado_mxn": redondea((piso + techo) / 2.0, 0),
            "gasto_piso_mxn": redondea(piso, 0),
            "gasto_techo_mxn": redondea(techo, 0),
            "impresiones_estimadas": int(impresiones),
        }


class MetaGraphInsightsClient:
    """Metricas organicas de paginas propias (Page Insights).

    En produccion requiere el page access token del candidato. Cuando no hay
    token disponible, `genera_mock` entrega la misma estructura de datos para
    que el frontend se desarrolle y se demuestre sin bloquear el pipeline.
    """

    METRICAS = (
        "page_impressions_unique",
        "page_post_engagements",
        "page_fans",
        "page_fan_adds",
    )

    def __init__(self, page_id: Optional[str] = None, access_token: Optional[str] = None):
        self.page_id = page_id
        self.access_token = access_token or os.environ.get("META_PAGE_TOKEN")

    @property
    def habilitado(self) -> bool:
        return bool(self.page_id and self.access_token)

    def serie_insights(self, dias: int = 30) -> List[Dict[str, Any]]:
        if not self.habilitado:
            raise RuntimeError("Faltan page_id o META_PAGE_TOKEN para Page Insights.")
        desde = (date.today() - timedelta(days=dias)).isoformat()
        datos = _peticion_json(f"{GRAPH_API_BASE}/{self.page_id}/insights", {
            "access_token": self.access_token,
            "metric": ",".join(self.METRICAS),
            "period": "day",
            "since": desde,
            "until": hoy_iso(),
        })
        return self._aplana(datos.get("data", []))

    @staticmethod
    def _aplana(bloques: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convierte la respuesta por metrica de Graph API en una serie diaria."""
        por_fecha: Dict[str, Dict[str, Any]] = {}
        for bloque in bloques:
            nombre = bloque.get("name")
            for valor in bloque.get("values", []):
                fecha = str(valor.get("end_time", ""))[:10]
                if not fecha:
                    continue
                por_fecha.setdefault(fecha, {"fecha": fecha})[nombre] = a_numero(valor.get("value"))
        serie = []
        for fecha in sorted(por_fecha):
            fila = por_fecha[fecha]
            seguidores = fila.get("page_fans", 0.0)
            interacciones = fila.get("page_post_engagements", 0.0)
            serie.append({
                "fecha": fecha,
                "audiencia": int(seguidores),
                "nuevos_seguidores": int(fila.get("page_fan_adds", 0.0)),
                "alcance": int(fila.get("page_impressions_unique", 0.0)),
                "engagement_rate": redondea(
                    interacciones / seguidores * 100.0 if seguidores else 0.0
                ),
                "gasto_ads_mxn": 0.0,
            })
        return serie

    @staticmethod
    def genera_mock(dias: int = 30, audiencia_inicial: int = 78000,
                    semilla: int = 20270606) -> List[Dict[str, Any]]:
        """Serie sintetica con la misma forma que la respuesta real, util para
        maquetacion y demostraciones comerciales."""
        rng = random.Random(semilla)
        serie: List[Dict[str, Any]] = []
        audiencia = float(audiencia_inicial)
        for i in range(dias):
            fecha = (date.today() - timedelta(days=dias - 1 - i)).isoformat()
            evento_calle = i % 7 in (4, 5)  # picos de fin de semana
            nuevos = rng.randint(120, 420) * (2 if evento_calle else 1)
            audiencia += nuevos
            engagement = rng.uniform(3.4, 6.8) * (1.18 if evento_calle else 1.0)
            serie.append({
                "fecha": fecha,
                "audiencia": int(audiencia),
                "nuevos_seguidores": nuevos,
                "alcance": int(audiencia * rng.uniform(1.4, 2.9)),
                "engagement_rate": redondea(engagement),
                "gasto_ads_mxn": redondea(rng.uniform(4500, 21000), 0),
            })
        return serie


def calcula_nfs(positivos_pct: float, negativos_pct: float) -> float:
    """Indice de Favorabilidad Neta: comentarios positivos menos negativos."""
    return redondea(positivos_pct - negativos_pct)


# =====================================================================
# 5.b INTELIGENCIA DIGITAL COMPETITIVA
#     Benchmark entre actores, ventanas horarias de publicacion y
#     deteccion de anomalias de conversacion (ataque coordinado).
# =====================================================================

def calcula_engagement_rate(interacciones: float, seguidores: float) -> float:
    """(Interacciones / Seguidores) x 100. Es la tasa real, no la que reporta
    la plataforma sobre alcance, que infla el dato en cuentas pequenas."""
    if not seguidores:
        return 0.0
    return redondea(interacciones / seguidores * 100.0)


def calcula_share_of_voice(menciones_por_actor: Dict[str, float]) -> Dict[str, float]:
    """Reparto porcentual de la conversacion municipal entre los actores."""
    total = sum(max(v, 0.0) for v in menciones_por_actor.values())
    if not total:
        return {actor: 0.0 for actor in menciones_por_actor}
    return {
        actor: redondea(max(valor, 0.0) / total * 100.0)
        for actor, valor in menciones_por_actor.items()
    }


def costo_por_mil_alcanzados(gasto_mxn: float, alcance: float) -> float:
    """CPM territorial: cuanto cuesta alcanzar a mil personas del municipio."""
    if not alcance:
        return 0.0
    return redondea(gasto_mxn / (alcance / 1000.0), 2)


def construye_benchmark(actores: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normaliza a los actores en una sola tabla comparable.

    Cada actor entra con: nombre, coalicion, es_propio, seguidores,
    crecimiento_7d, interacciones_7d, publicaciones_7d, menciones_30d,
    gasto_ads_30d_mxn y alcance_30d. Se derivan engagement rate, share of
    voice, CPM e interacciones por publicacion, y se ordena por share of voice.
    """
    menciones = {a["nombre"]: a.get("menciones_30d", 0.0) for a in actores}
    reparto = calcula_share_of_voice(menciones)

    tabla: List[Dict[str, Any]] = []
    for actor in actores:
        seguidores = float(actor.get("seguidores", 0) or 0)
        crecimiento = float(actor.get("crecimiento_7d", 0) or 0)
        interacciones = float(actor.get("interacciones_7d", 0) or 0)
        publicaciones = float(actor.get("publicaciones_7d", 0) or 0)
        gasto = float(actor.get("gasto_ads_30d_mxn", 0) or 0)
        alcance = float(actor.get("alcance_30d", 0) or 0)

        tabla.append({
            "nombre": actor["nombre"],
            "coalicion": actor.get("coalicion", ""),
            "es_propio": bool(actor.get("es_propio")),
            "seguidores": int(seguidores),
            "crecimiento_7d": int(crecimiento),
            "crecimiento_7d_pct": redondea(
                crecimiento / (seguidores - crecimiento) * 100.0
                if seguidores - crecimiento > 0 else 0.0
            ),
            "publicaciones_7d": int(publicaciones),
            "interacciones_7d": int(interacciones),
            "interacciones_por_publicacion": redondea(
                interacciones / publicaciones if publicaciones else 0.0, 0
            ),
            "engagement_rate": calcula_engagement_rate(interacciones, seguidores),
            "share_of_voice_pct": reparto.get(actor["nombre"], 0.0),
            "menciones_30d": int(actor.get("menciones_30d", 0) or 0),
            "gasto_ads_30d_mxn": redondea(gasto, 0),
            "alcance_30d": int(alcance),
            "cpm_mxn": costo_por_mil_alcanzados(gasto, alcance),
            "nfs": redondea(actor.get("nfs", 0.0)),
            "anuncios_activos": int(actor.get("anuncios_activos", 0) or 0),
            "temas_pauta": list(actor.get("temas_pauta", [])),
            "formatos": (actor.get("formatos")
                         or analiza_formatos(actor.get("publicaciones", []))),
        })

    tabla.sort(key=lambda a: a["share_of_voice_pct"], reverse=True)
    for posicion, fila in enumerate(tabla, start=1):
        fila["posicion_sov"] = posicion
        mejores = [f for f in fila["formatos"] if f.get("es_mejor")]
        fila["mejor_formato"] = mejores[0]["formato"] if mejores else None
    return tabla


DIAS_SEMANA = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")

#: Franjas operativas de campana: la manana sirve para convocar, la tarde para
#: dar seguimiento y la noche es donde se concentra el consumo de video.
FRANJAS = (
    ("Mañana", 6, 11),
    ("Tarde", 12, 18),
    ("Noche", 19, 23),
)


def resume_franjas(registros: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Agrega las publicaciones en las tres franjas operativas del dia."""
    salida: List[Dict[str, Any]] = []
    for nombre, desde, hasta in FRANJAS:
        dentro = [r for r in registros
                  if desde <= int(r.get("hora", 0)) <= hasta]
        alcance = sum(float(r.get("alcance", 0) or 0) for r in dentro)
        interacciones = sum(float(r.get("interacciones", 0) or 0) for r in dentro)
        salida.append({
            "franja": nombre,
            "rango": f"{desde:02d}:00 a {hasta:02d}:59",
            "publicaciones": len(dentro),
            "alcance": int(alcance),
            "interacciones": int(interacciones),
            "tasa_respuesta": redondea(interacciones / alcance * 100.0 if alcance else 0.0),
        })
    mejor = max(salida, key=lambda f: f["tasa_respuesta"], default=None)
    for franja in salida:
        franja["es_mejor"] = bool(mejor and franja["franja"] == mejor["franja"]
                                  and franja["tasa_respuesta"] > 0)
    return salida


def analiza_formatos(registros: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rendimiento por formato de publicacion.

    Compara Reel, Imagen, Carrusel y Video largo por tasa de respuesta sobre
    alcance, no por interacciones absolutas: un Reel con mucho alcance puede
    acumular mas likes y convertir peor que un carrusel bien armado.
    """
    agrupado: Dict[str, Dict[str, float]] = {}
    for registro in registros:
        formato = str(registro.get("formato") or "Sin clasificar")
        celda = agrupado.setdefault(formato, {
            "publicaciones": 0.0, "alcance": 0.0, "interacciones": 0.0
        })
        celda["publicaciones"] += 1
        celda["alcance"] += float(registro.get("alcance", 0) or 0)
        celda["interacciones"] += float(registro.get("interacciones", 0) or 0)

    salida: List[Dict[str, Any]] = []
    for formato, celda in agrupado.items():
        alcance = celda["alcance"]
        salida.append({
            "formato": formato,
            "publicaciones": int(celda["publicaciones"]),
            "alcance_promedio": int(alcance / celda["publicaciones"]) if celda["publicaciones"] else 0,
            "interacciones": int(celda["interacciones"]),
            "interacciones_por_publicacion": redondea(
                celda["interacciones"] / celda["publicaciones"]
                if celda["publicaciones"] else 0.0, 0),
            "tasa_respuesta": redondea(
                celda["interacciones"] / alcance * 100.0 if alcance else 0.0),
        })

    salida.sort(key=lambda f: f["tasa_respuesta"], reverse=True)
    for posicion, fila in enumerate(salida):
        fila["es_mejor"] = posicion == 0
    return salida


def matriz_smart_timing(registros: Sequence[Dict[str, Any]],
                        top: int = 6) -> Dict[str, Any]:
    """Mapa de calor de respuesta organica por dia y hora.

    registros: publicaciones con dia (0=lunes), hora (0-23), interacciones y
    alcance. La celda guarda la tasa de respuesta = interacciones / alcance
    x 100, que es comparable entre horas con distinto volumen de publicacion.
    Devuelve la matriz 7x24, las mejores franjas y el maximo para escalar
    el degradado en el frontend.
    """
    acumulado = [[{"interacciones": 0.0, "alcance": 0.0, "publicaciones": 0}
                  for _ in range(24)] for _ in range(7)]

    for registro in registros:
        dia = int(registro.get("dia", 0)) % 7
        hora = int(registro.get("hora", 0)) % 24
        celda = acumulado[dia][hora]
        celda["interacciones"] += float(registro.get("interacciones", 0) or 0)
        celda["alcance"] += float(registro.get("alcance", 0) or 0)
        celda["publicaciones"] += 1

    matriz: List[List[float]] = []
    planas: List[Dict[str, Any]] = []
    for dia in range(7):
        fila: List[float] = []
        for hora in range(24):
            celda = acumulado[dia][hora]
            tasa = (celda["interacciones"] / celda["alcance"] * 100.0
                    if celda["alcance"] else 0.0)
            tasa = redondea(tasa)
            fila.append(tasa)
            if celda["publicaciones"]:
                planas.append({
                    "dia": dia,
                    "dia_nombre": DIAS_SEMANA[dia],
                    "hora": hora,
                    "tasa_respuesta": tasa,
                    "publicaciones": celda["publicaciones"],
                    "interacciones": int(celda["interacciones"]),
                })
        matriz.append(fila)

    planas.sort(key=lambda c: c["tasa_respuesta"], reverse=True)
    maximo = max((c["tasa_respuesta"] for c in planas), default=0.0)

    return {
        "dias": list(DIAS_SEMANA),
        "matriz": matriz,
        "maximo": maximo,
        "publicaciones_analizadas": len(registros),
        "mejores_franjas": planas[:top],
        "franjas": resume_franjas(registros),
    }


def detecta_anomalias(serie: Sequence[float], umbral_z: float = 2.8,
                      minimo_absoluto: float = 25.0,
                      salto_minimo: float = 2.0) -> List[Dict[str, Any]]:
    """Picos atipicos en una serie diaria mediante puntuacion z.

    Se usa sobre el volumen de comentarios negativos: un pico que se aparta
    mas de `umbral_z` desviaciones de la media reciente no es conversacion
    organica, es un evento. La media y la desviacion se calculan excluyendo
    el punto evaluado para que un pico muy grande no oculte su propia anomalia.
    """
    valores = [float(v or 0) for v in serie]
    if len(valores) < 5:
        return []

    picos: List[Dict[str, Any]] = []
    for indice, valor in enumerate(valores):
        resto = valores[:indice] + valores[indice + 1:]
        media = statistics.fmean(resto)
        desviacion = statistics.pstdev(resto)
        if desviacion <= 0:
            continue
        z = (valor - media) / desviacion
        if (z >= umbral_z and valor >= minimo_absoluto
                and valor >= media * salto_minimo):
            picos.append({
                "indice": indice,
                "valor": redondea(valor, 0),
                "media_referencia": redondea(media, 0),
                "z": redondea(z),
            })
    return picos


def construye_alertas(
    topicos: Sequence[Dict[str, Any]],
    fechas: Sequence[str],
    umbral_z: float = 2.8,
    umbral_cuentas_nuevas_pct: float = 35.0,
) -> List[Dict[str, Any]]:
    """Alertas tempranas de crisis tematica y de ataque coordinado.

    Cruza tres senales por topico:
      - pico atipico de menciones negativas (puntuacion z);
      - proporcion de cuentas recien creadas entre quienes comentan;
      - favorabilidad neta del tema.
    La severidad sube cuando coinciden el pico y las cuentas nuevas: ese
    patron distingue una crisis real de una campana de descalificacion.
    """
    alertas: List[Dict[str, Any]] = []

    for topico in topicos:
        serie = topico.get("serie_negativas", [])
        picos = detecta_anomalias(serie, umbral_z)
        cuentas_nuevas = float(topico.get("cuentas_nuevas_pct", 0.0) or 0.0)
        nfs = float(topico.get("nfs", 0.0) or 0.0)

        if not picos and cuentas_nuevas < umbral_cuentas_nuevas_pct:
            continue

        pico = max(picos, key=lambda p: p["z"]) if picos else None
        fecha = (fechas[pico["indice"]] if pico and pico["indice"] < len(fechas)
                 else (fechas[-1] if fechas else None))

        coordinado = bool(pico) and cuentas_nuevas >= umbral_cuentas_nuevas_pct
        if coordinado:
            tipo = "ataque_coordinado"
            severidad = "alta"
            titulo = "Posible ataque coordinado en " + topico["tema"]
        elif pico:
            tipo = "crisis_tematica"
            severidad = "alta" if nfs <= -25 else "media"
            titulo = "Pico de conversación negativa en " + topico["tema"]
        else:
            tipo = "anomalia_cuentas"
            severidad = "media"
            titulo = "Concentración de cuentas recién creadas en " + topico["tema"]

        evidencia = []
        if pico:
            evidencia.append(
                f"{int(pico['valor'])} menciones negativas contra una media de "
                f"{int(pico['media_referencia'])} (z = {pico['z']})"
            )
        if cuentas_nuevas:
            evidencia.append(
                f"{redondea(cuentas_nuevas)}% de las cuentas que comentan se "
                f"crearon en los últimos 30 días"
            )
        evidencia.append(f"Favorabilidad neta del tema: {redondea(nfs)}")

        alertas.append({
            "tipo": tipo,
            "severidad": severidad,
            "titulo": titulo,
            "tema": topico["tema"],
            "fecha": fecha,
            "cuentas_nuevas_pct": redondea(cuentas_nuevas),
            "z": pico["z"] if pico else None,
            "evidencia": evidencia,
        })

    orden = {"alta": 0, "media": 1, "baja": 2}
    alertas.sort(key=lambda a: (orden.get(a["severidad"], 3), -(a["z"] or 0)))
    return alertas


def construye_fiscalizacion(tope_mxn: float, devengado_sif_mxn: float,
                            gasto_ads_mxn: float) -> Dict[str, Any]:
    """Semaforo de fiscalizacion: gasto devengado reportado al SIF contra el
    tope oficial de campana, con la pauta digital como componente auditable."""
    tope = float(tope_mxn or 0)
    devengado = float(devengado_sif_mxn or 0)
    uso = (devengado / tope * 100.0) if tope else 0.0
    if uso >= 90:
        semaforo = "rojo"
    elif uso >= 75:
        semaforo = "ambar"
    else:
        semaforo = "verde"
    return {
        "tope_campana_mxn": redondea(tope, 0),
        "devengado_sif_mxn": redondea(devengado, 0),
        "disponible_mxn": redondea(max(tope - devengado, 0.0), 0),
        "uso_tope_pct": redondea(uso),
        "pauta_digital_mxn": redondea(gasto_ads_mxn, 0),
        "pauta_sobre_devengado_pct": redondea(
            gasto_ads_mxn / devengado * 100.0 if devengado else 0.0
        ),
        "semaforo": semaforo,
    }


# =====================================================================
# 6. ENSAMBLADO DEL JSON MAESTRO
# =====================================================================

def construye_eleccion(
    identidad: Dict[str, Any],
    secciones: Sequence[Dict[str, Any]],
    redes: Dict[str, Any],
    parametros: ParametrosCalculo,
) -> Dict[str, Any]:
    """Arma el objeto de una eleccion con el esquema que espera el frontend."""
    indicadores = resume_indicadores(secciones)
    territorio = dict(identidad.get("territorio", {}))
    candidato = dict(identidad.get("candidato", {}))

    territorio.setdefault("lista_nominal", indicadores["lista_nominal_total"])
    territorio["secciones_totales"] = len(secciones)
    territorio.setdefault("participacion_historica_pct", redondea(
        statistics.fmean([s["participacion_media_pct"] for s in secciones]) if secciones else 0.0
    ))

    candidato.setdefault("meta_votos", indicadores["target_total"])
    candidato.setdefault("voto_duro_historico", int(
        territorio["lista_nominal"] * indicadores["ftn_promedio"] / 100.0
        * (100.0 - parametros.abstencion_estimada_pct) / 100.0
    ))

    kpis_redes = dict(redes.get("kpis", {}))
    kpis_redes.setdefault("nfs", calcula_nfs(
        kpis_redes.get("sentimiento_positivo_pct", 0.0),
        kpis_redes.get("sentimiento_negativo_pct", 0.0),
    ))

    fiscalizacion = identidad.get("fiscalizacion") or construye_fiscalizacion(
        candidato.get("tope_gastos_campana_mxn", 0),
        identidad.get("devengado_sif_mxn", 0),
        kpis_redes.get("gasto_ads_acumulado_mxn", 0),
    )

    return {
        "eleccion_id": identidad["eleccion_id"],
        "cargo": identidad["cargo"],
        "fecha_jornada": identidad.get("fecha_jornada"),
        "fecha_corte": identidad.get("fecha_corte", hoy_iso()),
        "territorio": territorio,
        "candidato": candidato,
        "parametros": asdict(parametros),
        "indicadores": indicadores,
        "procedencia": identidad.get("procedencia", {}),
        "fiscalizacion": fiscalizacion,
        "secciones": list(secciones),
        "redes": {
            "kpis": kpis_redes,
            "serie_tiempo": redes.get("serie_tiempo", []),
            "competidores": redes.get("competidores", []),
            "benchmark": redes.get("benchmark", []),
            "smart_timing": redes.get("smart_timing", {}),
            "alertas": redes.get("alertas", []),
            "topicos": redes.get("topicos", []),
            "origen_serie": redes.get("origen_serie"),
        },
    }


#: Por encima de este tamano el JSON se escribe compacto: con 3 mil secciones
#: la sangria agrega cerca de un tercio del peso sin aportar legibilidad real.
UMBRAL_SANGRIA_BYTES = 1_200_000


def resume_procedencia(elecciones: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Consolida la procedencia de todas las elecciones del paquete.

    Si un campo es oficial en todas, se declara oficial; basta con que una
    eleccion lo traiga modelado para que el paquete entero lo declare asi.
    """
    campos = ("lista_nominal", "secciones", "resultados_electorales",
              "tope_gastos_campana", "redes_sociales")
    jerarquia = {"oficial": 0, "estimado": 1, "modelado": 2}
    resumen: Dict[str, Any] = {}

    for campo in campos:
        peor = None
        fuente = None
        for eleccion in elecciones:
            bloque = (eleccion.get("procedencia") or {}).get(campo) or {}
            estado = bloque.get("estado", "modelado")
            if peor is None or jerarquia.get(estado, 3) > jerarquia.get(peor, 3):
                peor = estado
                fuente = bloque.get("fuente")
        resumen[campo] = {"estado": peor or "modelado", "fuente": fuente}

    resumen["completo"] = all(
        resumen[c]["estado"] == "oficial" for c in campos
    )
    return resumen


def _bloque_meta(fuente_primaria: str, elecciones: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "procedencia": resume_procedencia(elecciones),
        "version_esquema": VERSION_ESQUEMA,
        "generado_en": ahora_iso(),
        "generador": "etl_electoral_sync.py",
        "marca": "Consensus Estrategia",
        "fuente_primaria": fuente_primaria,
        "elecciones_incluidas": len(elecciones),
        "secciones_incluidas": sum(len(e.get("secciones", [])) for e in elecciones),
    }


def _vuelca(paquete: Dict[str, Any], ruta: str) -> str:
    compacto = json.dumps(paquete, ensure_ascii=False, separators=(",", ":"))
    contenido = (json.dumps(paquete, ensure_ascii=False, indent=2)
                 if len(compacto) < UMBRAL_SANGRIA_BYTES else compacto)
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(contenido)
    return ruta


def escribe_json_maestro(elecciones: Sequence[Dict[str, Any]], ruta: str,
                         fuente_primaria: str = "INE / IEPC Jalisco") -> str:
    return _vuelca({
        "meta": _bloque_meta(fuente_primaria, elecciones),
        "elecciones": list(elecciones),
    }, ruta)


def construye_respaldo(elecciones: Sequence[Dict[str, Any]],
                       secciones_por_eleccion: int = 40) -> List[Dict[str, Any]]:
    """Version reducida del paquete para el respaldo embebido del navegador.

    Conserva intactos los indicadores agregados, la fiscalizacion y la suite
    digital —que es lo que se lee en pantalla— y recorta el detalle seccional
    a una muestra estratificada por clasificacion, para que el mosaico y la
    tabla sigan mostrando las tres categorias sin arrastrar dos megabytes.
    """
    reducidas: List[Dict[str, Any]] = []

    for eleccion in elecciones:
        copia = dict(eleccion)
        secciones = eleccion.get("secciones", [])

        # Muestra proporcional: cada clasificacion aporta segun su peso real.
        por_clase: Dict[str, List[Dict[str, Any]]] = {}
        for seccion in secciones:
            por_clase.setdefault(seccion["clasificacion"], []).append(seccion)

        muestra: List[Dict[str, Any]] = []
        for clase, grupo in por_clase.items():
            cuota = max(1, round(len(grupo) / max(len(secciones), 1)
                                 * secciones_por_eleccion))
            ordenado = sorted(grupo, key=lambda s: s["target_movilizacion"],
                              reverse=True)
            muestra.extend(ordenado[:cuota])

        muestra.sort(key=lambda s: s["seccion"])
        copia["secciones"] = muestra
        copia["muestra"] = {
            "es_muestra": True,
            "secciones_incluidas": len(muestra),
            "secciones_totales": len(secciones),
            "criterio": ("Muestra proporcional por clasificacion, ordenada por "
                         "target de movilizacion. Los indicadores agregados "
                         "corresponden al universo completo."),
        }
        reducidas.append(copia)

    return reducidas


def escribe_respaldo_js(elecciones: Sequence[Dict[str, Any]], ruta: str,
                        fuente_primaria: str = "INE / IEPC Jalisco") -> str:
    """Escribe `data_fallback.js`, que el tablero carga cuando el navegador
    bloquea la lectura del JSON (por ejemplo al abrir el archivo con doble
    clic, bajo el protocolo file://)."""
    paquete = {
        "meta": dict(_bloque_meta(fuente_primaria, elecciones),
                     **{"es_respaldo": True}),
        "elecciones": construye_respaldo(elecciones),
    }
    contenido = (
        "/* Respaldo embebido del tablero de Consensus Estrategia.\n"
        "   Generado por etl_electoral_sync.py --respaldo. No editar a mano.\n"
        "   Contiene una muestra seccional; el paquete completo vive en\n"
        "   electoral_master_data.json y requiere servirse por HTTP. */\n"
        "window.DATA_FALLBACK = "
        + json.dumps(paquete, ensure_ascii=False, separators=(",", ":"))
        + ";\n"
    )
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(contenido)
    return ruta


def escribe_por_eleccion(elecciones: Sequence[Dict[str, Any]], carpeta: str,
                         fuente_primaria: str = "INE / IEPC Jalisco") -> List[str]:
    """Escribe un archivo por eleccion mas un indice ligero.

    Es la ruta recomendada en produccion: el tablero carga el indice, que pesa
    unos kilobytes, y baja el detalle seccional solo del territorio abierto.
    """
    os.makedirs(carpeta, exist_ok=True)
    rutas: List[str] = []
    indice: List[Dict[str, Any]] = []

    for eleccion in elecciones:
        nombre = eleccion["eleccion_id"] + ".json"
        rutas.append(_vuelca({
            "meta": _bloque_meta(fuente_primaria, [eleccion]),
            "elecciones": [eleccion],
        }, os.path.join(carpeta, nombre)))

        indice.append({
            "eleccion_id": eleccion["eleccion_id"],
            "cargo": eleccion["cargo"],
            "territorio": {
                k: eleccion["territorio"].get(k)
                for k in ("entidad", "municipio", "distrito", "lista_nominal",
                          "secciones_totales")
            },
            "coalicion": (eleccion.get("candidato") or {}).get("coalicion"),
            "indicadores": eleccion.get("indicadores", {}),
            "archivo": nombre,
        })

    rutas.append(_vuelca({
        "meta": _bloque_meta(fuente_primaria, elecciones),
        "indice": indice,
    }, os.path.join(carpeta, "indice.json")))
    return rutas


# =====================================================================
# 7. PROCESAMIENTO DESDE ARCHIVO DE CONFIGURACION
# =====================================================================

def procesa_configuracion(ruta_config: str, con_red: bool = False) -> List[Dict[str, Any]]:
    """Lee un JSON de configuracion con una o varias elecciones (multi-tenant)
    y devuelve la lista de paquetes listos para serializar.

    Estructura esperada de cada entrada:
      {
        "eleccion_id": "JAL-MUN-TONALA-2027",
        "cargo": "Presidencia Municipal",
        "fecha_jornada": "2027-06-06",
        "territorio": {"entidad": "Jalisco", "municipio": "Tonala"},
        "candidato": {"nombre": "...", "coalicion": "..."},
        "parametros": {"abstencion_estimada_pct": 52.0},
        "fuentes": [
          {"anio": 2024, "ruta": "computos_2024.csv", "mapeo": "IEPC_JALISCO",
           "coalicion": ["PAN", "PRI", "PRD"],
           "filtro_columna": "MUNICIPIO", "filtro_valor": "TONALA"}
        ],
        "redes": {"pagina_id": "...", "termino_busqueda": "Nombre Candidato",
                  "competidores": [...], "topicos": [...],
                  "kpis": {"sentimiento_positivo_pct": 61.0,
                           "sentimiento_negativo_pct": 22.5}}
      }
    """
    with open(ruta_config, "r", encoding="utf-8") as fh:
        config = json.load(fh)

    entradas = config if isinstance(config, list) else config.get("elecciones", [config])
    resultado: List[Dict[str, Any]] = []

    for entrada in entradas:
        parametros = ParametrosCalculo(**entrada.get("parametros", {}))
        fuentes = [FuenteEleccion(**f) for f in entrada.get("fuentes", [])]
        secciones = construye_secciones(fuentes, parametros) if fuentes else []

        conf_redes = entrada.get("redes", {})
        redes = {
            "kpis": dict(conf_redes.get("kpis", {})),
            "serie_tiempo": conf_redes.get("serie_tiempo", []),
            "competidores": conf_redes.get("competidores", []),
            "topicos": conf_redes.get("topicos", []),
        }

        if con_red:
            termino = conf_redes.get("termino_busqueda")
            if termino:
                cliente_ads = MetaAdLibraryClient(pais=conf_redes.get("pais", "MX"))
                try:
                    anuncios = cliente_ads.busca_anuncios(termino)
                    resumen = MetaAdLibraryClient.resume_gasto(anuncios)
                    redes["kpis"]["gasto_ads_acumulado_mxn"] = resumen["gasto_estimado_mxn"]
                    redes["kpis"]["anuncios_activos"] = resumen["anuncios_activos"]
                    redes["auditoria_pauta"] = resumen
                except RuntimeError as exc:
                    print(f"[ads] {entrada.get('eleccion_id')}: {exc}", file=sys.stderr)

            pagina = conf_redes.get("pagina_id")
            if pagina:
                cliente_insights = MetaGraphInsightsClient(page_id=pagina)
                try:
                    redes["serie_tiempo"] = cliente_insights.serie_insights(
                        dias=conf_redes.get("dias_serie", 30)
                    )
                except RuntimeError as exc:
                    print(f"[insights] {entrada.get('eleccion_id')}: {exc}", file=sys.stderr)

        if not redes["serie_tiempo"]:
            redes["serie_tiempo"] = MetaGraphInsightsClient.genera_mock(
                dias=conf_redes.get("dias_serie", 30),
                audiencia_inicial=int(conf_redes.get("audiencia_inicial", 78000)),
            )
            redes["origen_serie"] = "mock"

        ultima = redes["serie_tiempo"][-1] if redes["serie_tiempo"] else {}
        redes["kpis"].setdefault("audiencia_total", ultima.get("audiencia", 0))
        redes["kpis"].setdefault("engagement_promedio", redondea(
            statistics.fmean([p["engagement_rate"] for p in redes["serie_tiempo"]])
            if redes["serie_tiempo"] else 0.0
        ))
        redes["kpis"].setdefault("gasto_ads_acumulado_mxn", redondea(
            sum(p.get("gasto_ads_mxn", 0.0) for p in redes["serie_tiempo"]), 0
        ))

        # --- Suite digital: misma maquinaria que usa el generador demo ------
        publicaciones = conf_redes.get("publicaciones", [])
        if publicaciones:
            redes["smart_timing"] = matriz_smart_timing(publicaciones)

        fechas_serie = [p.get("fecha") for p in redes["serie_tiempo"]]
        if any(t.get("serie_negativas") for t in redes["topicos"]):
            redes["alertas"] = construye_alertas(redes["topicos"], fechas_serie)

        actores = conf_redes.get("actores")
        if not actores and redes["competidores"]:
            # Compatibilidad: si solo hay competidores en formato simple, se
            # arma el benchmark con la candidatura propia al frente.
            actores = [{
                "nombre": (entrada.get("candidato") or {}).get("nombre", "Candidatura propia"),
                "coalicion": (entrada.get("candidato") or {}).get("coalicion", ""),
                "es_propio": True,
                "seguidores": redes["kpis"].get("audiencia_total", 0),
                "crecimiento_7d": sum(
                    p.get("nuevos_seguidores", 0) for p in redes["serie_tiempo"][-7:]),
                "publicaciones_7d": conf_redes.get("publicaciones_7d", len(publicaciones[-7:]) or 0),
                # Si la configuracion no trae interacciones semanales, se
                # derivan del engagement medio sobre la audiencia. La ventana
                # es la misma que la de los rivales (siete dias), de modo que
                # la tasa del benchmark queda comparable entre actores.
                "interacciones_7d": conf_redes.get(
                    "interacciones_7d",
                    int(redes["kpis"].get("audiencia_total", 0)
                        * redes["kpis"].get("engagement_promedio", 0.0) / 100.0)
                ),
                "menciones_30d": sum(t.get("menciones", 0) for t in redes["topicos"]),
                "gasto_ads_30d_mxn": redes["kpis"].get("gasto_ads_acumulado_mxn", 0),
                "alcance_30d": sum(p.get("alcance", 0) for p in redes["serie_tiempo"]),
                "nfs": redes["kpis"].get("nfs", 0.0),
                "anuncios_activos": redes["kpis"].get("anuncios_activos", 0),
                "temas_pauta": conf_redes.get("temas_pauta", []),
                "publicaciones": publicaciones,
            }] + [{
                "nombre": c.get("nombre"),
                "coalicion": c.get("coalicion", ""),
                "es_propio": False,
                "seguidores": c.get("audiencia", 0),
                "crecimiento_7d": c.get("crecimiento_7d", 0),
                "publicaciones_7d": c.get("publicaciones_7d", 0),
                "interacciones_7d": c.get("interacciones_7d", 0),
                "menciones_30d": c.get("menciones_30d", 0),
                "gasto_ads_30d_mxn": c.get("gasto_ads_mxn", 0),
                "alcance_30d": c.get("alcance_30d", 0),
                "nfs": c.get("nfs", 0.0),
                "anuncios_activos": c.get("anuncios_activos", 0),
                "temas_pauta": c.get("temas_pauta", []),
                "publicaciones": c.get("publicaciones", []),
            } for c in redes["competidores"]]

        if actores:
            redes["benchmark"] = construye_benchmark(actores)
            propio = next((b for b in redes["benchmark"] if b["es_propio"]), None)
            if propio:
                redes["kpis"].setdefault("share_of_voice_pct", propio["share_of_voice_pct"])
                redes["kpis"].setdefault("cpm_mxn", propio["cpm_mxn"])


        # La procedencia declarada por la configuracion manda; si no viene,
        # se deduce de lo que realmente se cargo en esta corrida.
        entrada.setdefault("procedencia", {
            "lista_nominal": {
                "estado": "oficial" if secciones else "sin dato",
                "fuente": entrada.get("fuente_lista_nominal",
                                      "Computos cargados con --config"),
            },
            "secciones": {
                "estado": "oficial" if secciones else "sin dato",
                "fuente": "Conteo real de secciones en los computos cargados",
            },
            "resultados_electorales": {
                "estado": "oficial" if fuentes else "sin dato",
                "fuente": entrada.get(
                    "fuente_computos",
                    "Computos cargados desde " + ", ".join(
                        str(f.ruta) for f in fuentes) if fuentes else "Sin computos"),
            },
            "tope_gastos_campana": {
                "estado": "oficial" if (entrada.get("candidato") or {}).get(
                    "tope_gastos_campana_mxn") else "sin dato",
                "fuente": entrada.get("fuente_tope", "Declarado en la configuracion"),
            },
            "redes_sociales": {
                "estado": "oficial" if con_red and redes.get("origen_serie") != "mock"
                          else "modelado",
                "fuente": ("Meta Ad Library API y Graph API" if con_red
                           else "Pendiente de conectar las APIs de Meta"),
            },
        })
        resultado.append(construye_eleccion(entrada, secciones, redes, parametros))

    return resultado


# =====================================================================
# 8. CATALOGO TERRITORIAL Y GENERADOR DE DEMOSTRACION
# =====================================================================

#: Proxy para estimar el tope de gastos de campana mientras no se carga el
#: acuerdo vigente del IEPC. NO es el dato oficial: el tope real se fija por
#: acuerdo del Consejo General y debe sustituirse en la configuracion del
#: cliente (`candidato.tope_gastos_campana_mxn`).
FACTOR_TOPE_PROXY_MXN_POR_ELECTOR = 8.50

#: Promedio de electores por seccion en la Zona Metropolitana de Guadalajara,
#: usado solo para estimar cuantas secciones tiene cada municipio cuando aun
#: no se han cargado los computos. Al correr el pipeline con `--config`, el
#: numero de secciones sale de los propios computos y esta constante deja de
#: intervenir.
ELECTORES_POR_SECCION_AMG = 1_350

#: Corte de la Lista Nominal usado en el catalogo territorial.
FUENTE_LISTA_NOMINAL = "DERFE-INE, Lista Nominal al 29 de enero de 2026"


@dataclass
class PerfilMunicipal:
    """Parametros de calibracion de un municipio para el paquete de demostracion.

    `lista_nominal` y `secciones` son ordenes de magnitud declarados para
    dimensionar el tablero; el corte oficial se toma del padron del INE y de
    los computos del IEPC al correr el pipeline con `--config`.
    """
    clave: str
    municipio: str
    lista_nominal: int
    secciones: int
    fuerza_propia_pct: float
    competitividad: float          # dispersion del margen: a mayor valor, mas swing
    participacion_media_pct: float
    audiencia_inicial: int
    coalicion: str
    partidos: Tuple[str, ...]
    rivales: Tuple[Tuple[str, str], ...]
    temas: Tuple[str, ...]
    nota: str
    #: Procedencia de `lista_nominal`. El resto de los campos del perfil son
    #: parametros de modelado, no cifras oficiales.
    fuente_lista_nominal: str = FUENTE_LISTA_NOMINAL

    @property
    def secciones_estimadas(self) -> int:
        """Secciones estimadas a partir de la lista nominal oficial."""
        return max(1, round(self.lista_nominal / ELECTORES_POR_SECCION_AMG))


CATALOGO_JALISCO: Tuple[PerfilMunicipal, ...] = (
    PerfilMunicipal(
        clave="GDL", municipio="Guadalajara", lista_nominal=1_228_660, secciones=910,
        fuerza_propia_pct=34.5, competitividad=9.5, participacion_media_pct=52.0,
        audiencia_inicial=146_000, coalicion="Coalición opositora",
        partidos=("PAN", "PRI", "PRD"),
        rivales=(("Morena Guadalajara", "Morena-PT-PVEM"), ("Movimiento Ciudadano GDL", "MC")),
        temas=("Inseguridad", "Agua y drenaje", "Baches y pavimento",
               "Movilidad y transporte", "Imagen pública", "Acusaciones personales"),
        nota="Capital estatal, competencia cerrada entre Morena y MC",
    ),
    PerfilMunicipal(
        clave="ZAP", municipio="Zapopan", lista_nominal=1_143_381, secciones=847,
        fuerza_propia_pct=38.0, competitividad=7.5, participacion_media_pct=53.5,
        audiencia_inicial=132_000, coalicion="Coalición opositora",
        partidos=("PAN", "PRI", "PRD"),
        rivales=(("Movimiento Ciudadano Zapopan", "MC"), ("Morena Zapopan", "Morena-PT-PVEM")),
        temas=("Inseguridad", "Agua y drenaje", "Desarrollo urbano",
               "Movilidad y transporte", "Imagen pública", "Acusaciones personales"),
        nota="Bastión metropolitano con mayor lista nominal del estado",
    ),
    PerfilMunicipal(
        clave="TLQ", municipio="San Pedro Tlaquepaque", lista_nominal=517_918, secciones=384,
        fuerza_propia_pct=33.0, competitividad=10.5, participacion_media_pct=48.5,
        audiencia_inicial=61_000, coalicion="Coalición opositora",
        partidos=("PAN", "PRI", "PRD"),
        rivales=(("Morena Tlaquepaque", "Morena-PT-PVEM"), ("Movimiento Ciudadano TLQ", "MC")),
        temas=("Inseguridad", "Agua y drenaje", "Servicios públicos",
               "Baches y pavimento", "Imagen pública", "Acusaciones personales"),
        nota="Zona altamente disputada en el corredor sur del AMG",
    ),
    PerfilMunicipal(
        clave="TLJ", municipio="Tlajomulco de Zúñiga", lista_nominal=471_548, secciones=349,
        fuerza_propia_pct=31.5, competitividad=8.5, participacion_media_pct=47.0,
        audiencia_inicial=54_000, coalicion="Coalición opositora",
        partidos=("PAN", "PRI", "PRD"),
        rivales=(("Movimiento Ciudadano Tlajomulco", "MC"), ("Morena Tlajomulco", "Morena-PT-PVEM")),
        temas=("Agua y drenaje", "Inseguridad", "Movilidad y transporte",
               "Servicios públicos", "Imagen pública", "Acusaciones personales"),
        nota="Crecimiento habitacional acelerado y presión por servicios",
    ),
    PerfilMunicipal(
        clave="TON", municipio="Tonalá", lista_nominal=395_879, secciones=293,
        fuerza_propia_pct=32.0, competitividad=9.0, participacion_media_pct=46.5,
        audiencia_inicial=48_000, coalicion="Coalición opositora",
        partidos=("PAN", "PRI", "PRD"),
        rivales=(("Morena Tonalá", "Morena-PT-PVEM"), ("Movimiento Ciudadano Tonalá", "MC")),
        temas=("Inseguridad", "Agua y drenaje", "Recolección de basura",
               "Baches y pavimento", "Imagen pública", "Acusaciones personales"),
        nota="Plaza con alta volatilidad seccional",
    ),
    PerfilMunicipal(
        clave="SAL", municipio="El Salto", lista_nominal=149_884, secciones=111,
        fuerza_propia_pct=30.0, competitividad=11.0, participacion_media_pct=45.0,
        audiencia_inicial=21_000, coalicion="Coalición opositora",
        partidos=("PAN", "PRI", "PRD"),
        rivales=(("Morena El Salto", "Morena-PT-PVEM"), ("Movimiento Ciudadano El Salto", "MC")),
        temas=("Contaminacion del rio", "Inseguridad", "Agua y drenaje",
               "Servicios públicos", "Imagen pública", "Acusaciones personales"),
        nota="Corredor industrial con agenda ambiental dominante",
    ),
    PerfilMunicipal(
        clave="PVR", municipio="Puerto Vallarta", lista_nominal=247_852, secciones=184,
        fuerza_propia_pct=35.5, competitividad=8.0, participacion_media_pct=49.0,
        audiencia_inicial=39_000, coalicion="Coalición opositora",
        partidos=("PAN", "PRI", "PRD"),
        rivales=(("Morena Puerto Vallarta", "Morena-PT-PVEM"), ("Movimiento Ciudadano PV", "MC")),
        temas=("Agua y drenaje", "Inseguridad", "Turismo y empleo",
               "Servicios públicos", "Imagen pública", "Acusaciones personales"),
        nota="Plaza turística con electorado flotante",
    ),
)


def procedencia_territorio(perfil: PerfilMunicipal) -> Dict[str, Any]:
    """Declara, campo por campo, de donde sale cada cifra del paquete.

    Es lo que permite que el pie del tablero diga la verdad sin que nadie
    tenga que acordarse: en cuanto se cargan los computos reales con
    `--config`, la procedencia deja de decir "modelado" para ese campo.
    """
    return {
        "lista_nominal": {
            "estado": "oficial",
            "fuente": perfil.fuente_lista_nominal,
        },
        "secciones": {
            "estado": "estimado",
            "fuente": (
                f"Estimacion: lista nominal entre {ELECTORES_POR_SECCION_AMG:,} "
                f"electores por seccion. Se sustituye por el conteo real al "
                f"cargar los computos del IEPC."
            ).replace(",", ","),
        },
        "resultados_electorales": {
            "estado": "modelado",
            "fuente": (
                "Pendiente de cargar computos oficiales del IEPC Jalisco "
                "(2018, 2021 y 2024) con --config."
            ),
        },
        "tope_gastos_campana": {
            "estado": "estimado",
            "fuente": (
                f"Proxy de {FACTOR_TOPE_PROXY_MXN_POR_ELECTOR} MXN por elector. "
                f"Sustituir por el acuerdo de topes del Consejo General del IEPC."
            ),
        },
        "redes_sociales": {
            "estado": "modelado",
            "fuente": (
                "Pendiente de conectar Meta Ad Library API y Graph API con "
                "las cuentas registradas en el CRM de precandidatos."
            ),
        },
    }


def _genera_secciones_sinteticas(perfil: PerfilMunicipal,
                                 parametros: ParametrosCalculo,
                                 rng: random.Random) -> List[Dict[str, Any]]:
    """Reparte la lista nominal del municipio entre sus secciones y calcula
    los mismos indicadores que produce la ruta real del pipeline."""
    promedio = perfil.lista_nominal / max(perfil.secciones, 1)
    secciones: List[Dict[str, Any]] = []

    for i in range(1, perfil.secciones + 1):
        lista_nominal = max(300, int(rng.gauss(promedio, promedio * 0.28)))
        base = rng.gauss(perfil.fuerza_propia_pct, perfil.competitividad * 0.75)

        resultados: Dict[str, Dict[str, float]] = {}
        hist_tuplas: List[Tuple[int, float, float]] = []
        margenes: List[float] = []

        for idx, anio in enumerate((2018, 2021, 2024)):
            participacion = limita_rango(
                rng.gauss(perfil.participacion_media_pct, 6.5), 26.0, 79.0)
            share_propio = limita_rango(
                base + rng.gauss(idx * 0.9, perfil.competitividad * 0.55), 5.0, 74.0)
            share_rival = limita_rango(
                rng.gauss(100.0 - base - 28.0, perfil.competitividad * 0.6), 5.0, 74.0)
            margen = share_propio - share_rival
            emitidos = int(lista_nominal * participacion / 100.0)

            resultados[str(anio)] = {
                "participacion_pct": redondea(participacion),
                "share_propio_pct": redondea(share_propio),
                "share_primer_lugar_pct": redondea(max(share_propio, share_rival)),
                "margen_pct": redondea(margen),
                "votos_propios": int(emitidos * share_propio / 100.0),
                "votos_emitidos": emitidos,
            }
            hist_tuplas.append((anio, share_propio, participacion))
            margenes.append(margen)

        swing = calcula_swing_index(margenes)
        share_objetivo = min(
            max(r["share_primer_lugar_pct"] for r in resultados.values())
            + parametros.margen_seguridad_pct, 85.0)

        secciones.append({
            "seccion": f"{i:04d}",
            "municipio": perfil.municipio,
            "distrito_local": 1 + (i % 9),
            "distrito_federal": 1 + (i % 6),
            "casillas": max(1, lista_nominal // 750),
            "lista_nominal": lista_nominal,
            "participacion_media_pct": redondea(
                statistics.fmean([h[2] for h in hist_tuplas])),
            "ftn": calcula_ftn(hist_tuplas, parametros.factor_recencia),
            "swing_index": swing,
            "margen_ultima_pct": redondea(margenes[-1]),
            "target_movilizacion": calcula_target_movilizacion(
                lista_nominal, share_objetivo, parametros.abstencion_estimada_pct),
            "clasificacion": clasifica_seccion(margenes[-1], swing, parametros),
            "historico": resultados,
        })

    return secciones


def limita_rango(valor: float, minimo: float, maximo: float) -> float:
    return max(minimo, min(maximo, valor))


def _genera_publicaciones(rng: random.Random, cantidad: int,
                          audiencia: int) -> List[Dict[str, Any]]:
    """Publicaciones sinteticas con hora, alcance e interacciones.

    La estructura horaria reproduce el comportamiento observable en cuentas
    politicas del AMG: repunte matutino al salir al trabajo, meseta de
    sobremesa y pico nocturno; el fin de semana se recorre y se aplana.
    """
    curva_habil = {6: 0.55, 7: 0.85, 8: 1.15, 9: 1.05, 10: 0.85, 11: 0.75,
                   12: 0.8, 13: 0.95, 14: 1.0, 15: 0.85, 16: 0.7, 17: 0.75,
                   18: 0.9, 19: 1.1, 20: 1.3, 21: 1.35, 22: 1.05, 23: 0.7}
    curva_finde = {8: 0.6, 9: 0.8, 10: 1.0, 11: 1.15, 12: 1.2, 13: 1.05,
                   14: 0.9, 15: 0.85, 16: 0.9, 17: 1.0, 18: 1.1, 19: 1.15,
                   20: 1.2, 21: 1.0, 22: 0.75}

    # Cada formato tiene su propio perfil: el Reel compra alcance barato pero
    # convierte menos; el carrusel alcanza a menos gente y responde mejor.
    perfiles_formato = {
        "Reel": {"peso": 0.42, "alcance": 1.55, "tasa": 1.00},
        "Imagen": {"peso": 0.24, "alcance": 0.75, "tasa": 1.05},
        "Carrusel": {"peso": 0.20, "alcance": 0.80, "tasa": 1.15},
        "Video largo": {"peso": 0.14, "alcance": 0.95, "tasa": 0.88},
    }
    nombres = list(perfiles_formato.keys())
    pesos = [perfiles_formato[n]["peso"] for n in nombres]

    # Cada cuenta tiene su propia mano: una rinde con video, otra con
    # carrusel. El sesgo se sortea una vez por cuenta, no por publicacion,
    # para que el analisis de formatos encuentre un patron y no ruido.
    sesgo_tasa = {n: rng.uniform(0.55, 1.62) for n in nombres}
    sesgo_mezcla = [p * rng.uniform(0.6, 1.5) for p in pesos]

    # Cada plaza tiene su propio reloj: en unas la conversacion despierta
    # temprano y en otras se concentra de noche.
    sesgo_franja = {"manana": rng.uniform(0.82, 1.28),
                    "tarde": rng.uniform(0.82, 1.22),
                    "noche": rng.uniform(0.82, 1.28)}

    def factor_franja(hora: int) -> float:
        if hora <= 11:
            return sesgo_franja["manana"]
        if hora <= 18:
            return sesgo_franja["tarde"]
        return sesgo_franja["noche"]

    publicaciones: List[Dict[str, Any]] = []
    for _ in range(cantidad):
        dia = rng.randrange(7)
        curva = curva_finde if dia >= 5 else curva_habil
        hora = rng.choice(list(curva.keys()))
        factor = curva[hora] * (0.88 if dia >= 5 else 1.0) * factor_franja(hora)
        formato = rng.choices(nombres, weights=sesgo_mezcla, k=1)[0]
        perfil = perfiles_formato[formato]

        alcance = max(500, int(audiencia * rng.uniform(0.18, 0.62) * perfil["alcance"]))
        tasa = limita_rango(
            rng.gauss(2.6 * factor * perfil["tasa"] * sesgo_tasa[formato], 0.55), 0.3, 9.0)
        publicaciones.append({
            "dia": dia,
            "hora": hora,
            "formato": formato,
            "alcance": alcance,
            "interacciones": int(alcance * tasa / 100.0),
        })
    return publicaciones


def _genera_topicos(perfil: PerfilMunicipal, fechas: Sequence[str],
                    rng: random.Random) -> List[Dict[str, Any]]:
    """Topicos de conversacion con su serie diaria de menciones negativas.

    A un tema de cada municipio se le inyecta un evento: un pico de negativos
    sostenido dos dias con una proporcion alta de cuentas recien creadas. Es
    la firma que el detector de anomalias debe encontrar por su cuenta.
    """
    tema_atacado = rng.choice(perfil.temas[-2:])
    topicos: List[Dict[str, Any]] = []

    for tema in perfil.temas:
        es_critico = tema not in ("Imagen pública", "Turismo y empleo")
        nfs_base = rng.uniform(-42.0, -8.0) if es_critico else rng.uniform(2.0, 38.0)
        volumen = rng.randint(220, 1600) * (1 + perfil.lista_nominal // 600_000)

        media_negativa = volumen / len(fechas) * (0.62 if es_critico else 0.3)
        serie = [max(0, int(rng.gauss(media_negativa, media_negativa * 0.15)))
                 for _ in fechas]

        cuentas_nuevas = rng.uniform(6.0, 22.0)
        if tema == tema_atacado:
            golpe = rng.randrange(len(fechas) - 4, len(fechas) - 1)
            serie[golpe] = int(media_negativa * rng.uniform(4.2, 6.5))
            serie[golpe + 1] = int(media_negativa * rng.uniform(2.8, 4.0))
            cuentas_nuevas = rng.uniform(38.0, 64.0)
            nfs_base = min(nfs_base, -28.0)

        total_negativas = sum(serie)
        positivas = max(0, int(volumen - total_negativas))
        tendencia = ("al alza" if serie[-1] > statistics.fmean(serie[:-1])
                     else "a la baja" if serie[-1] < statistics.fmean(serie[:-1]) * 0.85
                     else "estable")

        topicos.append({
            "tema": tema,
            "menciones": int(volumen),
            "menciones_negativas": total_negativas,
            "menciones_positivas": positivas,
            "nfs": redondea(nfs_base),
            "cuentas_nuevas_pct": redondea(cuentas_nuevas),
            "es_critico": es_critico,
            "tendencia": tendencia,
            "serie_negativas": serie,
        })

    return topicos


def genera_demo(semilla: int = 20270606,
                catalogo: Sequence[PerfilMunicipal] = CATALOGO_JALISCO,
                incluir_distrito: bool = True) -> List[Dict[str, Any]]:
    """Paquete de demostracion con los siete municipios clave de Jalisco.

    Cada municipio se construye con la misma maquinaria que usa la ruta real:
    se generan las secciones, se calculan FTN, volatilidad y target, se arma
    el benchmark competitivo, se procesan las publicaciones para el mapa de
    mejores horas y se corre el detector de anomalias sobre la conversacion.
    Los insumos son sinteticos; los calculos no.
    """
    elecciones: List[Dict[str, Any]] = []

    for indice, perfil in enumerate(catalogo):
        rng = random.Random(semilla + indice * 977)
        parametros = ParametrosCalculo(
            abstencion_estimada_pct=redondea(100.0 - perfil.participacion_media_pct)
        )

        secciones = _genera_secciones_sinteticas(perfil, parametros, rng)

        # --- Serie diaria de audiencia y pauta -----------------------------
        serie = MetaGraphInsightsClient.genera_mock(
            dias=30, audiencia_inicial=perfil.audiencia_inicial,
            semilla=semilla + indice * 31,
        )
        fechas = [p["fecha"] for p in serie]
        audiencia_final = serie[-1]["audiencia"]
        gasto_30d = sum(p["gasto_ads_mxn"] for p in serie)
        alcance_30d = sum(p["alcance"] for p in serie)

        # --- Escucha: topicos, alertas y sentimiento -----------------------
        topicos = _genera_topicos(perfil, fechas, rng)
        alertas = construye_alertas(topicos, fechas)

        negativas = sum(t["menciones_negativas"] for t in topicos)
        positivas = sum(t["menciones_positivas"] for t in topicos)
        universo = max(negativas + positivas, 1)
        positivo_pct = redondea(positivas / universo * 100.0)
        negativo_pct = redondea(negativas / universo * 100.0)
        menciones_propias = universo

        # --- Publicaciones propias: alimentan formatos y mejores horas -----
        publicaciones = _genera_publicaciones(rng, 260, perfil.audiencia_inicial)
        smart_timing = matriz_smart_timing(publicaciones)

        # --- Benchmark competitivo ----------------------------------------
        actores = [{
            "nombre": "Candidatura Consensus",
            "coalicion": perfil.coalicion,
            "es_propio": True,
            "seguidores": audiencia_final,
            "crecimiento_7d": sum(p["nuevos_seguidores"] for p in serie[-7:]),
            "publicaciones_7d": rng.randint(18, 34),
            "interacciones_7d": int(audiencia_final * rng.uniform(0.022, 0.075)),
            "menciones_30d": menciones_propias,
            "gasto_ads_30d_mxn": gasto_30d,
            "alcance_30d": alcance_30d,
            "nfs": calcula_nfs(positivo_pct, negativo_pct),
            "anuncios_activos": rng.randint(9, 34),
            "temas_pauta": list(rng.sample(list(perfil.temas), 3)),
            "publicaciones": publicaciones,
        }]

        for nombre, coalicion in perfil.rivales:
            seguidores = int(audiencia_final * rng.uniform(0.55, 1.65))
            actores.append({
                "nombre": nombre,
                "coalicion": coalicion,
                "es_propio": False,
                "seguidores": seguidores,
                "crecimiento_7d": int(seguidores * rng.uniform(0.004, 0.028)),
                "publicaciones_7d": rng.randint(12, 42),
                "interacciones_7d": int(seguidores * rng.uniform(0.014, 0.068)),
                "menciones_30d": int(menciones_propias * rng.uniform(0.45, 1.5)),
                "gasto_ads_30d_mxn": redondea(gasto_30d * rng.uniform(0.6, 2.1), 0),
                "alcance_30d": int(alcance_30d * rng.uniform(0.5, 1.8)),
                "nfs": redondea(rng.uniform(-24.0, 30.0)),
                "anuncios_activos": rng.randint(5, 48),
                "temas_pauta": list(rng.sample(list(perfil.temas), 3)),
                "publicaciones": _genera_publicaciones(rng, 140, seguidores),
            })

        benchmark = construye_benchmark(actores)

        redes = {
            "kpis": {
                "audiencia_total": audiencia_final,
                "engagement_promedio": redondea(
                    statistics.fmean([p["engagement_rate"] for p in serie])),
                "sentimiento_positivo_pct": positivo_pct,
                "sentimiento_negativo_pct": negativo_pct,
                "nfs": calcula_nfs(positivo_pct, negativo_pct),
                "gasto_ads_acumulado_mxn": redondea(gasto_30d, 0),
                "anuncios_activos": rng.randint(9, 34),
                "alcance_30d": alcance_30d,
                "cpm_mxn": costo_por_mil_alcanzados(gasto_30d, alcance_30d),
                "share_of_voice_pct": next(
                    (b["share_of_voice_pct"] for b in benchmark if b["es_propio"]), 0.0),
            },
            "serie_tiempo": serie,
            "benchmark": benchmark,
            "competidores": [
                {
                    "nombre": b["nombre"],
                    "coalicion": b["coalicion"],
                    "audiencia": b["seguidores"],
                    "engagement_rate": b["engagement_rate"],
                    "gasto_ads_mxn": b["gasto_ads_30d_mxn"],
                    "nfs": b["nfs"],
                }
                for b in benchmark if not b["es_propio"]
            ],
            "smart_timing": smart_timing,
            "alertas": alertas,
            "topicos": topicos,
            "origen_serie": "mock",
        }

        # --- Fiscalizacion --------------------------------------------------
        tope = int(perfil.lista_nominal * FACTOR_TOPE_PROXY_MXN_POR_ELECTOR)
        devengado = redondea(tope * rng.uniform(0.46, 0.93), 0)

        identidad = {
            "eleccion_id": f"JAL-MUN-{perfil.clave}-2027",
            "cargo": "Presidencia Municipal",
            "fecha_jornada": "2027-06-06",
            "fecha_corte": hoy_iso(),
            "territorio": {
                "entidad": "Jalisco",
                "municipio": perfil.municipio,
                "distrito": None,
                "lista_nominal": perfil.lista_nominal,
                "participacion_historica_pct": perfil.participacion_media_pct,
                "nota": perfil.nota,
            },
            "candidato": {
                "nombre": "Candidatura Consensus",
                "coalicion": perfil.coalicion,
                "partidos": list(perfil.partidos),
                "tope_gastos_campana_mxn": tope,
            },
            "procedencia": procedencia_territorio(perfil),
            "fiscalizacion": construye_fiscalizacion(tope, devengado, gasto_30d),
        }

        elecciones.append(construye_eleccion(identidad, secciones, redes, parametros))

    if incluir_distrito:
        elecciones.append(_genera_distrito_demo(semilla))

    return elecciones


def _genera_distrito_demo(semilla: int) -> Dict[str, Any]:
    """Una diputacion local, para verificar que el selector de cargo cambia el
    universo territorial sin tocar la vista."""
    perfil = PerfilMunicipal(
        clave="DL08", municipio="Distrito local 8", lista_nominal=185_000, secciones=128,
        fuerza_propia_pct=36.5, competitividad=8.0, participacion_media_pct=48.0,
        audiencia_inicial=41_000, coalicion="Coalición estatal", partidos=("MC",),
        rivales=(("Morena Distrito 8", "Morena-PT-PVEM"), ("PAN Distrito 8", "PAN-PRI-PRD")),
        temas=("Inseguridad", "Agua y drenaje", "Servicios públicos",
               "Imagen pública", "Acusaciones personales"),
        nota="Distrito local de prueba para cargo legislativo",
    )
    rng = random.Random(semilla + 7717)
    parametros = ParametrosCalculo(
        abstencion_estimada_pct=redondea(100.0 - perfil.participacion_media_pct))

    secciones = _genera_secciones_sinteticas(perfil, parametros, rng)
    serie = MetaGraphInsightsClient.genera_mock(
        dias=30, audiencia_inicial=perfil.audiencia_inicial, semilla=semilla + 99)
    fechas = [p["fecha"] for p in serie]
    topicos = _genera_topicos(perfil, fechas, rng)
    alertas = construye_alertas(topicos, fechas)

    negativas = sum(t["menciones_negativas"] for t in topicos)
    positivas = sum(t["menciones_positivas"] for t in topicos)
    universo = max(negativas + positivas, 1)
    positivo_pct = redondea(positivas / universo * 100.0)
    negativo_pct = redondea(negativas / universo * 100.0)

    gasto_30d = sum(p["gasto_ads_mxn"] for p in serie)
    alcance_30d = sum(p["alcance"] for p in serie)
    audiencia_final = serie[-1]["audiencia"]

    publicaciones_propias = _genera_publicaciones(rng, 210, perfil.audiencia_inicial)
    actores = [{
        "nombre": "Candidatura Consensus", "coalicion": perfil.coalicion, "es_propio": True,
        "seguidores": audiencia_final,
        "crecimiento_7d": sum(p["nuevos_seguidores"] for p in serie[-7:]),
        "publicaciones_7d": rng.randint(14, 28),
        "interacciones_7d": int(audiencia_final * rng.uniform(0.020, 0.070)),
        "menciones_30d": universo, "gasto_ads_30d_mxn": gasto_30d,
        "alcance_30d": alcance_30d, "nfs": calcula_nfs(positivo_pct, negativo_pct),
        "anuncios_activos": rng.randint(6, 20),
        "temas_pauta": list(rng.sample(list(perfil.temas), 3)),
        "publicaciones": publicaciones_propias,
    }]
    for nombre, coalicion in perfil.rivales:
        seguidores = int(audiencia_final * rng.uniform(0.6, 1.5))
        actores.append({
            "nombre": nombre, "coalicion": coalicion, "es_propio": False,
            "seguidores": seguidores,
            "crecimiento_7d": int(seguidores * rng.uniform(0.005, 0.025)),
            "publicaciones_7d": rng.randint(10, 32),
            "interacciones_7d": int(seguidores * rng.uniform(0.013, 0.062)),
            "menciones_30d": int(universo * rng.uniform(0.5, 1.4)),
            "gasto_ads_30d_mxn": redondea(gasto_30d * rng.uniform(0.6, 1.9), 0),
            "alcance_30d": int(alcance_30d * rng.uniform(0.6, 1.6)),
            "nfs": redondea(rng.uniform(-20.0, 28.0)),
            "anuncios_activos": rng.randint(4, 30),
            "temas_pauta": list(rng.sample(list(perfil.temas), 3)),
            "publicaciones": _genera_publicaciones(rng, 120, seguidores),
        })
    benchmark = construye_benchmark(actores)
    tope = int(perfil.lista_nominal * FACTOR_TOPE_PROXY_MXN_POR_ELECTOR)
    devengado = redondea(tope * rng.uniform(0.5, 0.9), 0)

    identidad = {
        "eleccion_id": "JAL-DL-08-2027",
        "cargo": "Diputación Local",
        "fecha_jornada": "2027-06-06",
        "fecha_corte": hoy_iso(),
        "territorio": {
            "entidad": "Jalisco", "municipio": None, "distrito": perfil.municipio,
            "lista_nominal": perfil.lista_nominal,
            "participacion_historica_pct": perfil.participacion_media_pct,
            "nota": perfil.nota,
        },
        "candidato": {
            "nombre": "Candidatura Consensus", "coalicion": perfil.coalicion,
            "partidos": list(perfil.partidos), "tope_gastos_campana_mxn": tope,
        },
        "procedencia": procedencia_territorio(perfil),
        "fiscalizacion": construye_fiscalizacion(tope, devengado, gasto_30d),
    }

    redes = {
        "kpis": {
            "audiencia_total": audiencia_final,
            "engagement_promedio": redondea(
                statistics.fmean([p["engagement_rate"] for p in serie])),
            "sentimiento_positivo_pct": positivo_pct,
            "sentimiento_negativo_pct": negativo_pct,
            "nfs": calcula_nfs(positivo_pct, negativo_pct),
            "gasto_ads_acumulado_mxn": redondea(gasto_30d, 0),
            "anuncios_activos": rng.randint(6, 20),
            "alcance_30d": alcance_30d,
            "cpm_mxn": costo_por_mil_alcanzados(gasto_30d, alcance_30d),
            "share_of_voice_pct": next(
                (b["share_of_voice_pct"] for b in benchmark if b["es_propio"]), 0.0),
        },
        "serie_tiempo": serie,
        "benchmark": benchmark,
        "competidores": [
            {"nombre": b["nombre"], "coalicion": b["coalicion"], "audiencia": b["seguidores"],
             "engagement_rate": b["engagement_rate"], "gasto_ads_mxn": b["gasto_ads_30d_mxn"],
             "nfs": b["nfs"]}
            for b in benchmark if not b["es_propio"]
        ],
        "smart_timing": matriz_smart_timing(publicaciones_propias),
        "alertas": alertas,
        "topicos": topicos,
        "origen_serie": "mock",
    }

    return construye_eleccion(identidad, secciones, redes, parametros)



# =====================================================================
# 9. CLI
# =====================================================================

def construye_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="etl_electoral_sync",
        description="Pipeline electoral y digital de Consensus Estrategia.",
    )
    parser.add_argument("--config", help="Ruta del JSON de configuracion multi-eleccion.")
    parser.add_argument("--demo", action="store_true",
                        help="Genera un paquete sintetico sin archivos ni credenciales.")
    parser.add_argument("--con-red", action="store_true",
                        help="Consulta Meta Ad Library y Page Insights en vivo.")
    parser.add_argument("--salida", default="electoral_master_data.json",
                        help="Archivo JSON maestro de salida.")
    parser.add_argument("--semilla", type=int, default=20270606,
                        help="Semilla del generador de demostracion.")
    parser.add_argument("--respaldo", metavar="ARCHIVO", nargs="?",
                        const="data_fallback.js",
                        help="Escribe el respaldo embebido (data_fallback.js) que el "
                             "tablero usa cuando el navegador bloquea la lectura del JSON.")
    parser.add_argument("--por-eleccion", metavar="CARPETA",
                        help="Ademas del maestro, escribe un archivo por eleccion "
                             "y un indice ligero en esa carpeta.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = construye_parser().parse_args(argv)

    if not args.config and not args.demo:
        print("Indica --config <archivo.json> o --demo.", file=sys.stderr)
        return 2

    if args.demo:
        elecciones = genera_demo(semilla=args.semilla)
        fuente = "Lista Nominal DERFE-INE; resultados y redes en modelado"
    else:
        elecciones = procesa_configuracion(args.config, con_red=args.con_red)
        fuente = "INE / IEPC Jalisco"

    ruta = escribe_json_maestro(elecciones, args.salida, fuente_primaria=fuente)
    peso = os.path.getsize(ruta) / 1024.0

    if args.respaldo:
        ruta_respaldo = escribe_respaldo_js(elecciones, args.respaldo,
                                            fuente_primaria=fuente)
        peso_respaldo = os.path.getsize(ruta_respaldo) / 1024.0
        print(f"Respaldo embebido escrito en: {ruta_respaldo} ({peso_respaldo:,.0f} KB)")

    if args.por_eleccion:
        rutas = escribe_por_eleccion(elecciones, args.por_eleccion, fuente_primaria=fuente)
        print(f"Paquetes por eleccion escritos en: {args.por_eleccion} "
              f"({len(rutas)} archivos, indice incluido)")

    print(f"JSON maestro escrito en: {ruta} ({peso:,.0f} KB)")
    for eleccion in elecciones:
        ind = eleccion["indicadores"]
        print(
            f"  - {eleccion['eleccion_id']}: {eleccion['territorio']['secciones_totales']} secciones | "
            f"FTN {ind['ftn_promedio']}% | ganadas {ind['casillas_ganadas']} / "
            f"swing {ind['casillas_swing']} / riesgo {ind['casillas_riesgo']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
