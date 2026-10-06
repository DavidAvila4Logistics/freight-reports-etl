"""Escritura del CSV final con el contrato exacto del destino:
separador ';', encoding cp1252, CRLF, sin quoting (por eso ninguna celda
puede contener ';' — los configs lo reemplazan por ',' y acá se re-verifica).
"""

from pathlib import Path


def escribir_csv(nombres, df, ruta: Path, separador=";", encoding="cp1252"):
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)

    con_sep = [n for n, c in zip(nombres, df.columns)
               if bool(df[c].str.contains(separador, literal=True).any())]
    if con_sep:
        raise ValueError(
            f"Celdas con '{separador}' en {con_sep} — agregar 'reemplazar_texto' en la config; "
            "el CSV de salida no lleva comillas y se rompería"
        )

    try:
        with open(ruta, "w", encoding=encoding, errors="replace", newline="") as f:
            f.write(separador.join(nombres) + "\r\n")
            for fila in df.iter_rows():
                f.write(separador.join(fila) + "\r\n")
    except PermissionError:
        # Windows bloquea el archivo mientras Excel lo tiene abierto, y el
        # traceback crudo no lo dice por ningún lado. Pasa seguido: se revisa
        # el CSV de ayer, se deja abierto, y a la mañana siguiente muere el lote.
        raise PermissionError(
            f"No se pudo escribir {ruta}: el archivo está abierto en otro "
            f"programa (casi siempre Excel). Cerralo y volvé a correr."
        ) from None
    return ruta
