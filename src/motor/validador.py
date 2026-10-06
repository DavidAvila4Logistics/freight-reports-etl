"""Validaciones previas a la salida: replican las causas de rechazo conocidas del
tablero de destino (facturas duplicadas, fechas mal formateadas, caracteres
prohibidos) para no enterarse recién cuando el procesador del destino rebota el archivo.

Cada entrada de `validaciones:` es {tipo: {params..., nivel: error|advertencia}}.
Nivel por defecto: error (bloquea la escritura salvo --forzar).
"""

import polars as pl

from .transformador import _col


def validar(nombres, df, validaciones, fmt_fecha: str):
    """Devuelve lista de hallazgos: dicts {nivel, mensaje}."""
    hallazgos = []
    for val in validaciones:
        (tipo, params), = val.items()
        params = dict(params or {})
        nivel = params.pop("nivel", "error")
        fn = _CHEQUEOS.get(tipo)
        if fn is None:
            raise ValueError(f"Validación desconocida: '{tipo}'. Disponibles: {sorted(_CHEQUEOS)}")
        for mensaje in fn(nombres, df, params, fmt_fecha):
            hallazgos.append({"nivel": nivel, "tipo": tipo, "mensaje": mensaje})
    return hallazgos


def _columnas_requeridas(nombres, df, params, _fmt):
    limpios = [n.strip() for n in nombres]
    for c in params["columnas"]:
        if c not in nombres and c.strip() not in limpios:
            yield f"Falta la columna requerida '{c}' — ¿cambió el layout del agente?"


def _fechas_validas(nombres, df, params, fmt):
    for c in params["columnas"]:
        col = _col(nombres, c)
        s = df[col].str.strip_chars()
        no_vacias = s.filter(s != "")
        malas = no_vacias.filter(no_vacias.str.to_date(fmt, strict=False).is_null())
        if len(malas):
            ejemplos = ", ".join(malas.unique().head(3).to_list())
            yield f"'{c}': {len(malas)} valores no son fecha {fmt.replace('%d','DD').replace('%m','MM').replace('%Y','AAAA')} (ej.: {ejemplos})"


def _sin_duplicados(nombres, df, params, _fmt):
    col = _col(nombres, params["columna"])
    s = df[col].str.strip_chars()
    dups = s.filter((s != "") & s.is_duplicated()).unique()
    if len(dups):
        yield (f"'{params['columna']}': {len(dups)} valores duplicados — el destino exige 1 línea por factura "
               f"(ej.: {', '.join(dups.head(5).to_list())})")


def _duplicados_consistentes(nombres, df, params, _fmt):
    """Los valores repetidos de `clave` deben tener idénticas las `columnas_iguales`
    (misma factura en dos filas con fechas distintas => rechazo del destino)."""
    clave = _col(nombres, params["clave"])
    iguales = [_col(nombres, c) for c in params["columnas_iguales"]]
    g = (df.filter(pl.col(clave).str.strip_chars() != "")
           .group_by(clave)
           .agg([pl.col(c).n_unique().alias(c) for c in iguales]))
    for c, nombre_real in zip(iguales, params["columnas_iguales"]):
        malos = g.filter(pl.col(c) > 1)[clave].to_list()
        if malos:
            yield (f"'{params['clave']}' repetidos con '{nombre_real}' distinto: "
                   f"{', '.join(malos[:5])}{'…' if len(malos) > 5 else ''} — el destino rechaza la inconsistencia")


def _sin_caracter(nombres, df, params, _fmt):
    col = _col(nombres, params["columna"])
    n = int(df[col].str.contains(str(params["caracter"]), literal=True).sum())
    if n:
        yield f"'{params['columna']}': {n} celdas contienen '{params['caracter']}' — quitar antes de subir"


def _no_vacias(nombres, df, params, _fmt):
    """`identificar_por` (opcional) lista QUÉ cargas quedaron sin el dato.

    Sin eso el aviso dice "12 celdas vacías" y no hay por dónde empezar a
    buscarlas. Con la columna que identifica la carga ante el forwarder (la
    guía, el BL, el número de job) el hallazgo ya es una lista de trabajo: son
    exactamente los documentos que hay que pedirle al forwarder.
    """
    col = _col(nombres, params["columna"])
    vacias = df[col].str.strip_chars() == ""
    n = int(vacias.sum())
    if not n:
        return
    mensaje = f"'{params['columna']}': {n} celdas vacías"
    if params.get("identificar_por"):
        ref = df.filter(vacias)[_col(nombres, params["identificar_por"])]
        ids = [v for v in ref.str.strip_chars().to_list() if v]
        if ids:
            mensaje += (f" — {params['identificar_por']}: "
                        f"{', '.join(ids[:10])}{'…' if len(ids) > 10 else ''}")
    yield mensaje


def _patron_columna(nombres, df, params, _fmt):
    """Las celdas no vacías de `columna` deben matchear la expresión `patron`.

    Ejemplo: un contenedor ISO 6346 es `[A-Z]{4}\\d{7}`.

    Pensada para ir como ADVERTENCIA: cuando el origen manda un código mal (un
    dígito de más), la carga se sube igual con el código tal cual llega, y el
    aviso queda para corregirlo después con una corrección de la operación.

    `referencia` (opcional) agrega al mensaje la columna que identifica la carga
    — la factura — así el aviso ya dice qué hay que corregir.
    """
    col = _col(nombres, params["columna"])
    patron = params["patron"]
    s = df[col].str.strip_chars()
    no_vacias = s.filter(s != "")
    if not len(no_vacias):
        return
    malos = no_vacias.filter(~no_vacias.str.contains(f"^(?:{patron})$"))
    if not len(malos):
        return
    detalle = malos.unique().to_list()
    if params.get("referencia"):
        ref = df[_col(nombres, params["referencia"])].str.strip_chars()
        detalle = [f"{c} (FC {r})" for c, r in zip(s.to_list(), ref.to_list())
                   if c and c in set(malos.to_list())]
    yield (f"'{params['columna']}': {len(malos)} valores no cumplen el patrón {patron} — "
           f"se suben tal cual y hay que corregirlos con una corrección de la operación. "
           f"Pendientes: {', '.join(detalle[:5])}{'…' if len(detalle) > 5 else ''}")


def _columnas_iguales(nombres, df, params, _fmt):
    a, b = _col(nombres, params["a"]), _col(nombres, params["b"])
    n = int((df[a] != df[b]).sum())
    if n:
        yield f"'{params['a']}' y '{params['b']}' difieren en {n} filas — deben quedar iguales"


_CHEQUEOS = {
    "columnas_requeridas": _columnas_requeridas,
    "fechas_validas": _fechas_validas,
    "sin_duplicados": _sin_duplicados,
    "duplicados_consistentes": _duplicados_consistentes,
    "sin_caracter": _sin_caracter,
    "no_vacias": _no_vacias,
    "patron_columna": _patron_columna,
    "columnas_iguales": _columnas_iguales,
}


# ── Chequeos automáticos: el contrato de números con el destino ──────────────
# Corren para TODOS los agentes sin declararse en ningún YAML. El dato que
# necesitan ya está en la config (`campos_canonicos`), y una lista por agente
# sería una segunda fuente de verdad que se desactualiza.
#
# El problema que cubren: el lector del destino toma un número pelado largo como
# número y lo guarda con SEIS cifras significativas. Dos facturas distintas de 12
# dígitos (318447005512 y 318447004801) colapsan en `3,18447E+11`; si además el BL
# y el contenedor vienen vacíos, la clave de la carga queda idéntica y el destino
# rechaza el archivo ENTERO por clave duplicada.

# A partir de 12 dígitos un número pelado deja de verse entero del otro lado.
DIGITOS_LARGOS = 12

# Una celda que YA salió en exponencial. Anclado a la celda entera para no
# confundirse con una descripción del portal que contenga algo parecido.
# Es la forma que escriben de verdad Excel (`3,18447E+11`), Python (`1e+16`) y
# Polars (`1.5e+16`): UN dígito antes de la coma y el exponente CON signo. Sin esa
# restricción, un número de viaje de naviera como `05E37` (que Excel abre como
# `5,00E+37`) rechazaría un archivo que está bien.
_EXPONENCIAL = r"^[+-]?\d(?:[.,]\d+)?[eE][+-]\d+$"

# Los campos de `campos_canonicos` que forman la identidad de la carga ante el
# destino. `eta` queda afuera: es una fecha, no un número que pueda colapsar.
_IDENTIDAD = ("factura", "bl", "contenedor")

# Tipos que no se pueden perder entre decenas de avisos de rutina. `main` los
# repite al final, en el bloque REVISAR ANTES DE SUBIR.
TIPOS_CRITICOS = ("clave_destino_colapsada", "notacion_cientifica")


def colapso_destino(valor) -> str:
    """El valor tal como lo va a leer el destino.

    Un número pelado de 12+ dígitos vuelve con seis cifras significativas
    (`318447005512` -> `3.18447E+11`); cualquier otra cosa se compara tal cual,
    en mayúscula, igual que `registro.clave_de`.

    `isascii()` antes de `isdigit()` porque `'²'.isdigit()` es True y `float('²')`
    revienta: una celda con un superíndice del portal tumbaría la corrida.
    """
    v = str(valor or "").strip()
    if v.isascii() and v.isdigit() and len(v) >= DIGITOS_LARGOS:
        return f"{float(v):.5E}"
    return v.upper()


def es_numero_largo(valor) -> bool:
    v = str(valor or "").strip()
    return v.isascii() and v.isdigit() and len(v) >= DIGITOS_LARGOS


def validaciones_automaticas(nombres, df, cfg) -> list:
    """Hallazgos que no dependen de `validaciones:` del YAML. Misma forma que `validar`."""
    hallazgos = []
    hallazgos += list(_notacion_cientifica(nombres, df))
    hallazgos += list(_clave_destino(nombres, df, cfg.get("campos_canonicos") or {}))
    return hallazgos


def _notacion_cientifica(nombres, df):
    """Ninguna celda de ninguna columna puede salir con la forma `3,18447E+11`.

    Es la garantía global, y mira el df FINAL, así que cubre también lo que entra
    por las correcciones. Hoy no se puede disparar desde un entero:
    `lector.formatear_numero` los escribe con `str(int(v))`. Cierra el agujero que
    sí queda — un decimal de 1e16 para arriba, donde `str(float)` de Python pasa
    solo a exponencial — y sirve de alarma si un crudo llega con el número ya
    redondeado desde el origen.

    Va como error (bloquea salvo `--forzar`) y no como excepción del escritor
    justamente para que `--forzar` siga siendo la salida de emergencia y el
    hallazgo quede escrito en hallazgos.log.
    """
    for i, (nombre, c) in enumerate(zip(nombres, df.columns)):
        s = df[c].str.strip_chars()
        malas = s.filter(s.str.contains(_EXPONENCIAL))
        if not len(malas):
            continue
        ejemplos = ", ".join(malas.unique().head(3).to_list())
        etiqueta = nombre.strip() or f"columna {i + 1} (sin título)"
        yield {
            "nivel": "error", "tipo": "notacion_cientifica",
            "mensaje": (f"'{etiqueta}': {len(malas)} celdas salen en notación científica "
                        f"(ej.: {ejemplos}). Ese valor ya perdió dígitos y el destino lo "
                        f"guarda así — el motor no puede reconstruirlo: hay que mirar el "
                        f"crudo del agente."),
        }


def _columnas_identidad(nombres, campos):
    """{campo: columna interna} de los `campos_canonicos` que existen en la salida.

    Una columna que no se resuelve se saltea en silencio a propósito: estos
    chequeos no los declaró nadie, y no pueden voltear una corrida por un YAML que
    apunta a una columna que `orden_columnas` sacó. Si falta de verdad, lo dice
    `columnas_requeridas`, que sí es del agente.
    """
    salida = {}
    for campo in _IDENTIDAD:
        nombre = (campos.get(campo) or "").strip()
        if not nombre:
            continue
        try:
            salida[campo] = _col(nombres, nombre)
        except ValueError:
            continue
    return salida


def _clave_destino(nombres, df, campos):
    """Dos facturas distintas que el destino va a leer como la misma.

    Se agrupa por (factura colapsada, BL, contenedor) —la clave con la que el
    destino identifica la carga— y solo se reporta el grupo que tiene DOS O MÁS
    facturas literalmente distintas. Esa condición es la que permite correrlo en
    todos los agentes sin ruido: en los reportes con una línea por pieza la clave
    repetida es lo normal (varias líneas con la MISMA factura).
    """
    cols = _columnas_identidad(nombres, campos)
    if "factura" not in cols:
        return                      # sin factura no hay clave de destino que mirar

    orden = [c for c in _IDENTIDAD if c in cols]
    valores = [df[cols[c]].to_list() for c in orden]
    grupos, largos = {}, []
    for fila in zip(*valores):
        factura = str(fila[0] or "").strip()
        clave = tuple(colapso_destino(v) for v in fila)
        grupos.setdefault(clave, set()).add(factura)
        largos += [str(v or "").strip() for v in fila if es_numero_largo(v)]

    for clave, facturas in grupos.items():
        if len(facturas) < 2:
            continue
        yield {
            "nivel": "advertencia", "tipo": "clave_destino_colapsada",
            "mensaje": (f"{len(facturas)} facturas DISTINTAS llegan al destino como el mismo "
                        f"valor '{clave[0]}' con el mismo BL y contenedor: "
                        f"{', '.join(sorted(facturas))}. Son números pelados largos y el "
                        f"destino los lee con seis cifras significativas, así que quedan "
                        f"como registros duplicados y RECHAZA EL ARCHIVO ENTERO del agente. "
                        f"Se arregla cuando esas cargas tengan BL o contenedor propio, o "
                        f"con una corrección de la operación."),
        }

    if largos:
        unicos = sorted(set(largos))
        mas = "…" if len(unicos) > 8 else ""
        yield {
            "nivel": "advertencia", "tipo": "numero_largo_identidad",
            "mensaje": (f"{len(unicos)} valores de identidad son números pelados de "
                        f"{DIGITOS_LARGOS}+ dígitos y el destino los compara redondeados "
                        f"({unicos[0]} -> {colapso_destino(unicos[0])}): "
                        f"{', '.join(unicos[:8])}{mas}. Hoy no chocan entre sí, pero "
                        f"si el destino además los GUARDA así, el día que llegue la "
                        f"factura corregida va a dar de alta una carga nueva en vez de "
                        f"pisar la vieja."),
        }
