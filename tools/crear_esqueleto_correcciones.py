"""Crea y verifica los esqueletos de `data/correcciones/<CSV final>.csv`.

Las correcciones se aplican **por posición** contra el encabezado del CSV de salida.
Si el encabezado del archivo no es exactamente el de la salida, el valor corregido
se escribiría en la columna de al lado, así que el motor prefiere no aplicar nada y
avisar. Este script es el que mantiene los dos encabezados sincronizados.

El encabezado que escribe es el de la salida MÁS las columnas de control `_match` y
`_motivo`, que van siempre: quien carga las correcciones es la operación desde Excel,
y agregar una columna a mano al final de un encabezado de 44 es el paso con más
chances de salir mal.

La fuente de verdad es `data/processed/<CSV final>.csv`, o sea lo que el motor
emitió de verdad la última vez: si el layout cambió, el archivo de correcciones tiene
que seguir al motor.

Uso:
    python tools/crear_esqueleto_correcciones.py              # crea los que faltan
    python tools/crear_esqueleto_correcciones.py --verificar   # solo informa
    python tools/crear_esqueleto_correcciones.py --agente parts_shipper

NUNCA toca un archivo que tenga correcciones cargadas: si a ese le quedó el
encabezado viejo, lo dice y la decisión de reescribirlo es de la operación.
`data/` no está en git, así que en una PC nueva este script es también la forma de
crear la carpeta.
"""

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from motor.config import DIR_CONFIGS, cargar_config_agente, cargar_config_global  # noqa: E402
from motor.correcciones import CONTROL, _alinear, _partir_encabezado  # noqa: E402


def _control_final(control):
    """Las columnas de control que va a llevar el encabezado, en orden fijo.

    `_match` y `_motivo` van SIEMPRE, aunque el archivo esté vacío: quien carga las
    correcciones es la operación desde Excel, y agregar a mano una columna al final
    de un encabezado de 44 es justo el paso que más chances tiene de salir mal (una
    de más y el motor no aplica nada). Se conservan al final las que el archivo
    tuviera y el motor no entienda: no son suyas para borrarlas.
    """
    return list(CONTROL) + [c for c in control if c not in CONTROL]


def leer_encabezado(ruta: Path, encoding: str, separador: str):
    """(encabezado, cantidad de filas de datos). (None, 0) si el archivo no existe."""
    if not ruta.exists():
        return None, 0
    lineas = ruta.read_text(encoding=encoding, errors="replace").splitlines()
    lineas = [l for l in lineas if l.strip(separador).strip()]
    if not lineas:
        return None, 0
    return lineas[0].split(separador), len(lineas) - 1


def revisar(agente: str, cfg_global: dict, verificar: bool) -> str:
    """Devuelve el estado del esqueleto de un agente: OK / CREADO / ... ."""
    cfg = cargar_config_agente(agente)
    nombre = cfg["salida"]["nombre"]
    sep, enc = cfg["salida"]["separador"], cfg["salida"]["encoding"]

    fuente = Path(cfg_global["dir_processed"]) / nombre
    salida, _ = leer_encabezado(fuente, enc, sep)
    if salida is None:
        return f"SIN FUENTE  {nombre:<24} correr el agente primero: no hay {fuente}"

    destino = Path(cfg_global["dir_correcciones"]) / nombre
    actual, filas = leer_encabezado(destino, enc, sep)

    if actual is None:
        if verificar:
            return f"FALTA       {nombre:<24} no existe (se crea sin --verificar)"
        destino.parent.mkdir(parents=True, exist_ok=True)
        with open(destino, "w", encoding=enc, errors="replace", newline="") as f:
            f.write(sep.join(salida + list(CONTROL)) + "\r\n")
        return (f"CREADO      {nombre:<24} {len(salida)} columnas "
                f"(+{', '.join(CONTROL)})")

    bloque, control = _partir_encabezado(actual)
    ajuste, detalle = _alinear(bloque, salida)
    if not detalle:
        extra = f" (+{', '.join(control)})" if control else ""
        sin_control = [c for c in CONTROL if c not in control]
        if ajuste == 0 and not sin_control:
            return f"OK          {nombre:<24} {filas} correcciones{extra}"
        # Nada de esto rompe una corrección —el motor tolera las terminadoras de más
        # o de menos y no exige las de control—, pero si el archivo está sin cargar
        # sale gratis dejarlo exacto y que nadie tenga que saberlo.
        pendiente = []
        if ajuste:
            pendiente.append(f"{'faltan' if ajuste > 0 else 'sobran'} "
                             f"{abs(ajuste)} terminadoras vacías")
        if sin_control:
            pendiente.append(f"faltan {', '.join(sin_control)}")
        falta = " y ".join(pendiente)
        if filas or verificar:
            return f"OK          {nombre:<24} {filas} correcciones{extra} — le {falta}"
        with open(destino, "w", encoding=enc, errors="replace", newline="") as f:
            f.write(sep.join(salida + _control_final(control)) + "\r\n")
        return f"NORMALIZADO {nombre:<24} encabezado exacto ({falta})"

    if filas:
        return (f"DESALINEADO {nombre:<24} {filas} correcciones cargadas y {detalle}. "
                f"NO se toca: revisarlas a mano antes de reescribir el encabezado")
    if verificar:
        return f"DESALINEADO {nombre:<24} vacío y {detalle} (se reescribe sin --verificar)"
    with open(destino, "w", encoding=enc, errors="replace", newline="") as f:
        f.write(sep.join(salida + _control_final(control)) + "\r\n")
    return f"REESCRITO   {nombre:<24} encabezado actualizado ({detalle})"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agente", help="solo este agente; por defecto, todos")
    ap.add_argument("--verificar", action="store_true", help="informar sin escribir nada")
    args = ap.parse_args()

    cfg_global = cargar_config_global()
    agentes = ([args.agente] if args.agente else
               sorted(p.stem for p in DIR_CONFIGS.glob("*.yaml") if p.stem != "global"))

    print(f"Correcciones: {cfg_global['dir_correcciones']}")
    print(f"Encabezado de referencia: {cfg_global['dir_processed']}\n")
    estados = [revisar(a, cfg_global, args.verificar) for a in agentes]
    for e in estados:
        print("  " + e)

    problemas = [e for e in estados if e.startswith(("DESALINEADO", "SIN FUENTE"))]
    print(f"\n{len(estados) - len(problemas)}/{len(estados)} en orden.")
    return 2 if problemas else 0


if __name__ == "__main__":
    sys.exit(main())
