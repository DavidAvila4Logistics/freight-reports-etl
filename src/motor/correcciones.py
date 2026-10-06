"""Correcciones de fila sobre el CSV final — `data/correcciones/<CSV de salida>.csv`.

El problema que resuelve: la operación detecta que una carga se subió con un dato
mal (un contenedor mal tipeado, un ETA corrido), lo arregla en el destino, y a la
corrida siguiente el portal vuelve a mandar el valor malo y el destino lo pisa otra
vez. Sin esto no hay forma de decirle al motor "para esta carga, este valor es el
bueno", y el mismo error se re-sube todos los días.

Un archivo por CSV final, con el encabezado EXACTO de la salida del agente. La
operación pega la fila, borra todo menos la clave y el dato corregido, y listo:

    HAWB;...;Container;...;_match
    HX1000001;;ABCU7654321;;                     <- clave intacta: `_match` vacía
    ;;ABCU7654321;;HAWB=HX1000001                <- arregla el Container, que ES clave

Reglas:

- **Celda vacía = no tocar.** Solo pisan las celdas con valor. Una fila pegada a
  medias borraría 40 columnas buenas sin que nadie se entere. Para blanquear a
  propósito está el centinela `<VACIO>`.
- **Nunca anexa filas.** Si la carga no está en el CSV de hoy, se avisa y nada más:
  al destino no le llega una fila que el portal no mandó.
- **Las reglas de días salen gratis.** Como solo se tocan filas que ya están en la
  salida, una carga que la ventana de días dejó afuera simplemente
  no se corrige. La corrección vive y muere con la fila de `processed`.
- **`_match` para cuando lo que está mal es la clave.** La fila de corrección lleva
  el valor NUEVO y el portal manda el VIEJO: no matchean. Una misma celda no puede
  ser criterio de búsqueda y valor nuevo a la vez, así que la coordenada va aparte.

La capa corre al FINAL, sobre la salida ya armada: arregla lo que se sube sin
reprocesar nada. Los esqueletos (solo encabezado) los crea
`tools/crear_esqueleto_correcciones.py`.
"""

import datetime as dt
import shutil
import traceback
from pathlib import Path

import openpyxl
import polars as pl

from .lector import formatear_numero
from .registro import clave_de
from .transformador import _indice

# Se escribe en una celda para blanquearla a propósito. Sin él, vacío es "no tocar",
# que es el default seguro.
CENTINELA_VACIO = "<VACIO>"

# Columnas de control: van DESPUÉS del encabezado de salida y no salen en el CSV.
CONTROL = ("_match", "_motivo")


def _celda_a_texto(valor, formato_fecha: str) -> str:
    """Una celda de Excel a texto plano, como si alguien la hubiera tipeado en un CSV.

    A propósito NO es `lector.canonicalizar()`: esa función aplica reglas de negocio
    por agente (`formato_cfg["numeros"]` y `columnas_fecha` están indexadas por el
    nombre de columna del CRUDO DEL PORTAL, no por el de la salida que usa
    este archivo) y acá aplicaría specs pensadas para otras columnas. Esto es más
    simple: lo que la persona tipeó se respeta tal cual, solo se resuelve el tipo de
    dato que Excel le puso a la celda.
    """
    if valor is None:
        return ""
    if isinstance(valor, (dt.datetime, dt.date)):
        return valor.strftime(formato_fecha)
    if isinstance(valor, bool):
        return str(valor)
    if isinstance(valor, (int, float)):
        return formatear_numero(valor)
    # saltos de línea dentro de la celda (alt-Enter de Excel) romperían el CSV sin
    # comillas de la salida, igual que en `lector.canonicalizar`.
    texto = str(valor).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return texto.strip()


def _hoja_a_lineas(ruta_xlsx: Path, separador: str, formato_fecha: str) -> list[str]:
    """El `.xlsx` volcado línea a línea, en el mismo layout de texto que el `.csv`."""
    wb = openpyxl.load_workbook(ruta_xlsx, read_only=True, data_only=True)
    try:
        filas = [tuple(f) for f in wb.active.iter_rows(values_only=True)]
    finally:
        wb.close()
    while filas and all(v is None or str(v).strip() == "" for v in filas[-1]):
        filas.pop()   # cola de filas vacías, típica de Excel
    if not filas:
        return []
    encabezado = [("" if v is None else str(v).strip()) for v in filas[0]]
    lineas = [separador.join(encabezado)]
    lineas += [separador.join(_celda_a_texto(v, formato_fecha) for v in fila)
              for fila in filas[1:]]
    return lineas


def _destino_libre(dir_backup: Path, nombre: str) -> Path:
    """`dir_backup/nombre`, o con `_2`, `_3`... si dos conversiones caen en el mismo
    segundo (poco probable con un archivo que edita una sola persona a mano, pero
    gratis de cubrir)."""
    destino = dir_backup / nombre
    stem, suf = destino.stem, destino.suffix
    n = 2
    while destino.exists():
        destino = dir_backup / f"{stem}_{n}{suf}"
        n += 1
    return destino


def resolver_excel_pendiente(ruta_csv: Path, dir_backup: Path, separador: str,
                             encoding: str, formato_fecha: str, avisos: list) -> bool:
    """Si `ruta_csv` tiene al lado un `.xlsx` con su mismo nombre y más nuevo, lo
    vuelca a texto y REEMPLAZA el `.csv`. Devuelve si convirtió algo.

    Pasa cuando alguien guarda el archivo de correcciones desde Excel eligiendo
    "Usar formato de Excel" en vez de mantener CSV: el motor busca `<nombre>.csv` por nombre exacto, y ese
    `.xlsx` — con los datos buenos adentro — es invisible para la corrida hasta que
    alguien nota que "la corrección no se aplicó". Esto lo resuelve solo.

    Nunca puede tumbar la corrida: TODO el cuerpo está en un único `try/except`, y si
    algo sale mal (el archivo abierto en Excel, un permiso) se avisa y se sigue con
    el `.csv` que ya había — el mismo comportamiento que si esta función no existiera.
    """
    try:
        ruta_xlsx = ruta_csv.with_suffix(".xlsx")
        if not ruta_xlsx.exists():
            return False

        if ruta_csv.exists() and ruta_csv.stat().st_mtime >= ruta_xlsx.stat().st_mtime:
            # El .xlsx es un sobrante viejo (ya se corrigió a mano después, o es de
            # una prueba anterior): no pisar un .csv que puede ser más nuevo. Mismo
            # criterio de "gana el más reciente" que usa el motor para elegir el
            # crudo en data/raw/.
            avisos.append({
                "nivel": "advertencia", "tipo": "correccion_excel_viejo",
                "mensaje": (f"{ruta_xlsx.name} existe pero es más viejo que "
                            f"{ruta_csv.name} — no se tocó. Si ya no hace falta, "
                            f"borrarlo a mano."),
            })
            return False

        lineas = _hoja_a_lineas(ruta_xlsx, separador, formato_fecha)
        if not lineas:
            avisos.append({
                "nivel": "advertencia", "tipo": "correccion_excel_vacio",
                "mensaje": f"{ruta_xlsx.name} está vacío — no se convirtió nada.",
            })
            return False

        dir_backup.mkdir(parents=True, exist_ok=True)
        sello = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        if ruta_csv.exists():
            shutil.copy2(ruta_csv, _destino_libre(dir_backup, f"{ruta_csv.stem}_{sello}.csv"))
        shutil.move(str(ruta_xlsx), str(_destino_libre(dir_backup, f"{ruta_xlsx.stem}_{sello}.xlsx")))

        with open(ruta_csv, "w", encoding=encoding, errors="replace", newline="") as f:
            f.write("\r\n".join(lineas) + "\r\n")

        avisos.append({
            "nivel": "advertencia", "tipo": "correccion_excel_convertido",
            "mensaje": (f"{ruta_xlsx.name} se guardó como Excel en vez de CSV — se "
                        f"convirtió solo y reemplazó a {ruta_csv.name} "
                        f"({len(lineas) - 1} filas). El .csv anterior y el .xlsx "
                        f"quedaron en back_up_data_used/correcciones/."),
        })
        return True
    except Exception as e:
        avisos.append({
            "nivel": "advertencia", "tipo": "correccion_excel_error",
            "mensaje": (f"{ruta_csv.name}: había un .xlsx para convertir pero falló "
                        f"({type(e).__name__}: {e}) — se sigue con el .csv que ya "
                        f"había."),
        })
        traceback.print_exc(limit=1)
        return False


def _partir_encabezado(cabecera):
    """(bloque de salida, nombres de control). Corta en la primera columna `_algo`.

    Ninguna columna de los reportes empieza con '_', así que el corte no es
    ambiguo y la operación puede agregar `_match` sin tocar nada más.
    """
    for i, n in enumerate(cabecera):
        if n.strip().startswith("_"):
            return cabecera[:i], [c.strip() for c in cabecera[i:]]
    return list(cabecera), []


def _alinear(bloque, nombres):
    """¿El encabezado del archivo es el de la salida? Devuelve (ajuste, detalle).

    `ajuste` es cuántas columnas hay que agregarle (>0) o sacarle (<0) a cada fila;
    `detalle` es no vacío cuando NO alinea. Se toleran las columnas terminadoras
    vacías de más o de menos: un esqueleto armado a mano sin el ';' final que sí
    lleva la salida no es motivo para no aplicar una corrección. Cualquier otra
    diferencia sí lo es: aplicar por posición contra un encabezado viejo escribiría
    el valor en la columna equivocada.
    """
    b = list(bloque)
    sobrantes = 0
    while len(b) > len(nombres) and not b[-1].strip():
        b.pop()
        sobrantes += 1
    faltantes = 0
    while len(b) < len(nombres) and not nombres[len(b)].strip():
        b.append(nombres[len(b)])
        faltantes += 1
    if len(b) != len(nombres):
        return 0, (f"el archivo tiene {len(bloque)} columnas y el CSV de salida "
                   f"{len(nombres)}")
    for i, (a, n) in enumerate(zip(b, nombres)):
        if a.strip() != n.strip():
            return 0, (f"la columna {i + 1} del archivo dice '{a}' y la del CSV de "
                       f"salida '{n}'")
    return faltantes - sobrantes, ""


def cargar(ruta, encoding, separador, nombres, avisos):
    """Lee el archivo de correcciones del agente. Devuelve la lista de filas.

    Cada fila es {"valores": [...], "control": {...}, "linea": N}, con `valores` ya
    del ancho exacto de la salida.

    Se parsea con un split directo y no con `csv.reader`, igual que el registro
    maestro (`registro.cargar_registro`) y por el mismo motivo: el CSV de salida va
    sin quoting, y un `csv.reader` tomaría por comilla de apertura las comillas
    sueltas de las descripciones y se comería el resto de la fila.
    """
    ruta = Path(ruta)
    if not ruta.exists():
        return []
    lineas = ruta.read_text(encoding=encoding, errors="replace").splitlines()
    lineas = [l for l in lineas if l.strip(separador).strip()]
    if len(lineas) < 2:
        return []       # vacío o solo encabezado: el esqueleto sin cargar

    cabecera = lineas[0].split(separador)
    bloque, control = _partir_encabezado(cabecera)
    corte = len(bloque)
    ajuste, detalle = _alinear(bloque, nombres)

    if detalle:
        avisos.append({
            "nivel": "error", "tipo": "correccion_encabezado",
            "mensaje": (f"{ruta.name} tiene {len(lineas) - 1} correcciones pero su "
                        f"encabezado no coincide con la salida ({detalle}). NO se "
                        f"aplicó ninguna: por posición se escribiría en la columna "
                        f"equivocada. Regenerar el encabezado con "
                        f"tools/crear_esqueleto_correcciones.py"),
        })
        return []

    desconocidas = [c for c in control if c not in CONTROL]
    if desconocidas:
        avisos.append({
            "nivel": "advertencia", "tipo": "correccion_control_desconocida",
            "mensaje": (f"{ruta.name}: columnas de control ignoradas {desconocidas} "
                        f"— las que el motor entiende son {list(CONTROL)}"),
        })

    filas = []
    for n, linea in enumerate(lineas[1:], start=2):
        campos = linea.split(separador)
        campos += [""] * (len(cabecera) - len(campos))
        valores = campos[:corte]
        if ajuste > 0:
            valores += [""] * ajuste
        elif ajuste < 0:
            valores = valores[:len(nombres)]
        filas.append({"valores": valores,
                      "control": dict(zip(control, campos[corte:])),
                      "linea": n})
    return filas


def _pares_match(texto, nombres):
    """'Col=valor|Otra=valor' -> [(indice, valor)]. Parte en el PRIMER '=' de cada par.

    Varios pares porque hay claves de tres columnas (parts_shipper): arreglar una
    obliga a nombrar las otras dos.
    """
    pares = []
    for trozo in texto.split("|"):
        trozo = trozo.strip()
        if not trozo:
            continue
        if "=" not in trozo:
            raise ValueError(f"'{trozo}' no tiene forma Columna=valor")
        col, valor = trozo.split("=", 1)
        pares.append((_indice(nombres, col.strip()), valor.strip()))
    if not pares:
        raise ValueError("está vacío")
    return pares


def aplicar(nombres, df, filas, columnas_clave, avisos, origen=""):
    """Pisa en `df` las celdas declaradas. Devuelve (df, tocadas, renombradas).

    `tocadas` son las claves (ya corregidas) de las filas que cambiaron, y
    `renombradas` el mapa {clave_vieja: clave_nueva} de las correcciones que tocaron
    una columna de la clave. Las dos van al registro maestro: sin `renombradas`, una
    carga a la que se le corrige la clave se da de alta como NUEVA y la vieja queda
    huérfana en el registro.
    """
    tocadas, renombradas = set(), {}
    if not filas:
        return df, tocadas, renombradas

    idx_clave = [_indice(nombres, c) for c in columnas_clave]
    columnas = {c: df[c].to_list() for c in df.columns}
    alto = df.height

    def clave_en(valores):
        """La clave de negocio de una fila, con el MISMO normalizado que el registro."""
        return clave_de({c: valores[i] for c, i in zip(columnas_clave, idx_clave)},
                        columnas_clave)

    def clave_del_dia(i):
        return clave_de({c: columnas[f"c{j}"][i] for c, j in zip(columnas_clave, idx_clave)},
                        columnas_clave)

    claves_dia = [clave_del_dia(i) for i in range(alto)]
    por_clave = {}
    for i, k in enumerate(claves_dia):
        por_clave.setdefault(k, []).append(i)

    # Una firma repetida = dos correcciones para la misma carga. No se elige ninguna:
    # ante dos candidatos el motor avisa, porque quedarse con la última
    # dependería del orden en que se pegaron las filas en el archivo.
    firmas = {}
    for f in filas:
        firma = ((f.get("control", {}).get("_match") or "").strip().upper()
                 or clave_en(f["valores"]))
        firmas[firma] = firmas.get(firma, 0) + 1

    celdas, sin_destino, repetidas = 0, [], set()

    for f in filas:
        valores = f["valores"]
        control = f.get("control", {})
        match = (control.get("_match") or "").strip()
        motivo = (control.get("_motivo") or "").strip()
        etiqueta = f"línea {f['linea']}"

        if match:
            firma = match.upper()
            try:
                pares = _pares_match(match, nombres)
            except ValueError as e:
                avisos.append({"nivel": "advertencia", "tipo": "correccion_ambigua",
                               "mensaje": f"{etiqueta}: `_match` inválido ({e}) — no se aplicó"})
                continue
            objetivos = [i for i in range(alto)
                         if all((columnas[f"c{j}"][i] or "").strip().upper() == v.upper()
                                for j, v in pares)]
            etiqueta = f"{etiqueta} (_match {match})"
        else:
            clave = clave_en(valores)
            firma = clave
            faltan = [c for c, i in zip(columnas_clave, idx_clave)
                      if not (valores[i] or "").strip()]
            if faltan:
                avisos.append({
                    "nivel": "advertencia", "tipo": "correccion_clave_incompleta",
                    "mensaje": (f"{etiqueta}: la clave viene incompleta (falta {faltan}) "
                                f"y una clave a medias matchearía cargas que no son. "
                                f"Completarla, o usar `_match` si es la clave lo que "
                                f"hay que corregir"),
                })
                continue
            objetivos = list(por_clave.get(clave, []))
            etiqueta = f"{etiqueta} ({clave})"

        if firmas.get(firma, 0) > 1:
            repetidas.add(firma)
            continue
        if not objetivos:
            sin_destino.append(etiqueta)
            continue
        # Con `_match` se exige unicidad SOLO si la corrección escribe una columna de la
        # clave: ahí se reescribe la identidad de la carga, y aplicar lo mismo a dos
        # filas las colapsaría en una sola clave. Si no la toca, varias filas es lo
        # esperado: en parts_shipper una factura trae cientos de piezas, y
        # corregirle el ETA con una fila por pieza serían cientos de filas. Con
        # `_match: Invoice Number=…` alcanza con una.
        # Sin `_match` también se aceptan varias: hay reportes que repiten clave.
        escribe_clave = any((valores[i] or "").strip() for i in idx_clave)
        if match and len(objetivos) > 1 and escribe_clave:
            avisos.append({
                "nivel": "advertencia", "tipo": "correccion_ambigua",
                "mensaje": (f"{etiqueta}: matchea {len(objetivos)} filas y no se puede "
                            f"elegir — agregar columnas al `_match` para que sea unívoco"),
            })
            continue

        toco_clave = False
        detalles = []
        for i in objetivos:
            antes = claves_dia[i]
            cambios = []
            for p, bruto in enumerate(valores):
                if not (bruto or "").strip():
                    continue
                nuevo = "" if bruto.strip() == CENTINELA_VACIO else bruto
                actual = columnas[f"c{p}"][i]
                if actual == nuevo:
                    continue
                columnas[f"c{p}"][i] = nuevo
                cambios.append((p, actual, nuevo))
                if p in idx_clave:
                    toco_clave = True
            if not cambios:
                continue
            celdas += len(cambios)
            detalles.append("; ".join(
                f"'{nombres[p] or 'columna ' + str(p + 1)}': '{viejo}' -> '{nuevo}'"
                for p, viejo, nuevo in cambios))
            despues = clave_del_dia(i)
            claves_dia[i] = despues
            tocadas.add(despues)
            if despues != antes:
                renombradas[antes] = despues

        # Un aviso por corrección y no por fila: con `_match` por factura, una sola
        # corrección de parts_shipper toca cientos de piezas y taparía el resto del log.
        if detalles:
            cuantas = f"{len(detalles)} filas, la primera " if len(detalles) > 1 else ""
            avisos.append({
                "nivel": "advertencia", "tipo": "correccion_fila",
                "mensaje": (f"{etiqueta}: {cuantas}{detalles[0]}"
                            + (f" — {motivo}" if motivo else "")),
            })

        if toco_clave:
            avisos.append({
                "nivel": "advertencia", "tipo": "correccion_identidad_destino",
                "mensaje": (f"{etiqueta}: la corrección cambia una columna de la CLAVE "
                            f"({columnas_clave}). En el destino la carga vieja NO se pisa: "
                            f"entra una nueva y hay que borrar la anterior a mano"),
            })

    if repetidas:
        avisos.append({
            "nivel": "advertencia", "tipo": "correccion_ambigua",
            "mensaje": ("hay dos correcciones para la misma carga y no se aplicó ninguna "
                        "(dejar una sola): " + "; ".join(sorted(repetidas)[:5])),
        })
    if sin_destino:
        # Agrupado a propósito: el archivo de correcciones crece para siempre, y a
        # los tres meses una línea por corrección vencida tapa el resto del log.
        avisos.append({
            "nivel": "advertencia", "tipo": "correccion_sin_destino",
            "mensaje": (f"{len(sin_destino)} correcciones declaradas cuya carga no está "
                        f"en el CSV de hoy (arribada, o fuera de la ventana de días): "
                        + "; ".join(sin_destino[:5])),
        })
    if celdas:
        print(f"  Correcciones{' (' + origen + ')' if origen else ''}: {celdas} celdas "
              f"en {len(tocadas)} cargas")

    df = df.with_columns([pl.Series(c, v, dtype=pl.Utf8) for c, v in columnas.items()])
    return df, tocadas, renombradas
