/* ===================================================================
   Consensus Estrategia — Plataforma Electoral Modular
   tablero_core.js · lógica de tablero desacoplada del origen de datos

   El núcleo no sabe de qué municipio, distrito o cargo se trata: recibe
   un paquete con el esquema de `electoral_master_data.json` y renderiza.
   Para servir otro cliente basta con apuntar a otro archivo o inyectar
   el objeto en memoria; la vista no se modifica.

       Tablero.crear();                                  // usa ./electoral_master_data.json
       Tablero.crear({ fuente: '/api/tonala.json' });     // cualquier URL
       Tablero.crear({ fuente: window.ELECTORAL_DATA });  // objeto ya cargado
       Tablero.crear({ fuente: () => miPromesa() });      // función asíncrona

   Dependencias externas: Chart.js (CDN). Todo lo demás es Vanilla ES6.
   =================================================================== */

(function (global) {
  'use strict';

  var FUENTE_POR_DEFECTO = './electoral_master_data.json';

  /* -----------------------------------------------------------------
     Formato
     ----------------------------------------------------------------- */

  var fmtEntero = new Intl.NumberFormat('es-MX', { maximumFractionDigits: 0 });
  var fmtDecimal = new Intl.NumberFormat('es-MX', {
    minimumFractionDigits: 1, maximumFractionDigits: 1
  });
  var fmtPesos = new Intl.NumberFormat('es-MX', {
    style: 'currency', currency: 'MXN', maximumFractionDigits: 0
  });
  var fmtFecha = new Intl.DateTimeFormat('es-MX', {
    day: '2-digit', month: 'short', year: 'numeric'
  });

  function entero(n) { return fmtEntero.format(Math.round(Number(n) || 0)); }
  function decimal(n) { return fmtDecimal.format(Number(n) || 0); }
  function pesos(n) { return fmtPesos.format(Number(n) || 0); }
  function porcentaje(n) { return decimal(n) + '%'; }

  function fechaLarga(iso) {
    if (!iso) return 'sin fecha';
    var partes = String(iso).slice(0, 10).split('-');
    if (partes.length !== 3) return iso;
    var d = new Date(Number(partes[0]), Number(partes[1]) - 1, Number(partes[2]));
    return fmtFecha.format(d);
  }

  function fechaCorta(iso) {
    return String(iso || '').slice(5).replace('-', '/');
  }

  function limita(valor, min, max) {
    return Math.max(min, Math.min(max, Number(valor) || 0));
  }

  /* -----------------------------------------------------------------
     DOM
     ----------------------------------------------------------------- */

  function $(selector, raiz) { return (raiz || document).querySelector(selector); }

  function crear(etiqueta, clases, texto) {
    var nodo = document.createElement(etiqueta);
    if (clases) nodo.className = clases;
    if (texto !== undefined && texto !== null) nodo.textContent = texto;
    return nodo;
  }

  function vacia(nodo) {
    while (nodo && nodo.firstChild) nodo.removeChild(nodo.firstChild);
    return nodo;
  }

  function opcion(valor, texto) {
    var o = document.createElement('option');
    o.value = valor;
    o.textContent = texto;
    return o;
  }

  var COLORES = {
    oro: '#d8a93b',
    cian: '#3fd2e2',
    ganada: '#3fb984',
    swing: '#e8973a',
    riesgo: '#d2455c',
    texto: '#e6ecf7',
    medio: '#a9b8d2',
    tenue: '#7286a6',
    linea: '#22314f'
  };

  var ETIQUETAS_CLASE = {
    ganada: 'Clave ganada',
    swing: 'Competida',
    riesgo: 'En riesgo'
  };

  /* -----------------------------------------------------------------
     Carga del paquete de datos
     ----------------------------------------------------------------- */

  function resuelveFuente(fuente) {
    if (typeof fuente === 'function') {
      return Promise.resolve().then(fuente);
    }
    if (fuente && typeof fuente === 'object') {
      return Promise.resolve(fuente);
    }
    var url = fuente || FUENTE_POR_DEFECTO;
    return fetch(url, { cache: 'no-store' }).then(function (respuesta) {
      if (!respuesta.ok) {
        throw new Error('El servidor respondió ' + respuesta.status + ' al pedir ' + url);
      }
      return respuesta.json();
    });
  }

  function normalizaPaquete(paquete) {
    if (!paquete) throw new Error('El paquete de datos llegó vacío.');
    var elecciones = Array.isArray(paquete) ? paquete : paquete.elecciones;
    if (!Array.isArray(elecciones) || elecciones.length === 0) {
      throw new Error('El paquete no contiene ninguna elección en `elecciones`.');
    }
    return {
      meta: paquete.meta || { version_esquema: 'desconocida' },
      elecciones: elecciones
    };
  }

  /* -----------------------------------------------------------------
     Estado
     ----------------------------------------------------------------- */

  var estado = {
    meta: null,
    elecciones: [],
    eleccion: null,
    filtroClase: 'todas',
    busqueda: '',
    orden: { campo: 'target_movilizacion', direccion: 'desc' },
    seccionActiva: null,
    fechaCorte: null,
    graficas: {},
    geojson: {}
  };

  /* -----------------------------------------------------------------
     Selectores del encabezado
     ----------------------------------------------------------------- */

  function cargosDisponibles() {
    var vistos = [];
    estado.elecciones.forEach(function (e) {
      if (vistos.indexOf(e.cargo) === -1) vistos.push(e.cargo);
    });
    return vistos;
  }

  function nombreTerritorio(eleccion) {
    var t = eleccion.territorio || {};
    return t.municipio || t.distrito || t.entidad || eleccion.eleccion_id;
  }

  function nombreCoalicion(eleccion) {
    return (eleccion.candidato && eleccion.candidato.coalicion) || 'Sin coalición declarada';
  }

  function eleccionesPorCargo(cargo) {
    return estado.elecciones.filter(function (e) { return e.cargo === cargo; });
  }

  function pintaSelectores() {
    var selCargo = $('#selector-cargo');
    var selTerritorio = $('#selector-territorio');
    var selCoalicion = $('#selector-coalicion');
    var campoCorte = $('#campo-corte');

    vacia(selCargo);
    cargosDisponibles().forEach(function (cargo) {
      selCargo.appendChild(opcion(cargo, cargo));
    });
    selCargo.value = estado.eleccion.cargo;

    vacia(selTerritorio);
    eleccionesPorCargo(estado.eleccion.cargo).forEach(function (e) {
      selTerritorio.appendChild(opcion(e.eleccion_id, nombreTerritorio(e)));
    });
    selTerritorio.value = estado.eleccion.eleccion_id;

    vacia(selCoalicion);
    selCoalicion.appendChild(opcion(estado.eleccion.eleccion_id, nombreCoalicion(estado.eleccion)));
    var partidos = (estado.eleccion.candidato && estado.eleccion.candidato.partidos) || [];
    if (partidos.length) {
      selCoalicion.appendChild(opcion(estado.eleccion.eleccion_id + '|siglas', partidos.join(' · ')));
    }
    selCoalicion.value = estado.eleccion.eleccion_id;

    campoCorte.value = estado.fechaCorte;
    var serie = serieDisponible();
    if (serie.length) {
      campoCorte.min = serie[0].fecha;
      campoCorte.max = serie[serie.length - 1].fecha;
    }
  }

  /* -----------------------------------------------------------------
     Hero: riel de meta de votos
     ----------------------------------------------------------------- */

  function pintaHero() {
    var e = estado.eleccion;
    var candidato = e.candidato || {};
    var territorio = e.territorio || {};
    var meta = Number(candidato.meta_votos) || 0;
    var duro = Number(candidato.voto_duro_historico) || 0;
    var brecha = Math.max(meta - duro, 0);
    var tope = Math.max(meta, duro) * 1.18 || 1;

    $('#hero-meta').textContent = entero(meta);
    $('#hero-sub').textContent =
      'Votos necesarios para ganar ' + (e.cargo || '').toLowerCase() + ' en ' +
      nombreTerritorio(e) + ', sobre una lista nominal de ' +
      entero(territorio.lista_nominal) + ' y una abstención estimada de ' +
      porcentaje((e.parametros && e.parametros.abstencion_estimada_pct) || 0) + '.';

    $('#riel-duro').style.width = limita(duro / tope * 100, 0, 100) + '%';
    var barraBrecha = $('#riel-brecha');
    barraBrecha.style.left = limita(duro / tope * 100, 0, 100) + '%';
    barraBrecha.style.width = limita(brecha / tope * 100, 0, 100) + '%';
    $('#riel-marca').style.left = limita(meta / tope * 100, 0, 100) + '%';

    $('#leyenda-duro').textContent = 'Voto duro estimado ' + entero(duro);
    $('#leyenda-brecha').textContent = 'Brecha por movilizar ' + entero(brecha);
    $('#leyenda-meta').textContent = 'Meta ' + entero(meta);

    $('#hero-cobertura').textContent = entero(
      (e.indicadores && e.indicadores.secciones_prioritarias) || 0
    );
    $('#hero-secciones').textContent = entero(territorio.secciones_totales);
    $('#hero-jornada').textContent = fechaLarga(e.fecha_jornada);
  }

  /* -----------------------------------------------------------------
     Tarjetas de KPI
     ----------------------------------------------------------------- */

  function tarjeta(config) {
    var nodo = crear('article', 'kpi');
    nodo.appendChild(crear('p', 'kpi-nombre', config.nombre));

    var valor = crear('p', 'kpi-valor cifra', config.valor);
    if (config.color) valor.style.color = config.color;
    nodo.appendChild(valor);

    if (typeof config.avance === 'number') {
      var barra = crear('div', 'barra');
      var relleno = crear('i');
      relleno.style.width = limita(config.avance, 0, 100) + '%';
      relleno.style.background = config.color || COLORES.cian;
      barra.appendChild(relleno);
      nodo.appendChild(barra);
    }

    nodo.appendChild(crear('p', 'kpi-pie', config.pie));
    return nodo;
  }

  function kpisRedesVigentes() {
    var redes = estado.eleccion.redes || {};
    var base = Object.assign({}, redes.kpis || {});
    var serie = serieHastaCorte();
    if (serie.length) {
      var ultimo = serie[serie.length - 1];
      base.audiencia_total = ultimo.audiencia;
      base.gasto_ads_acumulado_mxn = serie.reduce(function (suma, p) {
        return suma + (Number(p.gasto_ads_mxn) || 0);
      }, 0);
      base.engagement_promedio = serie.reduce(function (suma, p) {
        return suma + (Number(p.engagement_rate) || 0);
      }, 0) / serie.length;
    }
    return base;
  }

  function pintaKpis() {
    var e = estado.eleccion;
    var ind = e.indicadores || {};
    var redes = kpisRedesVigentes();
    var tope = Number((e.candidato || {}).tope_gastos_campana_mxn) || 0;
    var gasto = Number(redes.gasto_ads_acumulado_mxn) || 0;
    var nfs = Number(redes.nfs) || 0;

    var contenedor = vacia($('#kpis'));

    contenedor.appendChild(tarjeta({
      nombre: 'Fuerza territorial neta',
      valor: porcentaje(ind.ftn_promedio),
      color: COLORES.oro,
      avance: ind.ftn_promedio,
      pie: 'Votación histórica ponderada por participación. Volatilidad media de ' +
        decimal(ind.swing_promedio) + ' puntos.'
    }));

    contenedor.appendChild(tarjeta({
      nombre: 'Favorabilidad neta en redes',
      valor: (nfs > 0 ? '+' : '') + decimal(nfs),
      color: nfs >= 0 ? COLORES.cian : COLORES.riesgo,
      avance: limita((nfs + 100) / 2, 0, 100),
      pie: porcentaje(redes.sentimiento_positivo_pct) + ' de comentarios positivos contra ' +
        porcentaje(redes.sentimiento_negativo_pct) + ' negativos.'
    }));

    var usoTope = tope ? gasto / tope * 100 : 0;
    contenedor.appendChild(tarjeta({
      nombre: 'Pauta auditada contra tope INE',
      valor: pesos(gasto),
      color: usoTope > 85 ? COLORES.riesgo : COLORES.oro,
      avance: usoTope,
      pie: tope
        ? porcentaje(usoTope) + ' del tope de ' + pesos(tope) + ' · ' +
          entero(redes.anuncios_activos || 0) + ' anuncios en la Ad Library.'
        : 'Sin tope de campaña cargado para esta elección.'
    }));

    contenedor.appendChild(tarjeta({
      nombre: 'Cobertura de secciones prioritarias',
      valor: porcentaje(ind.cobertura_prioritarias_pct),
      color: COLORES.swing,
      avance: ind.cobertura_prioritarias_pct,
      pie: entero(ind.casillas_swing) + ' competidas y ' + entero(ind.casillas_riesgo) +
        ' en riesgo concentran el esfuerzo de movilización.'
    }));
  }

  /* -----------------------------------------------------------------
     Territorio: filtros, tabla y detalle
     ----------------------------------------------------------------- */

  function seccionesFiltradas() {
    var secciones = (estado.eleccion.secciones || []).slice();
    if (estado.filtroClase !== 'todas') {
      secciones = secciones.filter(function (s) {
        return s.clasificacion === estado.filtroClase;
      });
    }
    if (estado.busqueda) {
      var termino = estado.busqueda.trim().toLowerCase();
      secciones = secciones.filter(function (s) {
        return String(s.seccion).toLowerCase().indexOf(termino) !== -1;
      });
    }
    var campo = estado.orden.campo;
    var signo = estado.orden.direccion === 'asc' ? 1 : -1;
    secciones.sort(function (a, b) {
      var va = a[campo], vb = b[campo];
      if (typeof va === 'string' || typeof vb === 'string') {
        return String(va).localeCompare(String(vb)) * signo;
      }
      return ((Number(va) || 0) - (Number(vb) || 0)) * signo;
    });
    return secciones;
  }

  function pintaFiltros() {
    var ind = estado.eleccion.indicadores || {};
    var conteos = {
      todas: (estado.eleccion.secciones || []).length,
      ganada: ind.casillas_ganadas || 0,
      swing: ind.casillas_swing || 0,
      riesgo: ind.casillas_riesgo || 0
    };
    var definicion = [
      { clave: 'todas', texto: 'Todas las secciones', color: null },
      { clave: 'ganada', texto: 'Clave ganadas', color: COLORES.ganada },
      { clave: 'swing', texto: 'Competidas', color: COLORES.swing },
      { clave: 'riesgo', texto: 'En riesgo', color: COLORES.riesgo }
    ];

    var contenedor = vacia($('#filtros-seccion'));
    definicion.forEach(function (item) {
      var boton = crear('button', 'filtro');
      boton.type = 'button';
      boton.setAttribute('aria-pressed', String(estado.filtroClase === item.clave));
      if (item.color) {
        var punto = crear('i', 'punto');
        punto.style.background = item.color;
        boton.appendChild(punto);
      }
      boton.appendChild(document.createTextNode(item.texto + ' ' + entero(conteos[item.clave])));
      boton.addEventListener('click', function () {
        estado.filtroClase = item.clave;
        estado.seccionActiva = null;
        pintaFiltros();
        pintaTabla();
        pintaCartografia();
        pintaDetalle();
      });
      contenedor.appendChild(boton);
    });
  }

  var COLUMNAS = [
    { campo: 'seccion', titulo: 'Sección', movil: true },
    { campo: 'lista_nominal', titulo: 'Lista nominal', movil: false },
    { campo: 'ftn', titulo: 'FTN', movil: true },
    { campo: 'swing_index', titulo: 'Swing', movil: true },
    { campo: 'margen_ultima_pct', titulo: 'Margen', movil: false },
    { campo: 'target_movilizacion', titulo: 'Target', movil: true },
    { campo: 'clasificacion', titulo: 'Estatus', movil: true }
  ];

  function pintaEncabezadoTabla() {
    var fila = vacia($('#tabla-encabezado'));
    COLUMNAS.forEach(function (col) {
      var th = crear('th', col.movil ? '' : 'oculta-movil', col.titulo);
      th.scope = 'col';
      th.tabIndex = 0;
      th.setAttribute('aria-sort',
        estado.orden.campo === col.campo
          ? (estado.orden.direccion === 'asc' ? 'ascending' : 'descending')
          : 'none');
      function ordena() {
        if (estado.orden.campo === col.campo) {
          estado.orden.direccion = estado.orden.direccion === 'asc' ? 'desc' : 'asc';
        } else {
          estado.orden.campo = col.campo;
          estado.orden.direccion = col.campo === 'seccion' ? 'asc' : 'desc';
        }
        pintaEncabezadoTabla();
        pintaTabla();
      }
      th.addEventListener('click', ordena);
      th.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); ordena(); }
      });
      fila.appendChild(th);
    });
  }

  function celdaClase(clasificacion) {
    var etiqueta = crear('span', 'etiqueta-clase clase-' + clasificacion,
      ETIQUETAS_CLASE[clasificacion] || clasificacion);
    return etiqueta;
  }

  function pintaTabla() {
    var cuerpo = vacia($('#tabla-cuerpo'));
    var secciones = seccionesFiltradas();

    $('#conteo-secciones').textContent = secciones.length === 1
      ? '1 sección en la vista'
      : entero(secciones.length) + ' secciones en la vista';

    if (!secciones.length) {
      var fila = crear('tr');
      var celda = crear('td', 'estado', 'Ningún resultado con ese filtro. Cambia el estatus o limpia la búsqueda.');
      celda.colSpan = COLUMNAS.length;
      celda.style.textAlign = 'center';
      fila.appendChild(celda);
      cuerpo.appendChild(fila);
      return;
    }

    secciones.forEach(function (s) {
      var fila = crear('tr', 'fila-seccion' + (estado.seccionActiva === s.seccion ? ' activa' : ''));
      fila.tabIndex = 0;

      fila.appendChild(crear('td', '', s.seccion));

      var tdLista = crear('td', 'oculta-movil', entero(s.lista_nominal));
      fila.appendChild(tdLista);

      fila.appendChild(crear('td', '', porcentaje(s.ftn)));
      fila.appendChild(crear('td', '', decimal(s.swing_index)));

      var tdMargen = crear('td', 'oculta-movil',
        (s.margen_ultima_pct > 0 ? '+' : '') + decimal(s.margen_ultima_pct));
      tdMargen.style.color = s.margen_ultima_pct >= 0 ? COLORES.ganada : COLORES.riesgo;
      fila.appendChild(tdMargen);

      fila.appendChild(crear('td', '', entero(s.target_movilizacion)));

      var tdClase = crear('td');
      tdClase.appendChild(celdaClase(s.clasificacion));
      fila.appendChild(tdClase);

      function selecciona() { seleccionaSeccion(s.seccion); }
      fila.addEventListener('click', selecciona);
      fila.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selecciona(); }
      });

      cuerpo.appendChild(fila);
    });
  }

  function seccionPorClave(clave) {
    var lista = estado.eleccion.secciones || [];
    for (var i = 0; i < lista.length; i++) {
      if (lista[i].seccion === clave) return lista[i];
    }
    return null;
  }

  function seleccionaSeccion(clave) {
    estado.seccionActiva = estado.seccionActiva === clave ? null : clave;
    pintaTabla();
    pintaDetalle();
    marcaSeleccionCartografia();
  }

  function pintaDetalle() {
    var contenedor = vacia($('#detalle-seccion'));
    var s = estado.seccionActiva ? seccionPorClave(estado.seccionActiva) : null;

    if (!s) {
      contenedor.appendChild(crear('p', 'panel-nota',
        'Selecciona una sección en la tabla o en el mapa para ver su historial y su target de movilización.'));
      return;
    }

    var titulo = crear('div', 'flex items-center justify-between gap-4 flex-wrap');
    var izquierda = crear('div');
    izquierda.appendChild(crear('h3', 'panel-titulo', 'Sección ' + s.seccion));
    izquierda.appendChild(crear('p', 'panel-nota',
      [s.municipio, s.distrito_local ? 'Distrito local ' + s.distrito_local : null,
       entero(s.casillas) + ' casillas']
        .filter(Boolean).join(' · ')));
    titulo.appendChild(izquierda);
    titulo.appendChild(celdaClase(s.clasificacion));
    contenedor.appendChild(titulo);

    var rejilla = crear('div', 'detalle-rejilla');
    [
      { valor: entero(s.lista_nominal), etiqueta: 'Lista nominal' },
      { valor: porcentaje(s.ftn), etiqueta: 'Fuerza territorial neta' },
      { valor: decimal(s.swing_index), etiqueta: 'Índice de volatilidad' },
      { valor: porcentaje(s.participacion_media_pct), etiqueta: 'Participación media' },
      { valor: entero(s.target_movilizacion), etiqueta: 'Votos a movilizar' }
    ].forEach(function (dato) {
      var bloque = crear('div', 'detalle-dato');
      bloque.appendChild(crear('b', '', dato.valor));
      bloque.appendChild(crear('span', '', dato.etiqueta));
      rejilla.appendChild(bloque);
    });
    contenedor.appendChild(rejilla);

    var historico = s.historico || {};
    var anios = Object.keys(historico).sort();
    if (anios.length) {
      var tabla = crear('table', 'seccional');
      var thead = crear('thead');
      var filaCabeza = crear('tr');
      ['Elección', 'Participación', 'Votación propia', 'Margen', 'Votos propios']
        .forEach(function (texto) {
          var th = crear('th', '', texto);
          th.scope = 'col';
          th.style.cursor = 'default';
          filaCabeza.appendChild(th);
        });
      thead.appendChild(filaCabeza);
      tabla.appendChild(thead);

      var tbody = crear('tbody');
      anios.forEach(function (anio) {
        var r = historico[anio];
        var fila = crear('tr');
        fila.appendChild(crear('td', '', anio));
        fila.appendChild(crear('td', '', porcentaje(r.participacion_pct)));
        fila.appendChild(crear('td', '', porcentaje(r.share_propio_pct)));
        var tdMargen = crear('td', '', (r.margen_pct > 0 ? '+' : '') + decimal(r.margen_pct));
        tdMargen.style.color = r.margen_pct >= 0 ? COLORES.ganada : COLORES.riesgo;
        fila.appendChild(tdMargen);
        fila.appendChild(crear('td', '', entero(r.votos_propios)));
        tbody.appendChild(fila);
      });
      tabla.appendChild(tbody);
      contenedor.appendChild(tabla);
    }
  }

  /* -----------------------------------------------------------------
     Cartografía: mapa GeoJSON cuando existe, mosaico seccional si no
     ----------------------------------------------------------------- */

  function claveGeo(propiedades) {
    var llaves = ['seccion', 'SECCION', 'Seccion', 'cve_seccion', 'CVE_SECCION', 'seccion_electoral'];
    for (var i = 0; i < llaves.length; i++) {
      if (propiedades && propiedades[llaves[i]] !== undefined) {
        var numero = parseInt(propiedades[llaves[i]], 10);
        if (!isNaN(numero)) return ('0000' + numero).slice(-4);
      }
    }
    return null;
  }

  function anillosDeFeature(geometria) {
    if (!geometria) return [];
    if (geometria.type === 'Polygon') return geometria.coordinates;
    if (geometria.type === 'MultiPolygon') {
      return geometria.coordinates.reduce(function (acumulado, poligono) {
        return acumulado.concat(poligono);
      }, []);
    }
    return [];
  }

  function dibujaMapa(geojson, contenedor) {
    var features = (geojson && geojson.features) || [];
    if (!features.length) return false;

    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    features.forEach(function (f) {
      anillosDeFeature(f.geometry).forEach(function (anillo) {
        anillo.forEach(function (punto) {
          if (punto[0] < minX) minX = punto[0];
          if (punto[0] > maxX) maxX = punto[0];
          if (punto[1] < minY) minY = punto[1];
          if (punto[1] > maxY) maxY = punto[1];
        });
      });
    });
    if (!isFinite(minX)) return false;

    // Proyección equirectangular corregida por latitud: suficiente y exacta
    // a escala municipal o distrital, sin cargar una librería cartográfica.
    var latMedia = (minY + maxY) / 2 * Math.PI / 180;
    var escalaX = Math.cos(latMedia);
    var ancho = (maxX - minX) * escalaX || 1;
    var alto = (maxY - minY) || 1;
    var lienzoAncho = 1000;
    var lienzoAlto = Math.round(lienzoAncho * alto / ancho);

    function proyecta(punto) {
      var x = (punto[0] - minX) * escalaX / ancho * lienzoAncho;
      var y = lienzoAlto - (punto[1] - minY) / alto * lienzoAlto;
      return x.toFixed(1) + ',' + y.toFixed(1);
    }

    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 ' + lienzoAncho + ' ' + lienzoAlto);
    svg.setAttribute('class', 'lienzo-mapa');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Mapa de secciones electorales por estatus competitivo');

    features.forEach(function (f) {
      var clave = claveGeo(f.properties);
      var seccion = clave ? seccionPorClave(clave) : null;
      var d = anillosDeFeature(f.geometry).map(function (anillo) {
        return 'M' + anillo.map(proyecta).join('L') + 'Z';
      }).join(' ');
      if (!d) return;

      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', d);
      path.setAttribute('fill', seccion ? COLORES[seccion.clasificacion] : '#1a2740');
      if (clave) {
        path.setAttribute('data-seccion', clave);
        var titulo = document.createElementNS('http://www.w3.org/2000/svg', 'title');
        titulo.textContent = seccion
          ? 'Sección ' + clave + ' · ' + ETIQUETAS_CLASE[seccion.clasificacion] +
            ' · target ' + entero(seccion.target_movilizacion)
          : 'Sección ' + clave + ' · sin datos de cómputo';
        path.appendChild(titulo);
        if (seccion) {
          path.addEventListener('click', function () { seleccionaSeccion(clave); });
        }
      }
      svg.appendChild(path);
    });

    contenedor.appendChild(svg);
    return true;
  }

  function dibujaMosaico(contenedor) {
    var secciones = seccionesFiltradas();
    var mosaico = crear('div', 'mosaico');

    if (!secciones.length) {
      contenedor.appendChild(crear('p', 'estado',
        'No hay secciones que mostrar con el filtro actual.'));
      return;
    }

    var maxLista = secciones.reduce(function (max, s) {
      return Math.max(max, Number(s.lista_nominal) || 0);
    }, 1);

    secciones.forEach(function (s) {
      var proporcion = Math.sqrt((Number(s.lista_nominal) || 0) / maxLista);
      var lado = Math.round(13 + proporcion * 17);
      var tesela = crear('button', 'tesela tesela-' + s.clasificacion +
        (estado.seccionActiva === s.seccion ? ' activa' : ''));
      tesela.type = 'button';
      tesela.style.width = lado + 'px';
      tesela.style.height = lado + 'px';
      tesela.dataset.seccion = s.seccion;
      tesela.title = 'Sección ' + s.seccion + ' · ' + ETIQUETAS_CLASE[s.clasificacion] +
        ' · lista nominal ' + entero(s.lista_nominal) + ' · target ' +
        entero(s.target_movilizacion);
      tesela.setAttribute('aria-label', tesela.title);
      tesela.addEventListener('click', function () { seleccionaSeccion(s.seccion); });
      mosaico.appendChild(tesela);
    });

    contenedor.appendChild(mosaico);
  }

  function pintaCartografia() {
    var contenedor = vacia($('#lienzo-territorial'));
    var nota = $('#nota-cartografia');
    var url = (estado.eleccion.territorio || {}).geojson_url;

    if (url && estado.geojson[url]) {
      if (dibujaMapa(estado.geojson[url], contenedor)) {
        nota.textContent = 'Cartografía seccional del INE, coloreada por estatus competitivo.';
        marcaSeleccionCartografia();
        return;
      }
    }

    dibujaMosaico(contenedor);
    nota.textContent = url
      ? 'Mosaico seccional mientras carga la cartografía. Cada tesela es una sección; el tamaño refleja su lista nominal.'
      : 'Cada tesela es una sección electoral y su tamaño refleja la lista nominal. Carga un GeoJSON en `territorio.geojson_url` para ver el mapa real.';
  }

  function marcaSeleccionCartografia() {
    var nodos = document.querySelectorAll('#lienzo-territorial [data-seccion]');
    Array.prototype.forEach.call(nodos, function (nodo) {
      var esActiva = nodo.dataset.seccion === estado.seccionActiva;
      if (nodo.tagName.toLowerCase() === 'path') {
        nodo.classList.toggle('activa', esActiva);
      } else {
        nodo.classList.toggle('activa', esActiva);
      }
    });
  }

  function cargaGeojsonSiHay() {
    var url = (estado.eleccion.territorio || {}).geojson_url;
    if (!url || estado.geojson[url]) return Promise.resolve();
    return fetch(url)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (datos) {
        if (datos) {
          estado.geojson[url] = datos;
          pintaCartografia();
        }
      })
      .catch(function () { /* el mosaico queda como vista vigente */ });
  }

  /* -----------------------------------------------------------------
     Estrategia digital: series, competidores y tópicos
     ----------------------------------------------------------------- */

  function serieDisponible() {
    return ((estado.eleccion.redes || {}).serie_tiempo || []).slice().sort(function (a, b) {
      return String(a.fecha).localeCompare(String(b.fecha));
    });
  }

  function serieHastaCorte() {
    var corte = estado.fechaCorte;
    return serieDisponible().filter(function (p) {
      return !corte || String(p.fecha) <= corte;
    });
  }

  function opcionesBase() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          labels: { color: COLORES.medio, boxWidth: 10, boxHeight: 10, usePointStyle: true }
        },
        tooltip: {
          backgroundColor: '#0e1729',
          borderColor: COLORES.linea,
          borderWidth: 1,
          titleColor: COLORES.texto,
          bodyColor: COLORES.medio,
          padding: 10
        }
      },
      scales: {
        x: {
          ticks: { color: COLORES.tenue, maxRotation: 0, autoSkipPadding: 18 },
          grid: { color: 'rgba(34,49,79,0.45)' }
        }
      }
    };
  }

  function pintaGraficaCompetidores() {
    var competidores = ((estado.eleccion.redes || {}).competidores || []).slice();
    var propio = estado.eleccion.candidato || {};
    var redes = kpisRedesVigentes();

    var nombres = [propio.nombre || 'Candidatura propia'].concat(
      competidores.map(function (c) { return c.nombre; })
    );
    var gastos = [Number(redes.gasto_ads_acumulado_mxn) || 0].concat(
      competidores.map(function (c) { return Number(c.gasto_ads_mxn) || 0; })
    );
    var engagement = [Number(redes.engagement_promedio) || 0].concat(
      competidores.map(function (c) { return Number(c.engagement_rate) || 0; })
    );

    var opciones = opcionesBase();
    opciones.scales.y = {
      position: 'left',
      title: { display: true, text: 'Gasto en pauta (MXN)', color: COLORES.tenue },
      ticks: {
        color: COLORES.tenue,
        callback: function (valor) { return '$' + entero(valor / 1000) + 'k'; }
      },
      grid: { color: 'rgba(34,49,79,0.45)' }
    };
    opciones.scales.y2 = {
      position: 'right',
      title: { display: true, text: 'Engagement (%)', color: COLORES.tenue },
      ticks: { color: COLORES.tenue, callback: function (v) { return decimal(v) + '%'; } },
      grid: { drawOnChartArea: false }
    };

    var datos = {
      labels: nombres,
      datasets: [
        {
          type: 'bar',
          label: 'Gasto en pauta',
          data: gastos,
          backgroundColor: nombres.map(function (_, i) {
            return i === 0 ? 'rgba(216,169,59,0.85)' : 'rgba(63,210,226,0.32)';
          }),
          borderRadius: 3,
          yAxisID: 'y',
          order: 2
        },
        {
          type: 'line',
          label: 'Engagement rate',
          data: engagement,
          borderColor: COLORES.cian,
          backgroundColor: COLORES.cian,
          pointRadius: 5,
          pointHoverRadius: 7,
          borderWidth: 2,
          tension: 0.25,
          yAxisID: 'y2',
          order: 1
        }
      ]
    };

    if (estado.graficas.competidores) {
      estado.graficas.competidores.data = datos;
      estado.graficas.competidores.options = opciones;
      estado.graficas.competidores.update();
    } else {
      estado.graficas.competidores = new Chart($('#grafica-competidores'), {
        type: 'bar', data: datos, options: opciones
      });
    }
  }

  function pintaGraficaTrayectoria() {
    var serie = serieHastaCorte();
    var etiquetas = serie.map(function (p) { return fechaCorta(p.fecha); });

    var opciones = opcionesBase();
    opciones.scales.y = {
      position: 'left',
      title: { display: true, text: 'Audiencia', color: COLORES.tenue },
      ticks: { color: COLORES.tenue, callback: function (v) { return entero(v / 1000) + 'k'; } },
      grid: { color: 'rgba(34,49,79,0.45)' }
    };
    opciones.scales.y2 = {
      position: 'right',
      title: { display: true, text: 'Gasto diario (MXN)', color: COLORES.tenue },
      ticks: { color: COLORES.tenue, callback: function (v) { return '$' + entero(v / 1000) + 'k'; } },
      grid: { drawOnChartArea: false }
    };

    var datos = {
      labels: etiquetas,
      datasets: [
        {
          type: 'line',
          label: 'Audiencia acumulada',
          data: serie.map(function (p) { return p.audiencia; }),
          borderColor: COLORES.cian,
          backgroundColor: 'rgba(63,210,226,0.12)',
          fill: true,
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.3,
          yAxisID: 'y'
        },
        {
          type: 'bar',
          label: 'Gasto diario en pauta',
          data: serie.map(function (p) { return p.gasto_ads_mxn; }),
          backgroundColor: 'rgba(216,169,59,0.55)',
          borderRadius: 2,
          yAxisID: 'y2'
        }
      ]
    };

    if (estado.graficas.trayectoria) {
      estado.graficas.trayectoria.data = datos;
      estado.graficas.trayectoria.options = opciones;
      estado.graficas.trayectoria.update();
    } else {
      estado.graficas.trayectoria = new Chart($('#grafica-trayectoria'), {
        type: 'line', data: datos, options: opciones
      });
    }
  }

  function colorSentimiento(nfs) {
    if (nfs >= 10) return COLORES.ganada;
    if (nfs >= -10) return COLORES.swing;
    return COLORES.riesgo;
  }

  function pintaTopicos() {
    var topicos = ((estado.eleccion.redes || {}).topicos || []).slice().sort(function (a, b) {
      return (Number(a.nfs) || 0) - (Number(b.nfs) || 0);
    });
    var contenedor = vacia($('#topicos'));

    if (!topicos.length) {
      contenedor.appendChild(crear('p', 'estado',
        'Sin escucha digital cargada para esta elección.'));
      return;
    }

    topicos.forEach(function (t) {
      var fila = crear('div', 'topico');

      var izquierda = crear('div');
      izquierda.appendChild(crear('p', 'topico-nombre', t.tema));
      izquierda.appendChild(crear('p', 'topico-meta',
        entero(t.menciones) + ' menciones · tendencia ' + (t.tendencia || 'sin definir')));
      fila.appendChild(izquierda);

      var semaforo = crear('div', 'semaforo');
      var punto = crear('i', 'punto');
      var color = colorSentimiento(Number(t.nfs) || 0);
      punto.style.background = color;
      semaforo.appendChild(punto);
      var valor = crear('span', '', (t.nfs > 0 ? '+' : '') + decimal(t.nfs));
      valor.style.color = color;
      semaforo.appendChild(valor);
      fila.appendChild(semaforo);

      contenedor.appendChild(fila);
    });
  }

  /* -----------------------------------------------------------------
     Pie y contexto
     ----------------------------------------------------------------- */

  function pintaContexto() {
    var e = estado.eleccion;
    var redes = e.redes || {};
    $('#contexto-eleccion').textContent =
      e.cargo + ' · ' + nombreTerritorio(e) + ' · ' + nombreCoalicion(e);
    $('#contexto-corte').textContent = 'Corte al ' + fechaLarga(estado.fechaCorte);
    $('#pie-version').textContent =
      'Esquema ' + (estado.meta.version_esquema || 'n/d') +
      ' · generado el ' + fechaLarga((estado.meta.generado_en || '').slice(0, 10)) +
      ' · fuente ' + (estado.meta.fuente_primaria || 'no declarada') +
      (redes.origen_serie === 'mock' ? ' · métricas de redes en modo demostración' : '');
  }

  /* -----------------------------------------------------------------
     Orquestación
     ----------------------------------------------------------------- */

  function pintaTodo() {
    pintaSelectores();
    pintaContexto();
    pintaHero();
    pintaKpis();
    pintaFiltros();
    pintaEncabezadoTabla();
    pintaTabla();
    pintaDetalle();
    pintaCartografia();
    pintaGraficaCompetidores();
    pintaGraficaTrayectoria();
    pintaTopicos();
  }

  function seleccionaEleccion(eleccionId) {
    var encontrada = estado.elecciones.filter(function (e) {
      return e.eleccion_id === eleccionId;
    })[0];
    if (!encontrada) return;

    estado.eleccion = encontrada;
    estado.seccionActiva = null;
    estado.filtroClase = 'todas';
    estado.busqueda = '';
    var buscador = $('#buscador-seccion');
    if (buscador) buscador.value = '';

    var serie = serieDisponible();
    estado.fechaCorte = encontrada.fecha_corte ||
      (serie.length ? serie[serie.length - 1].fecha : null);

    pintaTodo();
    cargaGeojsonSiHay();
  }

  function conectaControles() {
    $('#selector-cargo').addEventListener('change', function (ev) {
      var candidatas = eleccionesPorCargo(ev.target.value);
      if (candidatas.length) seleccionaEleccion(candidatas[0].eleccion_id);
    });

    $('#selector-territorio').addEventListener('change', function (ev) {
      seleccionaEleccion(ev.target.value);
    });

    $('#selector-coalicion').addEventListener('change', function (ev) {
      seleccionaEleccion(String(ev.target.value).split('|')[0]);
    });

    $('#campo-corte').addEventListener('change', function (ev) {
      estado.fechaCorte = ev.target.value;
      pintaContexto();
      pintaKpis();
      pintaGraficaCompetidores();
      pintaGraficaTrayectoria();
    });

    var buscador = $('#buscador-seccion');
    buscador.addEventListener('input', function (ev) {
      estado.busqueda = ev.target.value;
      pintaTabla();
      pintaCartografia();
    });

    $('#boton-exportar').addEventListener('click', exportaCsv);
  }

  /* -----------------------------------------------------------------
     Exportación operativa (la lista que baja a territorio)
     ----------------------------------------------------------------- */

  function exportaCsv() {
    var secciones = seccionesFiltradas();
    var encabezados = ['seccion', 'municipio', 'distrito_local', 'lista_nominal',
      'participacion_media_pct', 'ftn', 'swing_index', 'margen_ultima_pct',
      'target_movilizacion', 'clasificacion'];

    var filas = [encabezados.join(',')];
    secciones.forEach(function (s) {
      filas.push(encabezados.map(function (campo) {
        var valor = s[campo];
        if (valor === null || valor === undefined) return '';
        return /[",\n]/.test(String(valor)) ? '"' + String(valor).replace(/"/g, '""') + '"' : valor;
      }).join(','));
    });

    var blob = new Blob(['\ufeff' + filas.join('\n')], { type: 'text/csv;charset=utf-8;' });
    var enlace = document.createElement('a');
    enlace.href = URL.createObjectURL(blob);
    enlace.download = estado.eleccion.eleccion_id + '_secciones_' +
      estado.filtroClase + '.csv';
    document.body.appendChild(enlace);
    enlace.click();
    document.body.removeChild(enlace);
    URL.revokeObjectURL(enlace.href);
  }

  /* -----------------------------------------------------------------
     Estado de error
     ----------------------------------------------------------------- */

  function muestraError(error) {
    var tablero = $('#tablero');
    if (tablero) tablero.hidden = true;
    var caja = $('#estado-carga');
    vacia(caja);
    caja.hidden = false;
    caja.appendChild(crear('h2', 'panel-titulo', 'No se pudieron cargar los datos'));
    caja.appendChild(crear('p', '', error.message ||
      'El archivo de datos no está disponible.'));
    var ayuda = crear('p', '');
    ayuda.appendChild(document.createTextNode('Genera el paquete con '));
    ayuda.appendChild(crear('code', '', 'python3 etl_electoral_sync.py --demo'));
    ayuda.appendChild(document.createTextNode(
      ' y sirve la carpeta por HTTP; abrir el archivo con doble clic bloquea la lectura del JSON.'));
    caja.appendChild(ayuda);
  }

  /* -----------------------------------------------------------------
     API pública
     ----------------------------------------------------------------- */

  function crearTablero(opciones) {
    opciones = opciones || {};
    var fuente = opciones.fuente !== undefined ? opciones.fuente : global.ELECTORAL_DATA;

    return resuelveFuente(fuente)
      .then(normalizaPaquete)
      .then(function (paquete) {
        estado.meta = paquete.meta;
        estado.elecciones = paquete.elecciones;

        $('#estado-carga').hidden = true;
        $('#tablero').hidden = false;

        conectaControles();
        seleccionaEleccion(
          opciones.eleccionInicial || paquete.elecciones[0].eleccion_id
        );
        return estado;
      })
      .catch(function (error) {
        muestraError(error);
        throw error;
      });
  }

  global.Tablero = {
    crear: crearTablero,
    estado: estado,
    utilidades: { entero: entero, decimal: decimal, pesos: pesos, porcentaje: porcentaje }
  };

})(window);
