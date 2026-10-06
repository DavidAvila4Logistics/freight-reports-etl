"""Archivo maestro acumulativo por agente: `data/registro/<agente>.csv`.

El motor es stateless por diseño — cada corrida pisa el CSV de `processed/` y el
destino hace match por factura, así que re-subir es idempotente. Eso alcanza para
cargar, pero no para saber QUÉ se cargó ni CUÁNDO, ni para ver qué cambió de un
día al otro.

El registro es esa memoria: una fila por carga (clave de negocio declarada en
`registro.clave` de cada YAML), que se actualiza en cada corrida y NUNCA se
borra. Una carga que deja de venir en el portal conserva su última foto y su
`_ultima_carga` vieja.

Formato: el mismo dialecto que el CSV de salida (';', cp1252, CRLF), para que se
abra en Excel igual que el resto y para que pasarlo a una base de datos sea copiar
el archivo, no reescribir el motor.
"""

import datetime as dt
import hashlib
from pathlib import Path

import polars as pl

from .transformador import _indice

# Metadatos que el registro agrega a las columnas de salida del agente. El
# prefijo '_' los separa a ojo de las columnas de salida y evita chocar con un
# título del portal.
META = ["_clave", "_primera_carga", "_ultima_carga", "_origen", "_estado", "_corregido_por", "_hash"]

# Valores de _estado. `corregido` lo escribe la capa de correcciones.
NUEVO, SIN_CAMBIOS, MODIFICADO, CORREGIDO = "nuevo", "sin_cambios", "modificado", "corregido"


def esta_activo(cfg_registro) -> bool:
    """`registro: {activo: false}` para lo que no es una carga (un reporte de
    movimientos de stock no tiene factura ni BL: no es un embarque)."""
    return bool(cfg_registro) and cfg_registro.get("activo", True)


def clave_de(fila: dict, columnas_clave) -> str:
    """Clave de negocio normalizada. Mayúsculas y sin espacios porque el mismo
    número de factura vuelve del portal con distinto casing entre corridas."""
    return "|".join(str(fila.get(c, "")).strip().upper() for c in columnas_clave)


def _hash_de(fila: dict, columnas_negocio) -> str:
    crudo = "\x1f".join(str(fila.get(c, "")) for c in columnas_negocio)
    return hashlib.sha1(crudo.encode("utf-8")).hexdigest()[:12]


def titulos_registro(nombres) -> list[str]:
    """Un encabezado único por columna de salida, conservando la posición.

    Varios reportes repiten un título a propósito y el destino los espera así:
    hay reportes que cierran con dos columnas sin nombre, y transit_report trae
    'Loaded for pre-carriage' dos veces. Esas dos **no** llevan el mismo valor:
    la primera es la fecha ya normalizada (`27/08/2026`) y la segunda el crudo del
    portal (`27.08.2026 00:00`).

    Un dict por nombre colapsaría las dos en una sola y escribiría el valor crudo
    en la columna que el destino espera normalizada. Por
    eso el registro guarda una columna por posición: la primera aparición se
    queda con el título tal cual (así las claves declaradas en el YAML siguen
    resolviendo) y las siguientes llevan sufijo `#2`, `#3`. Las columnas sin
    título pasan a `_colN`, que es su posición.
    """
    vistos, salida = {}, []
    for i, n in enumerate(nombres):
        base = n if n.strip() else f"_col{i}"
        vistos[base] = vistos.get(base, 0) + 1
        salida.append(base if vistos[base] == 1 else f"{base}#{vistos[base]}")
    return salida


def _a_filas(titulos, df) -> list[dict]:
    """(titulos_registro, df) -> lista de dicts, uno por fila, sin colapsar
    columnas repetidas (ver `titulos_registro`)."""
    return [dict(zip(titulos, fila)) for fila in df.iter_rows()]


def cargar_registro(ruta: Path, encoding: str, separador: str):
    """Lee el registro anterior. Si no existe todavía, devuelve vacío.

    Se parsea con un split directo y no con `csv.reader` ni `leer_crudo`: el
    archivo lo escribió `escribir_csv`, que garantiza que ninguna celda contiene
    el separador y que no hay quoting. Un `csv.reader` interpretaría como comilla
    de apertura los `"` sueltos que traen las descripciones de los portales
    ('CYLINDER 3" NPT') y se comería el resto de la fila.
    """
    ruta = Path(ruta)
    if not ruta.exists():
        return [], []
    lineas = ruta.read_text(encoding=encoding).splitlines()
    lineas = [l for l in lineas if l.strip(separador).strip()]
    if not lineas:
        return [], []
    nombres = lineas[0].split(separador)
    filas = []
    for l in lineas[1:]:
        campos = l.split(separador)
        campos += [""] * (len(nombres) - len(campos))
        filas.append(dict(zip(nombres, campos)))
    return nombres, filas


def upsert(nombres, df, cfg_registro, nombres_prev, filas_prev, fecha_corrida: dt.date,
           origen: str, corregidas=frozenset(), renombradas=None):
    """Funde la corrida actual contra el registro anterior.

    Devuelve `(nombres_registro, df_registro, resumen)`. `resumen` es
    {'nuevos': N, 'modificados': N, 'sin_cambios': N, 'historicos': N} —
    `historicos` son las filas que ya no vienen en el portal y se conservan.

    `corregidas` son las claves que pisó `data/correcciones/`: se marcan con
    `_estado=corregido` y `_corregido_por`, que es la traza de por qué esa fila
    difiere de lo que mandó el portal.

    `renombradas` es {clave_vieja: clave_nueva} de las correcciones que tocaron una
    columna de la CLAVE. Sin eso la carga corregida cambia de identidad, el upsert
    no la encuentra y la da de alta como NUEVA: la misma carga queda dos veces en el
    registro (ver `config._avisar_clave_corregible`). Con el mapa, la fila histórica se
    rebautiza y se actualiza, conservando su `_primera_carga`.
    """
    columnas_clave = cfg_registro.get("clave")
    if not columnas_clave:
        raise ValueError("La sección 'registro' debe declarar 'clave' (columnas que "
                         "identifican una carga) o marcar 'activo: false'")
    for c in columnas_clave:
        _indice(nombres, c)  # falla acá y no en silencio si el portal cambió el título

    # Las columnas de negocio del registro son las de la salida de hoy, una por
    # posición. Si el layout del agente cambió, las columnas viejas se descartan
    # y las nuevas arrancan vacías en las filas históricas.
    negocio = titulos_registro(nombres)
    hoy = f"{fecha_corrida:%d/%m/%Y}"

    renombradas = renombradas or {}
    previas, vistas_prev = {}, {}
    for f in filas_prev:
        clave_prev = renombradas.get(f.get("_clave", ""), f.get("_clave", ""))
        interna_prev, _ = _clave_interna(clave_prev, f.get("_hash", ""), vistas_prev)
        previas[interna_prev] = f

    resumen = {"nuevos": 0, "modificados": 0, "sin_cambios": 0, "historicos": 0,
               "clave_repetida": 0, "corregidos": 0}
    actualizadas, vistas = {}, {}

    for fila in _a_filas(negocio, df):
        clave = clave_de(fila, columnas_clave)
        h = _hash_de(fila, negocio)
        interna, ordinal = _clave_interna(clave, h, vistas)
        if ordinal > 1:
            resumen["clave_repetida"] += 1
        anterior = previas.get(interna)

        if anterior is None:
            estado, primera, corregido_por = NUEVO, hoy, ""
            resumen["nuevos"] += 1
        else:
            cambio = anterior.get("_hash", "") != h
            estado = MODIFICADO if cambio else SIN_CAMBIOS
            primera = anterior.get("_primera_carga") or hoy
            # Una corrección aplicada antes se conserva: es la traza de por qué
            # esa fila difiere de lo que manda el portal.
            corregido_por = anterior.get("_corregido_por", "")
            resumen["modificados" if cambio else "sin_cambios"] += 1

        # La corrección manual gana: es una persona mirando el documento, y su
        # marca en el registro explica por qué la fila difiere del portal.
        if clave in corregidas:
            estado, corregido_por = CORREGIDO, "correcciones"
            resumen["corregidos"] += 1

        salida = {c: fila.get(c, "") for c in negocio}
        salida.update({"_clave": clave, "_primera_carga": primera, "_ultima_carga": hoy,
                       "_origen": origen, "_estado": estado,
                       "_corregido_por": corregido_por, "_hash": h})
        actualizadas[interna] = salida

    # Las filas del registro que hoy no vinieron se conservan intactas, en su
    # posición original: son cargas ya arribadas que el portal dejó de listar.
    finales = []
    for interna, vieja in previas.items():
        if interna in actualizadas:
            finales.append(actualizadas.pop(interna))
        else:
            finales.append({c: vieja.get(c, "") for c in negocio + META})
            resumen["historicos"] += 1
    finales.extend(actualizadas.values())

    cols = negocio + META
    datos = {f"c{i}": [str(f.get(c, "") or "") for f in finales] for i, c in enumerate(cols)}
    df_reg = pl.DataFrame(datos, schema={f"c{i}": pl.Utf8 for i in range(len(cols))})
    return cols, df_reg, resumen


def _clave_interna(clave: str, h: str, vistas: dict):
    """Clave de matcheo entre corridas. Devuelve `(interna, ordinal)`.

    Dos ajustes sobre la clave de negocio, los dos por casos reales:

    - Clave vacía: pasa con facturas que el portal todavía no asignó. Se cae al
      hash del contenido, así una fila sin factura no se duplica en cada corrida
      pero tampoco se confunde con otra distinta.
    - Clave repetida: hay reportes con varias líneas con la misma factura y el
      mismo producto, y no hay combinación de columnas que las separe. Se
      desempata por orden de aparición (1ª con 1ª, 2ª con 2ª). No es perfecto si
      el portal reordena las filas, pero es determinista y —lo que importa— no
      colapsa dos filas en una: sin esto el registro perdería cargas.
    """
    base = clave if clave.strip("|") else "\x00" + h
    ordinal = vistas.get(base, 0) + 1
    vistas[base] = ordinal
    return f"{base}\x02{ordinal}", ordinal
