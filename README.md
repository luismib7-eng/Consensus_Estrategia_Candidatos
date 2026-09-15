# Plataforma Electoral Modular — Consensus Estrategia

Tablero multi-inquilino de inteligencia electoral y digital. La vista no conoce
el territorio: consume un paquete JSON con el esquema de
`electoral_master_data.json`, de modo que el mismo frontend sirve para una
alcaldía, un distrito local, uno federal o una senaduría.

## Archivos

| Archivo | Función |
| --- | --- |
| `etl_electoral_sync.py` | Ingesta de cómputos INE/IEPC, cálculo de indicadores por sección, conectores de Meta y generación del JSON maestro. Sin dependencias externas. |
| `electoral_master_data.json` | Paquete de datos que consume el tablero. Incluido ya generado con los 7 municipios. |
| `datos/` | Un archivo por elección más `indice.json`, para cargar solo el territorio abierto. |
| `configuracion_ejemplo.json` | Plantilla de configuración por cliente (multi-inquilino). |
| `index.html` | Documento del dashboard ejecutivo. |
| `tablero_core.js` | Lógica de render, filtros, cartografía y gráficas. Desacoplada del origen de datos. |
| `estilos.css` | Tokens de marca, componentes y cartografía. |

## Puesta en marcha

```bash
# 1. Paquete de demostración: 7 municipios de Jalisco más un distrito local
python3 etl_electoral_sync.py --demo --salida electoral_master_data.json --por-eleccion datos

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

## Cobertura territorial del paquete de demostración

Guadalajara, Zapopan, San Pedro Tlaquepaque, Tlajomulco de Zúñiga, Tonalá,
El Salto y Puerto Vallarta, con la seccionalización completa de cada uno
(3,293 secciones en total), más el distrito local 8 para verificar que el
selector de cargo cambia el universo territorial sin tocar la vista.

La lista nominal, el número de secciones y el tope de gastos son **valores de
calibración**, no cifras oficiales: cada elección los declara en su bloque
`calibracion`, y el tope se estima con
`FACTOR_TOPE_PROXY_MXN_POR_ELECTOR`. Antes de usar la plataforma con un
cliente hay que sustituirlos por el corte del padrón del INE, la
seccionalización vigente y el acuerdo de topes del IEPC Jalisco.

El maestro completo pesa unos 2.4 MB. Para producción conviene `--por-eleccion`:
el índice pesa unos kilobytes y el detalle seccional se baja solo del
territorio que el usuario abre.

## Suite digital

- **Radar competitivo.** Candidatura contra sus dos rivales en seguidores,
  crecimiento de siete días, publicaciones semanales, engagement real
  —interacciones sobre seguidores, no sobre alcance—, share of voice y pauta de
  treinta días. El radar normaliza cada eje contra el líder de esa métrica,
  porque las unidades no son comparables entre sí.
- **Auditoría de pauta política.** Gasto declarado en la Ad Library por actor
  en los últimos treinta días, con su reparto relativo, número de anuncios
  activos, costo por mil personas alcanzadas y temas pautados.
- **Rendimiento por formato.** Reel, imagen, carrusel y video largo comparados
  por tasa de respuesta sobre alcance, no por interacciones absolutas: un reel
  con mucho alcance acumula más likes y convierte peor que un carrusel bien
  armado. Cada actor tiene su propio perfil.
- **Mejores horas.** Mapa de calor de 7 × 24 con la tasa de respuesta orgánica
  por día y hora, el resumen por franjas de mañana, tarde y noche, y las cinco
  mejores ventanas puntuales. La tasa se mide sobre alcance,
  de modo que las horas con poco volumen de publicación no quedan castigadas.
- **Alertas tempranas.** El detector marca un pico cuando se cumplen tres
  condiciones a la vez: puntuación z sobre la media excluyendo el propio punto,
  un mínimo absoluto de menciones que haga accionable la alerta, y al menos el
  doble de la media de referencia. Si además se concentran cuentas creadas en
  los últimos treinta días, la alerta sube a ataque coordinado; sin ese segundo
  indicio se clasifica como crisis temática, que es un problema distinto y se
  atiende distinto.
- **Fiscalización.** Semáforo del gasto devengado reportado al SIF contra el
  tope de campaña, con la pauta digital auditada como componente y el costo por
  cada mil personas alcanzadas.

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

## Reporte ejecutivo

El botón del encabezado abre el diálogo de impresión con una hoja de estilo
propia: fondo blanco, paleta legible en papel, encabezado con el territorio y
la fecha de corte, y la tabla seccional completa —en pantalla vive dentro de un
contenedor con desplazamiento que en papel cortaría el listado en la primera
página—. Los controles, el buscador y el mosaico no se imprimen. Desde ese
diálogo se guarda como PDF.

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
