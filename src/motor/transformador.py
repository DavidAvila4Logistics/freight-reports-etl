"""Operaciones de transformación declaradas en el YAML de cada agente.

Cada entrada de `transformaciones:` es un dict de un solo elemento:
    - filtrar_ventana_fecha: {columna: ETA, dias_atras: 15, dias_adelante: 30}
Las columnas se referencian por su nombre EXACTO en el encabezado del crudo.
"""

import datetime as dt

import polars as pl


def aplicar_transformaciones(nombres, df, transformaciones, fecha_referencia: dt.date, fmt_fecha: str,
                             auxiliares=None):
    """`auxiliares` es {nombre: (nombres, df)} con los archivos de apoyo ya leídos
    (ver `entrada.auxiliares` en la config), más la lista `_avisos` donde los pasos
    dejan lo que tiene que terminar en hallazgos.log."""
    for trans in transformaciones:
        (op, params), = trans.items()
        fn = _OPERACIONES.get(op)
        if fn is None:
            raise ValueError(f"Transformación desconocida: '{op}'. Disponibles: {sorted(_OPERACIONES)}")
        nombres, df = _normalizar(*fn(nombres, df, params or {}, fecha_referencia, fmt_fecha, auxiliares or {}))
    return nombres, df


def _normalizar(nombres, df):
    """Invariante del motor: df.columns es siempre c0..cN en el orden de `nombres`.

    Sin esto, cualquier operación que saque o reordene columnas desalinea los
    nombres internos y la siguiente referencia por nombre apunta a otra columna
    (o crea una fantasma). Renumerar después de cada paso mantiene la
    correspondencia posición <-> nombre interno pase lo que pase.
    """
    if df.width != len(nombres):
        raise ValueError(
            f"Desalineación interna: {len(nombres)} nombres y {df.width} columnas de datos. "
            "Es un bug de la transformación anterior, no de la config del agente."
        )
    df.columns = [f"c{i}" for i in range(df.width)]
    return nombres, df


def _indice(nombres, columna):
    exactas = [i for i, n in enumerate(nombres) if n == columna]
    if not exactas:
        exactas = [i for i, n in enumerate(nombres) if n.strip() == str(columna).strip()]
    if not exactas:
        raise ValueError(f"Columna '{columna}' no encontrada. Encabezado: {nombres}")
    if len(exactas) > 1:
        raise ValueError(f"Columna '{columna}' ambigua (aparece {len(exactas)} veces)")
    return exactas[0]


def _col(nombres, columna):
    return f"c{_indice(nombres, columna)}"


def _eliminar_columnas(nombres, df, params, *_):
    internos = [_col(nombres, c) for c in params["columnas"]]
    indices = {int(c[1:]) for c in internos}
    return [n for i, n in enumerate(nombres) if i not in indices], df.drop(internos)


def _copiar_columna(nombres, df, params, *_):
    origen, destino = _col(nombres, params["origen"]), _col(nombres, params["destino"])
    return nombres, df.with_columns(pl.col(origen).alias(destino))


def _renombrar(nombres, df, params, *_):
    nuevos = list(nombres)
    limpios = [n.strip() for n in nombres]
    for viejo, nuevo in params.items():
        # Si el origen ya trae el nombre final, no hay nada que hacer. Pasa cuando
        # el mismo reporte se puede bajar en dos formatos con títulos apenas
        # distintos (mail vs. portal): una sola config sirve para ambos.
        if str(viejo).strip() not in limpios and str(nuevo).strip() in limpios:
            continue
        nuevos[_indice(nombres, viejo)] = nuevo
    return nuevos, df


def _filtrar_ventana_fecha(nombres, df, params, fecha_referencia, fmt_fecha, auxiliares=None):
    """Conserva las filas cuya fecha cae en la ventana [ref - dias_atras, ref + dias_adelante].

    La cota inferior saca las cargas que arribaron hace rato; la superior saca las
    que todavía faltan un mes y solo hacen ruido. Las dos son opcionales: sin
    `dias_adelante` se comporta como el viejo `filtrar_fecha_minima`, que sigue
    registrado como alias.

    `conservar_si_vacia: "<columna>"` es la excepción para las cargas cuya FACTURA
    todavía no llegó: no se descartan nunca por antigüedad y se quedan en el CSV
    hasta que alguien complete el dato. Mientras no tengan factura el destino las
    ignora, así que no ensucian nada, y si se cayeran del CSV nadie se enteraría de
    que faltan.

    Una fecha que no parsea con `fmt_fecha` queda en null y se trata como vacía.
    """
    col = _col(nombres, params["columna"])
    fecha = pl.col(col).str.strip_chars().str.to_date(fmt_fecha, strict=False)

    cond = pl.lit(True)
    if params.get("dias_atras") is not None:
        cond = cond & (fecha >= fecha_referencia - dt.timedelta(days=int(params["dias_atras"])))
    if params.get("dias_adelante") is not None:
        cond = cond & (fecha <= fecha_referencia + dt.timedelta(days=int(params["dias_adelante"])))

    if params.get("conservar_vacias", True):
        cond = cond | fecha.is_null()
    else:
        cond = cond.fill_null(False)
    cond = cond.fill_null(False)

    columna_factura = params.get("conservar_si_vacia")
    if columna_factura:
        sin_factura = pl.col(_col(nombres, columna_factura)).str.strip_chars() == ""
        rescatadas = df.filter(sin_factura & ~cond)
        cond = cond | sin_factura
        if rescatadas.height:
            avisos = (auxiliares or {}).setdefault("_avisos", [])
            avisos.append({
                "nivel": "advertencia", "tipo": "sin_factura_retenida",
                "mensaje": (f"{rescatadas.height} filas fuera de la ventana de "
                            f"'{params['columna']}' se conservan porque "
                            f"'{columna_factura}' está vacía — hay que pedir la "
                            f"factura al forwarder para poder cargarlas"),
            })
            print(f"  Ventana: {rescatadas.height} filas retenidas sin '{columna_factura}'")

    return nombres, df.filter(cond.fill_null(False))


def _filtrar_excluir_prefijo(nombres, df, params, *_):
    """Borra las filas cuya columna arranca con alguno de los prefijos dados.

    Con `separador` mira CÓDIGO POR CÓDIGO en vez de la celda entera, y alcanza
    con que uno matchee para descartar la fila: en una casilla como
    '0609990001 6500000999' el código a excluir no siempre va primero.
    """
    col = _col(nombres, params["columna"])
    prefijos = tuple(str(p).upper() for p in params["prefijos"])
    sep = params.get("separador")

    if sep is None:
        valor = pl.col(col).str.strip_chars().str.to_uppercase()
        cond = pl.lit(False)
        for pref in prefijos:
            cond = cond | valor.str.starts_with(pref)
        return nombres, df.filter(~cond.fill_null(False))

    def tiene_prefijo(celda: str) -> bool:
        return any(t.strip().upper().startswith(prefijos)
                   for t in (celda or "").split(sep) if t.strip())

    marca = df[col].map_elements(tiene_prefijo, return_dtype=pl.Boolean)
    return nombres, df.filter(~marca)


def _filtrar_no_vacias(nombres, df, params, *_):
    col = _col(nombres, params["columna"])
    return nombres, df.filter(pl.col(col).str.strip_chars() != "")


def _filtrar_vacias_todas(nombres, df, params, *_):
    """Borra la fila solo cuando TODAS las columnas dadas están vacías.

    Una carga sin documento de transporte y sin ETA no sirve; con una sola de las
    dos llena la fila se queda: sin BL pero con ETA es una carga real que todavía
    no tiene documento, y sin ETA pero con BL todavía no zarpó.
    """
    columnas = [_col(nombres, c) for c in params["columnas"]]
    vacia = pl.lit(True)
    for col in columnas:
        vacia = vacia & (pl.col(col).fill_null("").str.strip_chars() == "")
    return nombres, df.filter(~vacia)


def _filtrar_largo(nombres, df, params, *_):
    """Borra las filas cuyo código no llega al largo mínimo (o pasa el máximo).

    Un código más corto de lo que debe medir es un código cortado en el origen: se
    borra la fila entera en vez de reconstruirlo, porque el motor no inventa un
    dato que no tiene.

    Las celdas VACÍAS se conservan salvo `conservar_vacias: false`: una casilla
    sin dato no es un código corto, y descartarla es una decisión aparte.
    """
    col = _col(nombres, params["columna"])
    valor = pl.col(col).str.strip_chars()
    cond = pl.lit(True)
    if params.get("minimo") is not None:
        cond = cond & (valor.str.len_chars() >= int(params["minimo"]))
    if params.get("maximo") is not None:
        cond = cond & (valor.str.len_chars() <= int(params["maximo"]))
    if params.get("conservar_vacias", True):
        cond = cond | (valor == "")
    return nombres, df.filter(cond.fill_null(False))


def _recortar_largo(nombres, df, params, *_):
    """Recorta la celda al largo declarado, tirando lo que sobra por la derecha.

    Un contenedor ISO 6346 mide 11 caracteres; cuando el portal manda uno con un
    dígito de más ('ABCU12345678'), lo correcto es conservar el arranque del código.
    Lo más corto que el largo declarado pasa intacto: acá solo se recorta.
    """
    col = _col(nombres, params["columna"])
    largo = int(params["largo"])
    return nombres, df.with_columns(
        pl.col(col).str.strip_chars().str.slice(0, largo).alias(col)
    )


def _filtrar_contiene(nombres, df, params, *_):
    col = _col(nombres, params["columna"])
    texto = str(params["contiene"]).upper()
    cond = pl.col(col).str.to_uppercase().str.contains(texto, literal=True)
    if params.get("conservar_vacias", False):
        cond = cond | (pl.col(col).str.strip_chars() == "")
    return nombres, df.filter(cond)


def _reemplazar_texto(nombres, df, params, *_):
    buscar, reemplazar = str(params["buscar"]), str(params["reemplazar"])
    if "columnas" in params:
        objetivo = [_col(nombres, c) for c in params["columnas"]]
    else:
        objetivo = df.columns
    return nombres, df.with_columns(
        [pl.col(c).str.replace_all(buscar, reemplazar, literal=True) for c in objetivo]
    )


def _vaciar_columna(nombres, df, params, *_):
    col = _col(nombres, params["columna"])
    return nombres, df.with_columns(pl.lit("").alias(col))


def _cruzar(nombres, df, params, _fecha, _fmt, auxiliares):
    """VLOOKUP contra un archivo auxiliar: reemplaza los cruces manuales en Excel.

    La planilla de soporte típica hace
        data!H = VLOOKUP(L, bajada!$C$2:$D$500, 2, 0)
    y a veces la clave del auxiliar viene con un carácter de más adelante
    (bajada!C = RIGHT(A, LEN(A)-1)). `recortar_clave` reproduce ese recorte.

    Las claves duplicadas en el auxiliar se resuelven como VLOOKUP: gana la
    primera aparición.

    `omitir_vacios_en_auxiliar` (default False) es la única divergencia opcional
    respecto de Excel: saltea las filas del auxiliar cuya columna a traer está
    vacía, de modo que ante una clave repetida gane la primera NO vacía. Encendido
    solo puede llenar celdas que el VLOOKUP hubiera dejado en blanco — nunca pisa
    un valor que el VLOOKUP sí encontraba. Hace falta cuando el reporte auxiliar
    lista la misma factura varias veces y la primera todavía no tiene el dato.
    """
    nombre_aux = params["auxiliar"]
    if nombre_aux not in auxiliares:
        raise ValueError(
            f"Falta el archivo auxiliar '{nombre_aux}' para el cruce. "
            f"Declarado en 'entrada.auxiliares'; guardarlo en data/raw/<agente>/ "
            f"con un nombre que matchee su patrón. Auxiliares leídos: {sorted(auxiliares)}"
        )
    nombres_aux, df_aux = auxiliares[nombre_aux]

    recorte = int(params.get("recortar_clave", 0))
    col_aux_clave = f"c{_indice(nombres_aux, params['clave_externa'])}"
    claves = df_aux[col_aux_clave].str.strip_chars()
    if recorte:
        claves = claves.str.slice(recorte)

    omitir_vacios = bool(params.get("omitir_vacios_en_auxiliar", False))

    tabla = {}
    for destino, origen in params["traer"].items():
        col_origen = f"c{_indice(nombres_aux, origen)}"
        # setdefault y no dict(zip(...)): ante claves repetidas en el auxiliar,
        # VLOOKUP se queda con la PRIMERA aparición
        mapa = {}
        for k, v in zip(claves.to_list(), df_aux[col_origen].to_list()):
            if omitir_vacios and (v is None or str(v).strip() == ""):
                continue
            mapa.setdefault(k, v)
        mapa.pop("", None)          # una clave vacía haría match con filas vacías
        tabla[destino] = mapa

    sin_match = params.get("si_no_encuentra", "vaciar")
    if sin_match not in ("vaciar", "conservar"):
        raise ValueError(f"'si_no_encuentra' debe ser 'vaciar' o 'conservar', no '{sin_match}'")

    col_local = _col(nombres, params["clave_local"])
    for destino, mapa in tabla.items():
        col_destino = _col(nombres, destino)
        buscado = pl.col(col_local).str.strip_chars().replace_strict(
            mapa, default=None, return_dtype=pl.Utf8)
        if sin_match == "conservar":
            nuevo = buscado.fill_null(pl.col(col_destino))
        else:
            nuevo = buscado.fill_null("")
        df = df.with_columns(nuevo.alias(col_destino))
    return nombres, df


def _corregir_facturas(nombres, df, params, *_):
    """Normaliza números de factura por prefijo, factura por factura.

    Reemplaza la columna "corrige facturas" de una planilla Excel hecha con IF
    anidados. Las reglas se declaran en el YAML y se evalúan EN ORDEN — la primera
    que matchea gana, igual que en la fórmula; el orden importa porque '261XA0101J'
    matchea tanto '261XA' como '26'.

    Los prefijos llevan el año adelante (25→2025, 26→2026…), así que la lista se
    amplía cada enero: por eso vive en la config y no acá.

    Diferencia deliberada con la fórmula: la fórmula operaba sobre la celda entera,
    así que en las celdas con varias facturas solo corregía la primera. Acá se
    corrige cada una por separado.

    `eliminar_prefijos` borra códigos enteros dentro de la casilla en vez de
    corregirlos (referencias internas que no son facturas). Si la casilla queda
    vacía, el aviso de la validación `no_vacias` dice qué cargas revisar: el motor
    nunca inventa el dato.

    `deduplicar` saca los repetidos que aparecen DESPUÉS de normalizar: la misma
    factura con y sin el cero de la izquierda en una casilla queda una sola vez.
    """
    col = _col(nombres, params["columna"])
    sep = params.get("separador", ",")
    unir_con = params.get("unir_con", ", ")
    reglas = params.get("reglas") or []
    eliminar = [str(p).upper() for p in (params.get("eliminar_prefijos") or [])]
    deduplicar = bool(params.get("deduplicar", False))

    def corregir_una(factura: str) -> str:
        for regla in reglas:
            for prefijo in regla["prefijos"]:
                if factura.startswith(str(prefijo)):
                    return str(regla.get("anteponer", "")) + factura[int(regla.get("quitar", 0)):]
        return factura

    def corregir(celda: str) -> str:
        if celda is None or not celda.strip():
            return ""
        partes = [p.strip() for p in celda.split(sep) if p.strip()]
        partes = [p for p in partes
                  if not any(p.upper().startswith(e) for e in eliminar)]
        salida = []
        for p in partes:
            corregida = corregir_una(p)
            if deduplicar and corregida in salida:
                continue
            salida.append(corregida)
        return unir_con.join(salida)

    return nombres, df.with_columns(
        pl.col(col).map_elements(corregir, return_dtype=pl.Utf8).alias(col)
    )


def _homogeneizar_serie(nombres, df, params, _fecha, _fmt, auxiliares=None):
    """Deja en la casilla solo los códigos de la serie mayoritaria.

    Dentro de una misma casilla los códigos tienen que empezar de manera
    homogénea, y el que rompe la serie se borra — pero SOLO cuando hay al menos
    `minimo_codigos`. Con dos o tres no hay serie de la que hablar y sobreviven todos.

    La firma de un código es (largo, dos primeros caracteres): es lo que separa
    '705610001' de '75610004' (un dígito comido) y '0705610005' de '6500000777'
    (una orden de compra colada entre facturas).

    Sin mayoría estricta la casilla NO se toca y se avisa: el motor no elige por
    el usuario cuál de dos series es la buena.
    """
    col = _col(nombres, params["columna"])
    sep = params.get("separador", " ")
    unir_con = params.get("unir_con", sep)
    minimo = int(params.get("minimo_codigos", 4))
    ambiguas, limpiadas = [], 0

    def firma(codigo: str):
        return (len(codigo), codigo[:2].upper())

    valores = df[col].to_list()
    salida = []
    for celda in valores:
        partes = [p.strip() for p in (celda or "").split(sep) if p.strip()]
        if len(partes) < minimo:
            salida.append(celda or "")
            continue
        conteo = {}
        for p in partes:
            conteo[firma(p)] = conteo.get(firma(p), 0) + 1
        orden = sorted(conteo.items(), key=lambda kv: -kv[1])
        if len(orden) > 1 and orden[0][1] == orden[1][1]:
            ambiguas.append(celda)
            salida.append(celda or "")
            continue
        mayoritaria = orden[0][0]
        quedan = [p for p in partes if firma(p) == mayoritaria]
        if len(quedan) != len(partes):
            limpiadas += 1
        salida.append(unir_con.join(quedan))

    avisos = (auxiliares or {}).setdefault("_avisos", [])
    if ambiguas:
        avisos.append({
            "nivel": "advertencia", "tipo": "serie_ambigua",
            "mensaje": (f"'{params['columna']}': {len(ambiguas)} casillas con series "
                        f"empatadas, se dejan como están -> "
                        + "; ".join(ambiguas[:3])),
        })
    if limpiadas or ambiguas:
        print(f"  Serie: {limpiadas} casillas homogeneizadas, {len(ambiguas)} empates sin tocar")
    return nombres, df.with_columns(pl.Series(col, salida, dtype=pl.Utf8))


def _rellenar_vacias(nombres, df, params, *_):
    """Llena las celdas vacías de una columna.

    Para destinos que no aceptan celdas vacías en una columna y donde cualquier
    código de la misma columna sirve. Se usa el primer valor no vacío para que la
    corrida sea reproducible: con un valor al azar dos corridas del mismo crudo
    darían CSV distintos y el registro maestro las contaría como modificaciones.
    """
    col = _col(nombres, params["columna"])
    desde = params.get("desde", "primera_no_vacia")
    if desde == "primera_no_vacia":
        no_vacias = [v for v in df[col].to_list() if (v or "").strip()]
        relleno = no_vacias[0] if no_vacias else str(params.get("valor", ""))
    else:
        relleno = str(desde)
    vacia = pl.col(col).str.strip_chars() == ""
    return nombres, df.with_columns(
        pl.when(vacia).then(pl.lit(relleno)).otherwise(pl.col(col)).alias(col)
    )


def _normalizar_entero(nombres, df, params, *_):
    """Devuelve a notación entera lo que venga en notación científica.

    Una factura puramente numérica de 12 dígitos que Excel entrega como float sale
    de `str(v)` como '3.18447000913e+11', y el destino la toma como otra factura.
    Los valores no numéricos ('E31877001') pasan intactos.
    """
    import re as _re
    col = _col(nombres, params["columna"])
    cientifica = _re.compile(r"^[+-]?\d+(?:[.,]\d+)?[eE][+-]?\d+$")

    def normalizar(v: str) -> str:
        s = (v or "").strip()
        if cientifica.match(s):
            return format(int(float(s.replace(",", "."))), "d")
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return v or ""

    return nombres, df.with_columns(
        pl.col(col).map_elements(normalizar, return_dtype=pl.Utf8).alias(col)
    )


def _primer_token(nombres, df, params, *_):
    """Deja solo el primer valor de una celda que lista varios separados por espacio.

    Una columna de órdenes de compra puede traer ocho números en una celda y el
    destino no acepta tantos caracteres: queda la primera.
    """
    col = _col(nombres, params["columna"])
    sep = params.get("separador", " ")
    return nombres, df.with_columns(
        pl.col(col).str.strip_chars().str.split(sep).list.first().fill_null("").alias(col)
    )


def _orden_columnas(nombres, df, params, *_):
    """Declara el orden EXACTO de salida y descarta lo que no esté en la lista.

    Resuelve de una los "mover la columna X delante de Y" + "eliminar Z" de un
    instructivo, que encadenados por separado son frágiles: acá el YAML dice
    literalmente cómo queda el encabezado final. Cualquier columna del crudo que no
    se nombre queda afuera, así que un cambio de layout del origen falla
    ruidosamente en vez de correr los datos una posición y producir un CSV que el
    destino acepta pero está mal.

    Una misma columna se puede pedir DOS VECES (hay destinos que esperan columnas
    repetidas). Por eso cada columna elegida se alias a un nombre interno fresco
    antes del select — polars rechaza un select con nombres repetidos, y
    `_normalizar` renumera todo después.

    Cada entrada de `columnas` es el nombre a secas o, cuando el crudo repite un
    título, un dict `{columna: X, ocurrencia: N}` (1-based) para decir cuál de las
    dos se quiere; `como: Y` además la renombra en la salida.
    """
    salida, internos = [], []
    for entrada in params["columnas"]:
        if isinstance(entrada, dict):
            nombre = entrada["columna"]
            ocurrencia = entrada.get("ocurrencia")
            salida.append(entrada.get("como", nombre))
        else:
            nombre, ocurrencia = entrada, None
            salida.append(entrada)
        internos.append(_col(nombres, nombre) if ocurrencia is None
                        else f"c{_indice_ocurrencia(nombres, nombre, ocurrencia)}")
    return salida, df.select(
        [pl.col(interno).alias(f"s{i}") for i, interno in enumerate(internos)]
    )


def _indice_ocurrencia(nombres, columna, ocurrencia: int):
    """Resuelve un nombre de columna REPETIDO en el crudo, por número de aparición
    (1-based). `_indice` aborta a propósito ante una ambigüedad; esto es la salida
    explícita para los layouts que de verdad repiten un título. Elegir "la primera
    que aparezca" en silencio sería justo el bug que `_indice` existe para evitar,
    así que la config tiene que decir cuál quiere.
    """
    posiciones = [i for i, n in enumerate(nombres) if n.strip() == str(columna).strip()]
    if len(posiciones) < ocurrencia or ocurrencia < 1:
        raise ValueError(
            f"Se pidió la ocurrencia {ocurrencia} de '{columna}', pero aparece "
            f"{len(posiciones)} vez/veces en el encabezado"
        )
    return posiciones[ocurrencia - 1]


def _ultimo_token(nombres, df, params, *_):
    """Deja solo el ÚLTIMO valor de una celda que lista varios.

    En un transbordo el portal lista todos los buques de la ruta en una celda
    ('SEA STAR, OCEAN PEARL'): el que importa es el último, el que arriba a destino.
    """
    col = _col(nombres, params["columna"])
    sep = params.get("separador", ",")
    return nombres, df.with_columns(
        pl.col(col).str.split(sep).list.last().str.strip_chars().fill_null("").alias(col)
    )


def _copiar_columna_si(nombres, df, params, *_):
    """Copia origen -> destino solo en las filas que cumplen una condición.

    El caso típico es el documento de transporte madre/hijo: con `TYPE = Straight`
    va el BL madre, con `TYPE = Consolidated` el BL hijo, y el valor elegido queda
    en las dos columnas. Se declara genérica (columna + valor) para que sirva a
    cualquier regla del mismo tipo.
    """
    condicion = params["si"]
    col_cond = _col(nombres, condicion["columna"])
    origen, destino = _col(nombres, params["origen"]), _col(nombres, params["destino"])
    esperado = str(condicion["igual"]).strip().lower()
    cumple = pl.col(col_cond).str.strip_chars().str.to_lowercase() == esperado
    return nombres, df.with_columns(
        pl.when(cumple).then(pl.col(origen)).otherwise(pl.col(destino)).alias(destino)
    )


def _reemplazar_valor_si(nombres, df, params, *_):
    """Cambia un valor EXACTO de una columna solo en las filas que cumplen una condición.

    Las dos comparaciones ignoran los espacios de los bordes, y la de la condición
    además las mayúsculas. La del valor es exacta: '99990' no es '9999'.
    """
    col = _col(nombres, params["columna"])
    condicion = params["si"]
    col_cond = _col(nombres, condicion["columna"])
    valor, nuevo = str(params["valor"]).strip(), str(params["nuevo"])
    esperado = str(condicion["igual"]).strip().lower()
    cumple = ((pl.col(col).fill_null("").str.strip_chars() == valor)
              & (pl.col(col_cond).fill_null("").str.strip_chars().str.to_lowercase() == esperado))
    return nombres, df.with_columns(
        pl.when(cumple).then(pl.lit(nuevo)).otherwise(pl.col(col)).alias(col)
    )


def _agregar_columna(nombres, df, params, *_):
    """Crea una columna que el crudo no trae, vacía o copiada de otra.

    `copiar_columna` no sirve porque exige que el destino ya exista, y
    `orden_columnas` tampoco: solo selecciona nombres presentes.

    Se agrega SIEMPRE al final; la posición final la decide después
    `orden_columnas`, que es el único lugar donde se declara el layout de salida.
    """
    nombre = params["nombre"]
    if "desde" in params and "valor" in params:
        raise ValueError(f"agregar_columna '{nombre}': usar 'desde' o 'valor', no los dos")
    if "desde" in params:
        valores = pl.col(_col(nombres, params["desde"]))
    else:
        valores = pl.lit(str(params.get("valor", "")))
    # df.columns es c0..cN-1 por el invariante de _normalizar, así que cN es libre
    return nombres + [nombre], df.with_columns(valores.alias(f"c{df.width}"))


def _a_numero(expr):
    """String es-AR ('1.234,50') -> Float64. Vacío/no numérico -> null."""
    limpio = (expr.str.strip_chars()
                  .str.replace_all(".", "", literal=True)
                  .str.replace_all(",", ".", literal=True))
    return pl.when(limpio == "").then(None).otherwise(limpio).cast(pl.Float64, strict=False)


def _deduplicar(nombres, df, params, *_):
    """Deja una sola fila por clave.

    - `criterio: mas_datos` (default): queda la fila con más celdas no vacías. No es
      cosmético: cuando el portal repite una carga, la versión completa no siempre
      es la primera, y quedarse con la primera subiría la incompleta.
    - `criterio: primera`: el desempate de `RemoveDuplicates` de Excel.

    Ante empate gana la primera aparición, las filas con la clave vacía se
    conservan todas (no son duplicados de nada) y el orden original se respeta.

    - `columnas: [a, b]` — clave compuesta, el `CONCATENATE(F, H)` de una columna
      auxiliar en Excel.
    - `sumar: [col]` — las filas que se colapsan no se tiran: su valor se acumula
      en la que queda. Es la tabla dinámica + VLOOKUP de una macro que reinyecta
      "Suma de cantidad"; sin esto el destino recibiría la cantidad de una sola
      línea en lugar del total.
    - `conservar_si_vacia: col` — una fila con esa columna vacía no se deduplica.
      Con clave compuesta, una fila sin factura quedaría agrupada solo por el
      número de parte, y se sumarían cargas que no tienen nada que ver.

    La clave compuesta lleva un separador (`\\x1f`), aunque la macro concatena sin
    nada: sin él `41203377`+`8418KTM07` y `412033778`+`418KTM07` son la misma clave.
    """
    criterio = params.get("criterio", "mas_datos")
    if criterio not in ("mas_datos", "primera"):
        raise ValueError(
            f"'criterio' de deduplicar acepta 'mas_datos' o 'primera', no '{criterio}'")
    if ("columna" in params) == ("columnas" in params):
        raise ValueError("deduplicar: indicar 'columna' (simple) o 'columnas' (clave compuesta)")

    claves = [params["columna"]] if "columna" in params else list(params["columnas"])
    internas = [_col(nombres, c) for c in claves]
    sumar = [_col(nombres, c) for c in (params.get("sumar") or [])]

    # Todas las partes vacías = clave vacía: esas filas no son duplicados de nada.
    partes = [pl.col(c).str.strip_chars() for c in internas]
    clave = (pl.when(pl.all_horizontal([p == "" for p in partes])).then(pl.lit(""))
               .otherwise(pl.concat_str(partes, separator="\x1f")))
    if params.get("conservar_si_vacia"):
        vacia = pl.col(_col(nombres, params["conservar_si_vacia"])).str.strip_chars() == ""
        clave = pl.when(vacia).then(pl.lit("")).otherwise(clave)
    llenas = sum((pl.col(c).str.strip_chars() != "").cast(pl.Int32) for c in df.columns)
    tmp = df.with_row_index("__orden").with_columns([
        llenas.alias("__llenas"),
        clave.alias("__clave"),
    ])

    # Los totales se calculan ANTES de descartar filas: si no, se pierde
    # justamente la cantidad de las líneas que se colapsan.
    if sumar:
        totales = (tmp.filter(pl.col("__clave") != "")
                      .group_by("__clave")
                      .agg([_a_numero(pl.col(c)).sum().alias(f"__sum_{c}") for c in sumar]))

    con_clave = tmp.filter(pl.col("__clave") != "")
    if criterio == "mas_datos":
        con_clave = con_clave.sort(["__llenas", "__orden"], descending=[True, False])
    else:  # 'primera': el desempate de RemoveDuplicates de Excel
        con_clave = con_clave.sort("__orden")
    con_clave = con_clave.unique(subset=["__clave"], keep="first", maintain_order=True)

    if sumar:
        con_clave = con_clave.join(totales, on="__clave", how="left")
        for c in sumar:
            # Se reemite con los decimales que traía el crudo, para no inventar
            # precisión ni truncar la que había.
            dec = int(tmp[c].str.strip_chars().str.extract(r",(\d+)$", 1)
                        .fill_null("").str.len_chars().max() or 0)
            total = pl.col(f"__sum_{c}").round(dec)
            texto = (total.cast(pl.Int64).cast(pl.Utf8) if dec == 0
                     else total.cast(pl.Utf8).str.replace(".", ",", literal=True))
            con_clave = con_clave.with_columns(
                pl.when(pl.col(f"__sum_{c}").is_null()).then(pl.col(c))
                  .otherwise(texto).alias(c)
            ).drop(f"__sum_{c}")

    sin_clave = tmp.filter(pl.col("__clave") == "")
    salida = (pl.concat([con_clave, sin_clave])
                .sort("__orden")
                .drop(["__orden", "__llenas", "__clave"]))
    return nombres, salida


_OPERACIONES = {
    "eliminar_columnas": _eliminar_columnas,
    "copiar_columna": _copiar_columna,
    "copiar_columna_si": _copiar_columna_si,
    "reemplazar_valor_si": _reemplazar_valor_si,
    "renombrar": _renombrar,
    # `filtrar_fecha_minima` es el nombre viejo de la misma operación, cuando solo
    # había cota inferior. Se mantiene registrado para que un YAML sin migrar siga
    # corriendo: sin `dias_adelante` el comportamiento es idéntico.
    "filtrar_fecha_minima": _filtrar_ventana_fecha,
    "filtrar_ventana_fecha": _filtrar_ventana_fecha,
    "filtrar_no_vacias": _filtrar_no_vacias,
    "filtrar_vacias_todas": _filtrar_vacias_todas,
    "filtrar_largo": _filtrar_largo,
    "recortar_largo": _recortar_largo,
    "filtrar_excluir_prefijo": _filtrar_excluir_prefijo,
    "filtrar_contiene": _filtrar_contiene,
    "reemplazar_texto": _reemplazar_texto,
    "vaciar_columna": _vaciar_columna,
    "orden_columnas": _orden_columnas,
    "primer_token": _primer_token,
    "ultimo_token": _ultimo_token,
    "corregir_facturas": _corregir_facturas,
    "homogeneizar_serie": _homogeneizar_serie,
    "rellenar_vacias": _rellenar_vacias,
    "normalizar_entero": _normalizar_entero,
    "cruzar": _cruzar,
    "deduplicar": _deduplicar,
    "agregar_columna": _agregar_columna,
}
