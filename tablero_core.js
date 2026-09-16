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

  var ESPERA_MAXIMA_MS = 6000;

  /* El respaldo embebido (data_fallback.js) permite abrir el tablero con doble
     clic. Bajo file:// el navegador bloquea la lectura del JSON por política de
     origen, así que ni siquiera se intenta: se va directo al respaldo. */
  function hayRespaldo() {
    return !!(global.DATA_FALLBACK && global.DATA_FALLBACK.elecciones);
  }

  function conTiempoLimite(promesa, ms) {
    return new Promise(function (resolver, rechazar) {
      var reloj = global.setTimeout(function () {
        rechazar(new Error('La carga excedió ' + (ms / 1000) + ' segundos.'));
      }, ms);
      promesa.then(function (valor) {
        global.clearTimeout(reloj);
        resolver(valor);
      }, function (error) {
        global.clearTimeout(reloj);
        rechazar(error);
      });
    });
  }

  function resuelveFuente(fuente) {
    if (typeof fuente === 'function') {
      return Promise.resolve().then(fuente);
    }
    if (fuente && typeof fuente === 'object') {
      return Promise.resolve(fuente);
    }

    var url = fuente || FUENTE_POR_DEFECTO;
    var protocoloLocal = global.location && global.location.protocol === 'file:';

    if (protocoloLocal && hayRespaldo()) {
      estado.usandoRespaldo = true;
      estado.motivoRespaldo = 'El archivo se abrió sin servidor, así que el tablero ' +
        'trabaja con la muestra embebida.';
      return Promise.resolve(global.DATA_FALLBACK);
    }

    if (typeof fetch !== 'function') {
      if (hayRespaldo()) {
        estado.usandoRespaldo = true;
        estado.motivoRespaldo = 'Este navegador no puede leer el archivo de datos.';
        return Promise.resolve(global.DATA_FALLBACK);
      }
      return Promise.reject(new Error('Este navegador no permite leer el archivo de datos.'));
    }

    var peticion = fetch(url, { cache: 'no-store' }).then(function (respuesta) {
      if (!respuesta.ok) {
        throw new Error('El servidor respondió ' + respuesta.status + ' al pedir ' + url);
      }
      return respuesta.json();
    });

    return conTiempoLimite(peticion, ESPERA_MAXIMA_MS).catch(function (error) {
      if (hayRespaldo()) {
        estado.usandoRespaldo = true;
        estado.motivoRespaldo = 'No se pudo leer ' + url + ' (' + error.message +
          '), así que el tablero trabaja con la muestra embebida.';
        return global.DATA_FALLBACK;
      }
      throw error;
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
    geojson: {},
    usandoRespaldo: false,
    motivoRespaldo: null,
    precandidatos: []
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

    pintaAccesoRapido();
  }

  /* Acceso rápido: un toque para saltar entre los territorios del mismo cargo,
     sin abrir el desplegable. */
  function pintaAccesoRapido() {
    var contenedor = vacia($('#selector-municipio-rapido'));
    var hermanas = eleccionesPorCargo(estado.eleccion.cargo);

    if (hermanas.length < 2) return;

    hermanas.forEach(function (e) {
      var boton = crear('button', 'filtro');
      boton.type = 'button';
      boton.dataset.eleccion = e.eleccion_id;
      boton.setAttribute('aria-pressed', String(e.eleccion_id === estado.eleccion.eleccion_id));
      boton.appendChild(document.createTextNode(nombreTerritorio(e)));
      contenedor.appendChild(boton);
    });
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

    var fisc = e.fiscalizacion || {};
    var colorSemaforo = fisc.semaforo === 'rojo' ? COLORES.riesgo
      : fisc.semaforo === 'ambar' ? COLORES.swing : COLORES.ganada;
    contenedor.appendChild(tarjeta({
      nombre: 'Fiscalización contra tope de campaña',
      valor: porcentaje(fisc.uso_tope_pct),
      color: colorSemaforo,
      avance: fisc.uso_tope_pct,
      pie: tope
        ? pesos(fisc.devengado_sif_mxn) + ' devengados de un tope de ' + pesos(tope) +
          '. Quedan ' + pesos(fisc.disponible_mxn) + '.'
        : 'Sin tope de campaña cargado para esta elección.'
    }));

    contenedor.appendChild(tarjeta({
      nombre: 'Pauta digital auditada',
      valor: pesos(gasto),
      color: COLORES.oro,
      avance: fisc.pauta_sobre_devengado_pct,
      pie: entero(redes.anuncios_activos || 0) + ' anuncios en la Ad Library · ' +
        pesos(redes.cpm_mxn || 0) + ' por cada mil alcanzados · ' +
        porcentaje(fisc.pauta_sobre_devengado_pct) + ' del gasto devengado.'
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

  /* -----------------------------------------------------------------
     Radar competitivo: la tabla de benchmark y su lectura normalizada
     ----------------------------------------------------------------- */

  var EJES_BENCHMARK = [
    { campo: 'seguidores', titulo: 'Seguidores', formato: entero },
    { campo: 'crecimiento_7d', titulo: 'Crecimiento 7D', formato: entero },
    { campo: 'publicaciones_7d', titulo: 'Pub. 7D', formato: entero },
    { campo: 'engagement_rate', titulo: 'Engagement', formato: porcentaje },
    { campo: 'share_of_voice_pct', titulo: 'Share of voice', formato: porcentaje },
    { campo: 'gasto_ads_30d_mxn', titulo: 'Pauta 30D', formato: pesos }
  ];

  function benchmarkVigente() {
    var redes = estado.eleccion.redes || {};
    return (redes.benchmark || []).slice().concat(filasEnMonitoreo());
  }

  /* Solo las filas con métricas entran a las gráficas; las que están en
     monitoreo aparecen en las tablas con guiones, no con ceros que parecerían
     un desempeño nulo. */
  function conMetricas(filas) {
    return filas.filter(function (f) { return !f.en_monitoreo; });
  }

  function celdaValor(valor, formateador) {
    return (valor === null || valor === undefined) ? '—' : formateador(valor);
  }

  function pintaBenchmark() {
    var filas = benchmarkVigente();
    var contenedor = vacia($('#benchmark'));

    if (!filas.length) {
      contenedor.appendChild(crear('p', 'estado',
        'Sin actores cargados para comparar en este territorio.'));
      return;
    }

    var tabla = crear('table', 'seccional comparativa');
    var thead = crear('thead');
    var filaCabeza = crear('tr');
    ['Actor'].concat(EJES_BENCHMARK.map(function (eje) { return eje.titulo; }))
      .concat(['CPM', 'Mejor formato'])
      .forEach(function (texto) {
        var th = crear('th', '', texto);
        th.scope = 'col';
        th.style.cursor = 'default';
        filaCabeza.appendChild(th);
      });
    thead.appendChild(filaCabeza);
    tabla.appendChild(thead);

    // Máximo por eje para marcar quién lidera cada métrica.
    var lideres = {};
    EJES_BENCHMARK.forEach(function (eje) {
      lideres[eje.campo] = filas.reduce(function (max, f) {
        return Math.max(max, Number(f[eje.campo]) || 0);
      }, 0);
    });

    var tbody = crear('tbody');
    filas.forEach(function (f) {
      var fila = crear('tr', f.es_propio ? 'actor-propio'
        : f.en_monitoreo ? 'actor-monitoreo' : '');

      var celdaNombre = crear('td');
      celdaNombre.appendChild(crear('span', 'actor-nombre', f.nombre));
      celdaNombre.appendChild(crear('span', 'actor-coalicion',
        f.en_monitoreo ? f.coalicion + ' · en monitoreo' : f.coalicion));
      fila.appendChild(celdaNombre);

      EJES_BENCHMARK.forEach(function (eje) {
        var bruto = f[eje.campo];
        var valor = Number(bruto) || 0;
        var celda = crear('td', '', celdaValor(bruto, eje.formato));
        if (eje.campo === 'crecimiento_7d' && f.crecimiento_7d_pct) {
          celda.title = '+' + decimal(f.crecimiento_7d_pct) + '% en siete días';
        }
        if (bruto !== null && bruto !== undefined && lideres[eje.campo] &&
            valor === lideres[eje.campo] && eje.campo !== 'gasto_ads_30d_mxn') {
          celda.classList.add('lidera');
        }
        fila.appendChild(celda);
      });

      fila.appendChild(crear('td', '', celdaValor(f.cpm_mxn, pesos)));

      var celdaFormato = crear('td');
      if (f.mejor_formato) {
        celdaFormato.appendChild(crear('span', 'etiqueta-formato', f.mejor_formato));
      } else {
        celdaFormato.textContent = '—';
      }
      if (f.en_monitoreo) {
        celdaFormato.title = 'Pendiente de la primera corrida del tracker.';
      }
      fila.appendChild(celdaFormato);

      tbody.appendChild(fila);
    });
    tabla.appendChild(tbody);
    contenedor.appendChild(tabla);

    var medibles = conMetricas(filas);
    var propio = medibles.filter(function (f) { return f.es_propio; })[0];
    var enMonitoreo = filas.length - medibles.length;
    if (propio) {
      var lectura = propio.posicion_sov === 1
        ? 'La candidatura encabeza la conversación con ' +
          porcentaje(propio.share_of_voice_pct) + ' del volumen municipal.'
        : 'La candidatura ocupa el lugar ' + propio.posicion_sov + ' en share of voice, con ' +
          porcentaje(propio.share_of_voice_pct) + ' frente al ' +
          porcentaje(medibles[0].share_of_voice_pct) + ' de ' + medibles[0].nombre + '.';
      if (enMonitoreo) {
        lectura += ' ' + entero(enMonitoreo) +
          (enMonitoreo === 1 ? ' aspirante registrado espera'
                             : ' aspirantes registrados esperan') +
          ' la primera corrida del tracker.';
      }
      contenedor.appendChild(crear('p', 'panel-nota px-5 py-4', lectura));
    }
  }

  function pintaRadar() {
    var filas = conMetricas(benchmarkVigente());
    if (!filas.length) return;

    // Cada eje se normaliza contra el líder de esa métrica: el radar compara
    // posiciones relativas, no magnitudes de distinta unidad.
    var topes = {};
    EJES_BENCHMARK.forEach(function (eje) {
      topes[eje.campo] = filas.reduce(function (max, f) {
        return Math.max(max, Number(f[eje.campo]) || 0);
      }, 0) || 1;
    });

    var paleta = [COLORES.oro, COLORES.cian, '#8f7ae5'];
    var datasets = filas.map(function (f, i) {
      var color = f.es_propio ? COLORES.oro : paleta[(i % (paleta.length - 1)) + 1];
      return {
        label: f.nombre,
        data: EJES_BENCHMARK.map(function (eje) {
          return Math.round((Number(f[eje.campo]) || 0) / topes[eje.campo] * 100);
        }),
        borderColor: color,
        backgroundColor: f.es_propio ? 'rgba(216,169,59,0.22)' : 'transparent',
        borderWidth: f.es_propio ? 2.5 : 1.6,
        pointBackgroundColor: color,
        pointRadius: 3
      };
    });

    var datos = {
      labels: EJES_BENCHMARK.map(function (eje) { return eje.titulo; }),
      datasets: datasets
    };

    var opciones = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          labels: { color: COLORES.medio, boxWidth: 10, usePointStyle: true }
        },
        tooltip: {
          backgroundColor: '#0e1729',
          borderColor: COLORES.linea,
          borderWidth: 1,
          titleColor: COLORES.texto,
          bodyColor: COLORES.medio,
          callbacks: {
            label: function (ctx) {
              var eje = EJES_BENCHMARK[ctx.dataIndex];
              var fila = filas[ctx.datasetIndex];
              return ctx.dataset.label + ': ' + eje.formato(fila[eje.campo]) +
                ' (' + ctx.formattedValue + ' de 100 contra el líder)';
            }
          }
        }
      },
      scales: {
        r: {
          min: 0,
          max: 100,
          angleLines: { color: 'rgba(34,49,79,0.8)' },
          grid: { color: 'rgba(34,49,79,0.8)' },
          pointLabels: { color: COLORES.medio, font: { size: 11 } },
          ticks: { display: false }
        }
      }
    };

    if (estado.graficas.radar) {
      estado.graficas.radar.data = datos;
      estado.graficas.radar.options = opciones;
      estado.graficas.radar.update();
    } else {
      estado.graficas.radar = new Chart($('#grafica-radar'), {
        type: 'radar', data: datos, options: opciones
      });
    }
  }

  /* -----------------------------------------------------------------
     Auditoría de pauta política: Ad Library cruzada con el tope del SIF
     ----------------------------------------------------------------- */

  function pintaAuditoriaPauta() {
    var filas = benchmarkVigente();
    var contenedor = vacia($('#auditoria-pauta'));

    if (!filas.length) {
      contenedor.appendChild(crear('p', 'estado',
        'Sin actores cargados para auditar la pauta de este territorio.'));
      return;
    }

    var ordenadas = filas.slice().sort(function (a, b) {
      if (!!a.en_monitoreo !== !!b.en_monitoreo) return a.en_monitoreo ? 1 : -1;
      return (b.gasto_ads_30d_mxn || 0) - (a.gasto_ads_30d_mxn || 0);
    });
    var lider = ordenadas[0].gasto_ads_30d_mxn || 1;

    var tabla = crear('table', 'seccional comparativa');
    var thead = crear('thead');
    var filaCabeza = crear('tr');
    ['Actor', 'Pauta 30D', 'Reparto', 'Anuncios', 'CPM', 'Temas pautados']
      .forEach(function (texto) {
        var th = crear('th', '', texto);
        th.scope = 'col';
        th.style.cursor = 'default';
        filaCabeza.appendChild(th);
      });
    thead.appendChild(filaCabeza);
    tabla.appendChild(thead);

    var tbody = crear('tbody');
    ordenadas.forEach(function (f) {
      var fila = crear('tr', f.es_propio ? 'actor-propio'
        : f.en_monitoreo ? 'actor-monitoreo' : '');

      var celdaNombre = crear('td');
      celdaNombre.appendChild(crear('span', 'actor-nombre', f.nombre));
      celdaNombre.appendChild(crear('span', 'actor-coalicion',
        f.en_monitoreo ? f.coalicion + ' · en monitoreo' : f.coalicion));
      fila.appendChild(celdaNombre);

      fila.appendChild(crear('td', '', celdaValor(f.gasto_ads_30d_mxn, pesos)));

      var celdaBarra = crear('td');
      var barra = crear('div', 'barra');
      var relleno = crear('i');
      relleno.style.width = limita((f.gasto_ads_30d_mxn || 0) / lider * 100, 0, 100) + '%';
      relleno.style.background = f.es_propio ? COLORES.oro : COLORES.cian;
      barra.appendChild(relleno);
      celdaBarra.appendChild(barra);
      fila.appendChild(celdaBarra);

      fila.appendChild(crear('td', '', celdaValor(f.anuncios_activos, entero)));
      fila.appendChild(crear('td', '', celdaValor(f.cpm_mxn, pesos)));

      var celdaTemas = crear('td', 'temas');
      (f.temas_pauta || []).forEach(function (tema) {
        celdaTemas.appendChild(crear('span', 'etiqueta-tema', tema));
      });
      if (!(f.temas_pauta || []).length) {
        celdaTemas.textContent = f.en_monitoreo
          ? 'Pendiente de la primera corrida del tracker'
          : 'Sin clasificar';
      }
      fila.appendChild(celdaTemas);

      tbody.appendChild(fila);
    });
    tabla.appendChild(tbody);
    contenedor.appendChild(tabla);

    var fisc = estado.eleccion.fiscalizacion || {};
    var propio = conMetricas(filas).filter(function (f) { return f.es_propio; })[0];
    if (propio) {
      var texto = 'La pauta digital representa ' +
        porcentaje(fisc.pauta_sobre_devengado_pct) +
        ' del gasto devengado ante el SIF. ';
      texto += propio.gasto_ads_30d_mxn >= lider
        ? 'La candidatura encabeza la inversión declarada en la Ad Library.'
        : 'La mayor inversión declarada es de ' + ordenadas[0].nombre + ', con ' +
          pesos(ordenadas[0].gasto_ads_30d_mxn) + '.';
      contenedor.appendChild(crear('p', 'panel-nota px-5 py-4', texto));
    }
  }

  /* -----------------------------------------------------------------
     Rendimiento por formato: qué funciona a cada quién
     ----------------------------------------------------------------- */

  var ORDEN_FORMATOS = ['Reel', 'Imagen', 'Carrusel', 'Video largo'];

  function pintaFormatos() {
    var filas = conMetricas(benchmarkVigente());
    if (!filas.length) return;

    var etiquetas = ORDEN_FORMATOS.filter(function (nombre) {
      return filas.some(function (f) {
        return (f.formatos || []).some(function (x) { return x.formato === nombre; });
      });
    });

    var paleta = [COLORES.oro, COLORES.cian, '#8f7ae5'];
    var datasets = filas.map(function (f, i) {
      var color = f.es_propio ? COLORES.oro : paleta[(i % (paleta.length - 1)) + 1];
      return {
        label: f.nombre,
        data: etiquetas.map(function (nombre) {
          var encontrado = (f.formatos || []).filter(function (x) {
            return x.formato === nombre;
          })[0];
          return encontrado ? encontrado.tasa_respuesta : 0;
        }),
        backgroundColor: color,
        borderRadius: 3
      };
    });

    var opciones = opcionesBase();
    opciones.scales.y = {
      title: { display: true, text: 'Tasa de respuesta (%)', color: COLORES.tenue },
      ticks: { color: COLORES.tenue, callback: function (v) { return decimal(v) + '%'; } },
      grid: { color: 'rgba(34,49,79,0.45)' }
    };

    var datos = { labels: etiquetas, datasets: datasets };

    if (estado.graficas.formatos) {
      estado.graficas.formatos.data = datos;
      estado.graficas.formatos.options = opciones;
      estado.graficas.formatos.update();
    } else {
      estado.graficas.formatos = new Chart($('#grafica-formatos'), {
        type: 'bar', data: datos, options: opciones
      });
    }

    var propio = filas.filter(function (f) { return f.es_propio; })[0];
    var nota = $('#nota-formatos');
    if (propio && propio.mejor_formato) {
      var mejor = (propio.formatos || []).filter(function (x) { return x.es_mejor; })[0];
      nota.textContent = 'El formato que mejor responde a la candidatura es ' +
        propio.mejor_formato.toLowerCase() + ', con ' + decimal(mejor.tasa_respuesta) +
        '% de respuesta sobre alcance y ' + entero(mejor.interacciones_por_publicacion) +
        ' interacciones por publicación.';
    } else {
      nota.textContent = 'Sin publicaciones clasificadas por formato.';
    }
  }

  /* -----------------------------------------------------------------
     Smart timing: mapa de calor de respuesta por día y hora
     ----------------------------------------------------------------- */

  function pintaSmartTiming() {
    var timing = (estado.eleccion.redes || {}).smart_timing || {};
    var matriz = timing.matriz || [];
    var contenedor = vacia($('#smart-timing'));

    if (!matriz.length) {
      contenedor.appendChild(crear('p', 'estado',
        'Sin publicaciones suficientes para calcular las mejores franjas.'));
      return;
    }

    var maximo = Number(timing.maximo) || 1;
    var dias = timing.dias || ['L', 'M', 'M', 'J', 'V', 'S', 'D'];

    var rejilla = crear('div', 'calor');

    // Esquina vacía y regla horaria: solo se rotulan las horas pares para
    // que la escala siga siendo legible en pantallas angostas.
    rejilla.appendChild(crear('span', 'calor-esquina'));
    for (var h = 0; h < 24; h++) {
      rejilla.appendChild(crear('span', 'calor-hora', h % 3 === 0 ? String(h) : ''));
    }

    matriz.forEach(function (fila, indiceDia) {
      rejilla.appendChild(crear('span', 'calor-dia', dias[indiceDia].slice(0, 3)));
      fila.forEach(function (valor, hora) {
        var intensidad = maximo ? Math.min(valor / maximo, 1) : 0;
        var celda = crear('span', 'calor-celda');
        celda.style.background = valor > 0
          ? 'rgba(63,210,226,' + (0.08 + intensidad * 0.82).toFixed(3) + ')'
          : 'var(--pizarra-alta)';
        celda.title = dias[indiceDia] + ' ' + hora + ':00 · tasa de respuesta ' +
          (valor > 0 ? decimal(valor) + '%' : 'sin publicaciones');
        rejilla.appendChild(celda);
      });
    });

    contenedor.appendChild(rejilla);

    var franjas = timing.franjas || [];
    if (franjas.length) {
      var tira = crear('div', 'tira-franjas');
      franjas.forEach(function (f) {
        var bloque = crear('div', 'franja-bloque' + (f.es_mejor ? ' franja-mejor' : ''));
        bloque.appendChild(crear('span', 'franja-nombre', f.franja));
        bloque.appendChild(crear('b', 'cifra', decimal(f.tasa_respuesta) + '%'));
        bloque.appendChild(crear('span', 'franja-rango', f.rango));
        bloque.title = entero(f.publicaciones) + ' publicaciones · ' +
          entero(f.interacciones) + ' interacciones';
        tira.appendChild(bloque);
      });
      contenedor.appendChild(tira);
    }

    var mejores = timing.mejores_franjas || [];
    if (mejores.length) {
      var lista = crear('ul', 'franjas');
      mejores.slice(0, 5).forEach(function (franja) {
        var item = crear('li');
        item.appendChild(crear('span', 'franja-hora',
          franja.dia_nombre + ' ' + franja.hora + ':00'));
        item.appendChild(crear('span', 'franja-tasa', decimal(franja.tasa_respuesta) + '%'));
        lista.appendChild(item);
      });
      contenedor.appendChild(crear('p', 'panel-nota px-5 pt-4',
        'Mejores franjas para publicar y para convocar, sobre ' +
        entero(timing.publicaciones_analizadas) + ' publicaciones analizadas.'));
      contenedor.appendChild(lista);
    }
  }

  /* -----------------------------------------------------------------
     Alertas tempranas
     ----------------------------------------------------------------- */

  var TIPOS_ALERTA = {
    ataque_coordinado: 'Ataque coordinado',
    crisis_tematica: 'Crisis temática',
    anomalia_cuentas: 'Anomalía de cuentas'
  };

  function pintaAlertas() {
    var alertas = ((estado.eleccion.redes || {}).alertas || []);
    var contenedor = vacia($('#alertas'));
    var contador = $('#contador-alertas');

    var altas = alertas.filter(function (a) { return a.severidad === 'alta'; }).length;
    contador.textContent = alertas.length
      ? entero(alertas.length) + (alertas.length === 1 ? ' alerta' : ' alertas') +
        (altas ? ' · ' + entero(altas) + ' de severidad alta' : '')
      : 'Sin señales atípicas';
    contador.style.color = altas ? COLORES.riesgo : COLORES.tenue;

    if (!alertas.length) {
      contenedor.appendChild(crear('p', 'estado',
        'La conversación se comporta dentro de lo esperado. El detector avisa cuando un tema rompe su propia media o se concentran cuentas recién creadas.'));
      return;
    }

    alertas.forEach(function (alerta) {
      var caja = crear('article', 'alerta alerta-' + alerta.severidad);

      var cabeza = crear('div', 'alerta-cabeza');
      cabeza.appendChild(crear('span', 'alerta-tipo',
        TIPOS_ALERTA[alerta.tipo] || alerta.tipo));
      cabeza.appendChild(crear('span', 'alerta-fecha', fechaLarga(alerta.fecha)));
      caja.appendChild(cabeza);

      caja.appendChild(crear('p', 'alerta-titulo', alerta.titulo));

      var evidencia = crear('ul', 'alerta-evidencia');
      (alerta.evidencia || []).forEach(function (linea) {
        evidencia.appendChild(crear('li', '', linea));
      });
      caja.appendChild(evidencia);

      contenedor.appendChild(caja);
    });
  }

  /* -----------------------------------------------------------------
     CRM de precandidatos: alta, persistencia local y configuración de
     monitoreo que consume el tracker en Python.
     ----------------------------------------------------------------- */

  var LLAVE_ALMACEN = 'consensus.precandidatos.v1';

  var CARGOS = ['Presidencia Municipal', 'Diputación Local',
                'Diputación Federal', 'Senaduría'];

  var TERRITORIOS_JALISCO = ['Guadalajara', 'Zapopan', 'San Pedro Tlaquepaque',
                             'Tlajomulco de Zúñiga', 'Tonalá', 'El Salto',
                             'Puerto Vallarta'];

  var CAMPOS_REDES = [
    { campo: 'facebook', etiqueta: 'Facebook Page ID o URL', ejemplo: '1234567890 o facebook.com/pagina' },
    { campo: 'instagram', etiqueta: 'Instagram', ejemplo: '@usuario' },
    { campo: 'x', etiqueta: 'X (Twitter)', ejemplo: '@usuario' },
    { campo: 'tiktok', etiqueta: 'TikTok', ejemplo: '@usuario' },
    { campo: 'ad_library_id', etiqueta: 'ID de Meta Ad Library', ejemplo: 'Page ID del anunciante' }
  ];

  function leePrecandidatos() {
    try {
      var crudo = global.localStorage.getItem(LLAVE_ALMACEN);
      var lista = crudo ? JSON.parse(crudo) : [];
      return Array.isArray(lista) ? lista : [];
    } catch (error) {
      // Navegación privada o almacenamiento bloqueado: el tablero sigue
      // funcionando, solo que el registro no sobrevive a la recarga.
      return [];
    }
  }

  function guardaPrecandidatos(lista) {
    estado.precandidatos = lista;
    try {
      global.localStorage.setItem(LLAVE_ALMACEN, JSON.stringify(lista));
      return true;
    } catch (error) {
      return false;
    }
  }

  function normalizaHandle(valor) {
    return String(valor || '').trim().replace(/^@/, '');
  }

  function precandidatosDe(eleccion) {
    var territorio = nombreTerritorio(eleccion);
    return estado.precandidatos.filter(function (p) {
      return p.territorio === territorio && p.cargo === eleccion.cargo;
    });
  }

  function pintaPrecandidatosRegistrados() {
    var contenedor = vacia($('#lista-precandidatos'));
    var lista = estado.precandidatos;
    var contador = $('#contador-precandidatos');

    contador.textContent = lista.length
      ? entero(lista.length) + (lista.length === 1 ? ' registrado' : ' registrados')
      : 'Ninguno registrado';

    if (!lista.length) {
      contenedor.appendChild(crear('p', 'estado',
        'Registra a los aspirantes que quieras auditar. Cada uno se suma al radar competitivo de su territorio y entra en la configuración de monitoreo que lee el tracker.'));
      return;
    }

    lista.forEach(function (p) {
      var ficha = crear('article', 'ficha-precandidato');

      var cabeza = crear('div', 'ficha-cabeza');
      var identidad = crear('div');
      identidad.appendChild(crear('p', 'ficha-nombre', p.nombre));
      identidad.appendChild(crear('p', 'ficha-meta',
        [p.cargo, p.territorio, p.entidad, p.partido].filter(Boolean).join(' · ')));
      cabeza.appendChild(identidad);

      var quitar = crear('button', 'boton boton-sutil', 'Quitar');
      quitar.type = 'button';
      quitar.addEventListener('click', function () {
        guardaPrecandidatos(estado.precandidatos.filter(function (otro) {
          return otro.id !== p.id;
        }));
        pintaPrecandidatosRegistrados();
        pintaBenchmark();
        pintaAuditoriaPauta();
      });
      cabeza.appendChild(quitar);
      ficha.appendChild(cabeza);

      var cuentas = crear('div', 'ficha-cuentas');
      CAMPOS_REDES.forEach(function (definicion) {
        var valor = p.redes && p.redes[definicion.campo];
        if (!valor) return;
        var etiqueta = crear('span', 'etiqueta-tema',
          definicion.etiqueta.split(' ')[0] + ': ' + valor);
        cuentas.appendChild(etiqueta);
      });
      if (!cuentas.childNodes.length) {
        cuentas.appendChild(crear('span', 'ficha-meta', 'Sin cuentas capturadas'));
      }
      ficha.appendChild(cuentas);

      contenedor.appendChild(ficha);
    });
  }

  function construyeFormulario() {
    var formulario = vacia($('#formulario-precandidato'));

    function control(etiqueta, nodo) {
      var envoltura = crear('label', 'control');
      envoltura.appendChild(crear('span', 'control-etiqueta', etiqueta));
      envoltura.appendChild(nodo);
      return envoltura;
    }

    function entrada(nombre, marcador) {
      var campo = crear('input', 'campo');
      campo.type = 'text';
      campo.name = nombre;
      campo.placeholder = marcador || '';
      campo.autocomplete = 'off';
      return campo;
    }

    formulario.appendChild(control('Nombre completo',
      entrada('nombre', 'Nombre de la persona aspirante')));

    var selCargo = crear('select', 'campo');
    selCargo.name = 'cargo';
    CARGOS.forEach(function (cargo) { selCargo.appendChild(opcion(cargo, cargo)); });
    formulario.appendChild(control('Cargo', selCargo));

    var campoEntidad = entrada('entidad', 'Jalisco');
    campoEntidad.value = 'Jalisco';
    formulario.appendChild(control('Entidad', campoEntidad));

    var selTerritorio = crear('select', 'campo');
    selTerritorio.name = 'territorio';
    TERRITORIOS_JALISCO.forEach(function (nombre) {
      selTerritorio.appendChild(opcion(nombre, nombre));
    });
    selTerritorio.appendChild(opcion('__manual__', 'Otro municipio o distrito'));
    formulario.appendChild(control('Municipio o distrito', selTerritorio));

    // La captura manual cubre el resto del país sin tocar el catálogo.
    var manual = entrada('territorio_manual', 'Escribe el municipio o distrito');
    var controlManual = control('Nombre del territorio', manual);
    controlManual.hidden = true;
    selTerritorio.addEventListener('change', function () {
      controlManual.hidden = selTerritorio.value !== '__manual__';
      if (!controlManual.hidden) manual.focus();
    });
    formulario.appendChild(controlManual);

    formulario.appendChild(control('Partido o coalición',
      entrada('partido', 'Partido, coalición o candidatura independiente')));

    CAMPOS_REDES.forEach(function (definicion) {
      formulario.appendChild(control(definicion.etiqueta,
        entrada('red_' + definicion.campo, definicion.ejemplo)));
    });
  }

  function valorCampo(nombre) {
    var nodo = $('[name="' + nombre + '"]', $('#formulario-precandidato'));
    return nodo ? String(nodo.value || '').trim() : '';
  }

  function registraPrecandidato() {
    var aviso = $('#aviso-precandidato');
    var nombre = valorCampo('nombre');
    var territorio = valorCampo('territorio');
    if (territorio === '__manual__') territorio = valorCampo('territorio_manual');

    if (!nombre) {
      aviso.textContent = 'Falta el nombre de la persona aspirante.';
      aviso.className = 'aviso aviso-error';
      return;
    }
    if (!territorio) {
      aviso.textContent = 'Indica el municipio o distrito que va a competir.';
      aviso.className = 'aviso aviso-error';
      return;
    }

    var redes = {};
    var conCuenta = false;
    CAMPOS_REDES.forEach(function (definicion) {
      var valor = valorCampo('red_' + definicion.campo);
      if (!valor) return;
      redes[definicion.campo] = definicion.campo === 'facebook' ||
        definicion.campo === 'ad_library_id' ? valor : normalizaHandle(valor);
      conCuenta = true;
    });

    var registro = {
      id: 'pc_' + Date.now().toString(36),
      nombre: nombre,
      cargo: valorCampo('cargo') || CARGOS[0],
      entidad: valorCampo('entidad') || 'Jalisco',
      territorio: territorio,
      partido: valorCampo('partido'),
      redes: redes,
      alta: new Date().toISOString().slice(0, 10)
    };

    var persistido = guardaPrecandidatos(estado.precandidatos.concat([registro]));

    var mensaje = nombre + ' quedó en monitoreo para ' + territorio + '.';
    var tono = 'ok';
    if (!conCuenta) {
      mensaje += ' Sin cuentas capturadas, el tracker no podrá auditarlo.';
      tono = 'atencion';
    }
    if (!persistido) {
      mensaje += ' El navegador no permitió guardarlo, así que el registro se pierde al recargar.';
      tono = 'atencion';
    }

    aviso.className = 'aviso aviso-' + tono;
    aviso.textContent = mensaje;

    construyeFormulario();
    pintaPrecandidatosRegistrados();
    pintaBenchmark();
    pintaAuditoriaPauta();

    // El alta devuelve al tablero: la confirmación se lee ahí, junto al radar
    // donde ya aparece la nueva fila en monitoreo.
    cierraModal();
    avisaEnTablero(mensaje, tono);
  }

  function exportaConfiguracionMonitoreo() {
    var configuracion = {
      generado_en: new Date().toISOString(),
      version: 1,
      origen: 'CRM de precandidatos · Consensus Estrategia',
      precandidatos: estado.precandidatos.map(function (p) {
        return {
          id: p.id,
          nombre: p.nombre,
          cargo: p.cargo,
          entidad: p.entidad,
          territorio: p.territorio,
          partido: p.partido,
          eleccion_id: (estado.elecciones.filter(function (e) {
            return nombreTerritorio(e) === p.territorio && e.cargo === p.cargo;
          })[0] || {}).eleccion_id || null,
          cuentas: {
            facebook_page: (p.redes || {}).facebook || null,
            instagram: (p.redes || {}).instagram || null,
            x: (p.redes || {}).x || null,
            tiktok: (p.redes || {}).tiktok || null,
            meta_ad_library_page_id: (p.redes || {}).ad_library_id || null
          }
        };
      })
    };

    var blob = new Blob([JSON.stringify(configuracion, null, 2)],
      { type: 'application/json;charset=utf-8;' });
    var enlace = document.createElement('a');
    enlace.href = URL.createObjectURL(blob);
    enlace.download = 'monitoreo_precandidatos.json';
    document.body.appendChild(enlace);
    enlace.click();
    document.body.removeChild(enlace);
    URL.revokeObjectURL(enlace.href);

    var aviso = $('#aviso-precandidato');
    aviso.className = 'aviso aviso-ok';
    aviso.textContent = 'Configuración exportada. Córrela con: python3 tracker_precandidatos.py ' +
      '--config monitoreo_precandidatos.json';
  }

  function abreModal() {
    var modal = $('#modal-precandidatos');
    if (!modal) return;
    modal.hidden = false;
    modal.classList.remove('hidden');
    document.body.classList.add('con-modal');
    construyeFormulario();
    pintaPrecandidatosRegistrados();
    $('#aviso-precandidato').textContent = '';
    $('#aviso-precandidato').className = 'aviso';
    var primero = $('[name="nombre"]', modal);
    if (primero) primero.focus();
  }

  /* Cierra por las tres vías: botón, fondo oscuro y Escape. Se apoya en el
     atributo `hidden` y además en la clase, por si el tablero se integra en
     una plantilla que use utilidades de Tailwind para ocultar. */
  function cierraModal() {
    var modal = $('#modal-precandidatos');
    if (modal) {
      modal.hidden = true;
      modal.classList.add('hidden');
      modal.classList.remove('flex');
    }
    document.body.classList.remove('con-modal');
    var disparador = $('#boton-precandidatos');
    if (disparador) disparador.focus();
  }

  /* Los registros sin métricas se muestran como filas en monitoreo: aparecen
     en el radar del territorio, pero con guiones en lugar de cifras hasta que
     el tracker traiga datos reales de las APIs. */
  function filasEnMonitoreo() {
    return precandidatosDe(estado.eleccion).map(function (p) {
      return {
        nombre: p.nombre,
        coalicion: p.partido || 'Sin partido declarado',
        es_propio: false,
        en_monitoreo: true,
        seguidores: null,
        crecimiento_7d: null,
        publicaciones_7d: null,
        engagement_rate: null,
        share_of_voice_pct: null,
        gasto_ads_30d_mxn: null,
        cpm_mxn: null,
        anuncios_activos: null,
        temas_pauta: [],
        formatos: [],
        mejor_formato: null
      };
    });
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
      var detalle = entero(t.menciones) + ' menciones · tendencia ' +
        (t.tendencia || 'sin definir');
      if (t.cuentas_nuevas_pct) {
        detalle += ' · ' + decimal(t.cuentas_nuevas_pct) + '% de cuentas nuevas';
      }
      izquierda.appendChild(crear('p', 'topico-meta', detalle));
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

  var NOMBRES_CAMPO = {
    lista_nominal: 'Lista nominal',
    secciones: 'Secciones',
    resultados_electorales: 'Resultados electorales',
    tope_gastos_campana: 'Tope de gastos',
    redes_sociales: 'Redes sociales'
  };

  /* El pie no repite una leyenda fija: lee la procedencia que el propio
     paquete declara campo por campo. Cuando los cómputos oficiales entran,
     la línea cambia sola. */
  function pintaProcedencia() {
    var procedencia = (estado.meta && estado.meta.procedencia) || {};
    var contenedor = vacia($('#pie-fuentes'));
    var campos = Object.keys(NOMBRES_CAMPO);

    if (!campos.some(function (c) { return procedencia[c]; })) {
      contenedor.appendChild(crear('span', 'fuente-linea',
        'Fuente: ' + (estado.meta.fuente_primaria || 'no declarada')));
      return;
    }

    if (procedencia.completo) {
      contenedor.appendChild(crear('span', 'fuente-linea',
        'Fuente: Cómputos oficiales IEPC Jalisco / INE · Meta Ad Library API y Graph API'));
      return;
    }

    campos.forEach(function (campo) {
      var bloque = procedencia[campo];
      if (!bloque) return;
      var etiqueta = crear('span', 'fuente-dato fuente-' + bloque.estado);
      etiqueta.appendChild(crear('b', '', NOMBRES_CAMPO[campo]));
      etiqueta.appendChild(document.createTextNode(' ' + bloque.estado));
      if (bloque.fuente) etiqueta.title = bloque.fuente;
      contenedor.appendChild(etiqueta);
    });
  }

  function pintaContexto() {
    var e = estado.eleccion;
    $('#contexto-eleccion').textContent =
      e.cargo + ' · ' + nombreTerritorio(e) + ' · ' + nombreCoalicion(e);
    $('#contexto-corte').textContent = 'Corte al ' + fechaLarga(estado.fechaCorte);
    $('#pie-version').textContent =
      'Esquema ' + (estado.meta.version_esquema || 'n/d') +
      ' · generado el ' + fechaLarga((estado.meta.generado_en || '').slice(0, 10));
    pintaProcedencia();
  }

  /* Aviso visible cuando el tablero corre con la muestra embebida, para que
     nadie confunda una vista parcial con el universo completo. */
  function avisaEnTablero(texto, tono) {
    var caja = $('#aviso-tablero');
    if (!caja) return;
    vacia(caja);
    caja.className = 'aviso aviso-' + (tono || 'ok') + ' mb-5';
    caja.hidden = false;
    caja.appendChild(crear('p', '', texto));
  }

  function pintaAvisoRespaldo() {
    var caja = $('#aviso-respaldo');
    var muestra = estado.eleccion.muestra;

    if (!estado.usandoRespaldo && !muestra) {
      caja.hidden = true;
      return;
    }

    caja.hidden = false;
    vacia(caja);
    var texto = estado.motivoRespaldo ||
      'El tablero trabaja con una muestra seccional embebida.';
    if (muestra) {
      texto += ' Se listan ' + entero(muestra.secciones_incluidas) + ' de ' +
        entero(muestra.secciones_totales) + ' secciones; los indicadores ' +
        'agregados sí corresponden al universo completo.';
    }
    caja.appendChild(crear('p', '', texto));
    caja.appendChild(crear('p', 'aviso-pie',
      'Para ver el detalle completo, sirve la carpeta por HTTP: python3 -m http.server 8080'));
  }

  /* -----------------------------------------------------------------
     Orquestación
     ----------------------------------------------------------------- */

  function pintaTodo() {
    pintaSelectores();
    pintaContexto();
    pintaAvisoRespaldo();
    pintaHero();
    pintaKpis();
    pintaFiltros();
    pintaEncabezadoTabla();
    pintaTabla();
    pintaDetalle();
    pintaCartografia();
    pintaGraficaCompetidores();
    pintaGraficaTrayectoria();
    pintaBenchmark();
    pintaRadar();
    pintaAuditoriaPauta();
    pintaFormatos();
    pintaSmartTiming();
    pintaAlertas();
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

  /* Si un nodo falta, el enlace se omite y los demás siguen conectándose: un
     solo elemento ausente no puede dejar el tablero sin controles. */
  function escucha(selector, evento, manejador) {
    var nodo = $(selector);
    if (!nodo) {
      if (global.console && console.warn) {
        console.warn('Tablero: no se encontró ' + selector + ', se omite el evento ' + evento + '.');
      }
      return false;
    }
    nodo.addEventListener(evento, manejador);
    return true;
  }

  function conectaControles() {
    escucha('#selector-cargo', 'change', function (ev) {
      var candidatas = eleccionesPorCargo(ev.target.value);
      if (candidatas.length) seleccionaEleccion(candidatas[0].eleccion_id);
    });

    escucha('#selector-territorio', 'change', function (ev) {
      seleccionaEleccion(ev.target.value);
    });

    escucha('#selector-coalicion', 'change', function (ev) {
      seleccionaEleccion(String(ev.target.value).split('|')[0]);
    });

    escucha('#campo-corte', 'change', function (ev) {
      estado.fechaCorte = ev.target.value;
      pintaContexto();
      pintaKpis();
      pintaGraficaCompetidores();
      pintaGraficaTrayectoria();
    });

    escucha('#selector-municipio-rapido', 'click', function (ev) {
      var boton = ev.target.closest('[data-eleccion]');
      if (boton) seleccionaEleccion(boton.dataset.eleccion);
    });

    escucha('#buscador-seccion', 'input', function (ev) {
      estado.busqueda = ev.target.value;
      pintaTabla();
      pintaCartografia();
    });

    escucha('#boton-exportar', 'click', exportaCsv);
    escucha('#boton-reporte', 'click', imprimeReporte);
    escucha('#boton-precandidatos', 'click', abreModal);
    escucha('#btn-cerrar-precandidatos', 'click', cierraModal);
    escucha('#boton-registrar', 'click', registraPrecandidato);
    escucha('#boton-exportar-monitoreo', 'click', exportaConfiguracionMonitoreo);

    escucha('#modal-precandidatos', 'click', function (ev) {
      if (ev.target === ev.currentTarget) cierraModal();
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && !$('#modal-precandidatos').hidden) cierraModal();
    });
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
     Reporte ejecutivo: la vista impresa del territorio abierto
     ----------------------------------------------------------------- */

  function imprimeReporte() {
    var e = estado.eleccion;
    var ind = e.indicadores || {};
    var fisc = e.fiscalizacion || {};

    // El pie impreso fija de qué territorio y de qué corte habla el papel,
    // porque una hoja suelta sin encabezado no sirve en una mesa de trabajo.
    $('#encabezado-impresion').textContent =
      'Consensus Estrategia · ' + e.cargo + ' · ' + nombreTerritorio(e) +
      ' · corte al ' + fechaLarga(estado.fechaCorte);

    $('#resumen-impresion').textContent =
      'Meta de ' + entero((e.candidato || {}).meta_votos) + ' votos sobre una lista nominal de ' +
      entero((e.territorio || {}).lista_nominal) + '. ' +
      entero(ind.casillas_swing) + ' secciones competidas y ' + entero(ind.casillas_riesgo) +
      ' en riesgo. Gasto devengado al ' + porcentaje(fisc.uso_tope_pct) + ' del tope de campaña.';

    // La tabla seccional se imprime completa: en pantalla vive dentro de un
    // contenedor con desplazamiento que cortaría el papel en la primera página.
    document.body.classList.add('imprimiendo');
    window.print();
    window.setTimeout(function () {
      document.body.classList.remove('imprimiendo');
    }, 500);
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
      ' y sirve la carpeta por HTTP. Para abrir el tablero con doble clic, genera además el respaldo embebido con '));
    ayuda.appendChild(crear('code', '', '--respaldo'));
    ayuda.appendChild(document.createTextNode(' y déjalo junto al index.'));
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

        estado.precandidatos = leePrecandidatos();
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
    abrirModalPrecandidatos: abreModal,
    cerrarModalPrecandidatos: cierraModal,
    estado: estado,
    utilidades: { entero: entero, decimal: decimal, pesos: pesos, porcentaje: porcentaje }
  };

})(window);
