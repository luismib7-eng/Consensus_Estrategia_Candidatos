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

VERSION_ESQUEMA = "1.0"
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

    return {
        "eleccion_id": identidad["eleccion_id"],
        "cargo": identidad["cargo"],
        "fecha_jornada": identidad.get("fecha_jornada"),
        "fecha_corte": identidad.get("fecha_corte", hoy_iso()),
        "territorio": territorio,
        "candidato": candidato,
        "parametros": asdict(parametros),
        "indicadores": indicadores,
        "secciones": list(secciones),
        "redes": {
            "kpis": kpis_redes,
            "serie_tiempo": redes.get("serie_tiempo", []),
            "competidores": redes.get("competidores", []),
            "topicos": redes.get("topicos", []),
        },
    }


def escribe_json_maestro(elecciones: Sequence[Dict[str, Any]], ruta: str,
                         fuente_primaria: str = "INE / IEPC Jalisco") -> str:
    paquete = {
        "meta": {
            "version_esquema": VERSION_ESQUEMA,
            "generado_en": ahora_iso(),
            "generador": "etl_electoral_sync.py",
            "marca": "Consensus Estrategia",
            "fuente_primaria": fuente_primaria,
        },
        "elecciones": list(elecciones),
    }
    with open(ruta, "w", encoding="utf-8") as fh:
        json.dump(paquete, fh, ensure_ascii=False, indent=2)
    return ruta


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

        resultado.append(construye_eleccion(entrada, secciones, redes, parametros))

    return resultado


# =====================================================================
# 8. GENERADOR DE DEMOSTRACION
# =====================================================================

def genera_demo(semilla: int = 20270606) -> List[Dict[str, Any]]:
    """Construye dos elecciones sinteticas (una alcaldia y un distrito local)
    con la misma estructura que produce el pipeline real. Sirve para levantar
    el dashboard sin credenciales ni archivos de computos."""
    rng = random.Random(semilla)

    plantillas = [
        {
            "eleccion_id": "JAL-MUN-TONALA-2027",
            "cargo": "Presidencia Municipal",
            "fecha_jornada": "2027-06-06",
            "territorio": {"entidad": "Jalisco", "municipio": "Tonala", "distrito": None},
            "candidato": {
                "nombre": "Candidatura en precampana",
                "coalicion": "Coalicion municipal",
                "partidos": ["PAN", "PRI", "PRD"],
                "tope_gastos_campana_mxn": 3_150_000,
            },
            "n_secciones": 210,
            "lista_por_seccion": (1200, 2600),
            "audiencia_inicial": 78000,
        },
        {
            "eleccion_id": "JAL-DL-08-2027",
            "cargo": "Diputacion Local",
            "fecha_jornada": "2027-06-06",
            "territorio": {"entidad": "Jalisco", "municipio": None, "distrito": "Distrito local 8"},
            "candidato": {
                "nombre": "Candidatura distrital",
                "coalicion": "Coalicion estatal",
                "partidos": ["MC"],
                "tope_gastos_campana_mxn": 1_480_000,
            },
            "n_secciones": 128,
            "lista_por_seccion": (900, 2100),
            "audiencia_inicial": 41000,
        },
    ]

    elecciones: List[Dict[str, Any]] = []
    for plantilla in plantillas:
        parametros = ParametrosCalculo()
        secciones: List[Dict[str, Any]] = []

        for i in range(1, plantilla["n_secciones"] + 1):
            lista_nominal = rng.randint(*plantilla["lista_por_seccion"])
            base = rng.gauss(36.0, 9.0)
            historico_anios = [2018, 2021, 2024]
            resultados: Dict[str, Dict[str, float]] = {}
            hist_tuplas: List[Tuple[int, float, float]] = []
            margenes: List[float] = []

            for idx, anio in enumerate(historico_anios):
                participacion = max(28.0, min(78.0, rng.gauss(48.0, 7.5)))
                share_propio = max(6.0, min(72.0, base + rng.gauss(idx * 1.5, 5.5)))
                share_rival = max(6.0, min(72.0, rng.gauss(34.0, 8.0)))
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

            ftn = calcula_ftn(hist_tuplas, parametros.factor_recencia)
            swing = calcula_swing_index(margenes)
            share_objetivo = min(
                max(r["share_primer_lugar_pct"] for r in resultados.values())
                + parametros.margen_seguridad_pct, 85.0
            )
            secciones.append({
                "seccion": f"{i:04d}",
                "municipio": plantilla["territorio"].get("municipio"),
                "distrito_local": 8 if plantilla["cargo"] == "Diputacion Local" else rng.randint(5, 12),
                "distrito_federal": rng.randint(7, 11),
                "casillas": max(1, lista_nominal // 750),
                "lista_nominal": lista_nominal,
                "participacion_media_pct": redondea(
                    statistics.fmean([h[2] for h in hist_tuplas])
                ),
                "ftn": ftn,
                "swing_index": swing,
                "margen_ultima_pct": redondea(margenes[-1]),
                "target_movilizacion": calcula_target_movilizacion(
                    lista_nominal, share_objetivo, parametros.abstencion_estimada_pct
                ),
                "clasificacion": clasifica_seccion(margenes[-1], swing, parametros),
                "historico": resultados,
            })

        serie = MetaGraphInsightsClient.genera_mock(
            dias=30,
            audiencia_inicial=plantilla["audiencia_inicial"],
            semilla=semilla + len(plantilla["eleccion_id"]),
        )
        positivos = redondea(rng.uniform(52.0, 68.0))
        negativos = redondea(rng.uniform(16.0, 28.0))

        redes = {
            "kpis": {
                "audiencia_total": serie[-1]["audiencia"],
                "engagement_promedio": redondea(
                    statistics.fmean([p["engagement_rate"] for p in serie])
                ),
                "sentimiento_positivo_pct": positivos,
                "sentimiento_negativo_pct": negativos,
                "nfs": calcula_nfs(positivos, negativos),
                "gasto_ads_acumulado_mxn": redondea(
                    sum(p["gasto_ads_mxn"] for p in serie), 0
                ),
                "anuncios_activos": rng.randint(8, 26),
            },
            "serie_tiempo": serie,
            "origen_serie": "mock",
            "competidores": [
                {
                    "nombre": nombre,
                    "coalicion": coalicion,
                    "audiencia": rng.randint(28000, 96000),
                    "engagement_rate": redondea(rng.uniform(2.1, 6.4)),
                    "gasto_ads_mxn": redondea(rng.uniform(120000, 480000), 0),
                    "nfs": redondea(rng.uniform(-18.0, 34.0)),
                }
                for nombre, coalicion in [
                    ("Contendiente A", "Partido en el gobierno"),
                    ("Contendiente B", "Coalicion opositora"),
                    ("Contendiente C", "Partido emergente"),
                ]
            ],
            "topicos": [
                {"tema": "Inseguridad", "menciones": rng.randint(900, 2400),
                 "nfs": redondea(rng.uniform(-45.0, -12.0)), "tendencia": "al alza"},
                {"tema": "Agua y drenaje", "menciones": rng.randint(600, 1800),
                 "nfs": redondea(rng.uniform(-38.0, -5.0)), "tendencia": "estable"},
                {"tema": "Servicios publicos", "menciones": rng.randint(400, 1500),
                 "nfs": redondea(rng.uniform(-20.0, 15.0)), "tendencia": "a la baja"},
                {"tema": "Imagen publica", "menciones": rng.randint(500, 2000),
                 "nfs": redondea(rng.uniform(5.0, 42.0)), "tendencia": "al alza"},
                {"tema": "Movilidad y transporte", "menciones": rng.randint(300, 1100),
                 "nfs": redondea(rng.uniform(-25.0, 8.0)), "tendencia": "estable"},
            ],
        }

        identidad = {k: plantilla[k] for k in
                     ("eleccion_id", "cargo", "fecha_jornada", "territorio", "candidato")}
        identidad["fecha_corte"] = hoy_iso()
        elecciones.append(construye_eleccion(identidad, secciones, redes, parametros))

    return elecciones


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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = construye_parser().parse_args(argv)

    if not args.config and not args.demo:
        print("Indica --config <archivo.json> o --demo.", file=sys.stderr)
        return 2

    if args.demo:
        elecciones = genera_demo(semilla=args.semilla)
        fuente = "Datos sinteticos de demostracion"
    else:
        elecciones = procesa_configuracion(args.config, con_red=args.con_red)
        fuente = "INE / IEPC Jalisco"

    ruta = escribe_json_maestro(elecciones, args.salida, fuente_primaria=fuente)

    print(f"JSON maestro escrito en: {ruta}")
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
