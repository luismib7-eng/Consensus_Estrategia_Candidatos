# Plataforma Electoral Modular — Consensus Estrategia

Tablero multi-inquilino de inteligencia electoral y digital. La vista no conoce
el territorio: consume un paquete JSON con el esquema de
`electoral_master_data.json`, de modo que el mismo frontend sirve para una
alcaldía, un distrito local, uno federal o una senaduría.

## Archivos

| Archivo | Función |
| --- | --- |
| `etl_electoral_sync.py` | Ingesta de cómputos INE/IEPC, cálculo de indicadores por sección, conectores de Meta y generación del JSON maestro. Sin dependencias externas. |
| `electoral_master_data.json` | Paquete de datos que consume el tablero. Incluido ya generado en modo demostración. |
| `configuracion_ejemplo.json` | Plantilla de configuración por cliente (multi-inquilino). |
| `index.html` | Documento del dashboard ejecutivo. |
| `tablero_core.js` | Lógica de render, filtros, cartografía y gráficas. Desacoplada del origen de datos. |
| `estilos.css` | Tokens de marca, componentes y cartografía. |

## Puesta en marcha

```bash
# 1. Paquete de datos de demostración (sin credenciales ni archivos)
python3 etl_electoral_sync.py --demo --salida electoral_master_data.json

# 2. Servir por HTTP — abrir index.html con doble clic bloquea la lectura del JSON
python3 -m http.server 8080
# http://localhost:8080
```

## Procesamiento con datos reales

```bash
# Copiar configuracion_ejemplo.json, ajustar rutas de cómputos y siglas de coalición
python3 etl_electoral_sync.py --config configuracion_tonala.json

# Con consulta en vivo a Meta Ad Library y Page Insights
export META_ACCESS_TOKEN="EAAG..."
export META_PAGE_TOKEN="EAAG..."
python3 etl_electoral_sync.py --config configuracion_tonala.json --con-red
```

Si falta token o falla la API, el pipeline no se detiene: registra el error en
`stderr` y completa la serie de redes con el generador de estructura idéntica,
marcando `redes.origen_serie = "mock"`. El pie del tablero lo declara en pantalla.

## Indicadores

- **FTN (Fuerza Territorial Neta).** Votación histórica de la coalición ponderada
  por la participación de cada elección, con un factor de recencia configurable
  que privilegia la elección más reciente.
- **Swing Index.** Desviación estándar del margen entre primero y segundo lugar
  en las últimas tres elecciones. Alto = sección volátil.
- **Target de Movilización.** Votación proyectada desde la lista nominal y el
  umbral de abstención estimado, aplicando el porcentaje del primer lugar
  histórico más el margen de seguridad.
- **Clasificación.** `swing` si el margen es estrecho o la volatilidad supera el
  umbral; si no, `ganada` o `riesgo` según el signo del último margen.

## Cambiar de cliente

```html
<!-- Un archivo por inquilino -->
<script>Tablero.crear({ fuente: './clientes/tonala.json' });</script>

<!-- Endpoint del backend -->
<script>Tablero.crear({ fuente: '/api/v1/elecciones/JAL-DL-08-2027' });</script>

<!-- Objeto ya cargado, compatible con data.js -->
<script src="data.js"></script>
<script>Tablero.crear({ fuente: window.ELECTORAL_DATA });</script>
```

## Cartografía

Si `territorio.geojson_url` apunta a un GeoJSON de secciones, el tablero dibuja
el mapa en SVG con proyección equirectangular corregida por latitud y colorea
cada sección por su estatus competitivo. La propiedad de la sección se detecta
entre `seccion`, `SECCION`, `CVE_SECCION` y variantes. Sin GeoJSON, la vista
cae en el mosaico seccional, donde cada tesela es una sección y su tamaño
refleja la lista nominal.

## Fuentes oficiales

| Ámbito | Fuente |
| --- | --- |
| Cómputos federales e histórico | INE — Sistema de Consulta de Cómputos |
| Datos abiertos federales | INE — Datos Abiertos |
| Alcaldías y diputaciones locales en Jalisco | IEPC Jalisco — Cómputos |
| Resto de las entidades | OPL estatal correspondiente |
| Gasto en pauta política | Meta Ad Library API (`/ads_archive`) |
| Métricas orgánicas | Meta Graph API — Page Insights |

## Fiscalización

El semáforo de pauta compara el gasto estimado de la Ad Library contra
`candidato.tope_gastos_campana_mxn`. La Ad Library reporta rangos, así que el
pipeline conserva piso, techo y punto medio en `redes.auditoria_pauta` para
auditoría. Ese dato es indicativo y no sustituye al reporte del Sistema Integral
de Fiscalización del INE.
