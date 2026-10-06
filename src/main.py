"""CLI del motor: reportes crudos de forwarders -> CSV normalizados para el tablero de aduana.

Uso típico (operación diaria):
    python src/main.py --lote                      corre los agentes de `lote_diario`
    python src/main.py --lote todos                corre todos los de configs/*.yaml
    python src/main.py --agente air_forwarder      corre uno solo
Toma el archivo más reciente de data/raw/<agente>/, aplica la receta YAML,
valida y deja el CSV final en data/processed/.

Opciones:
    --entrada RUTA             procesar un archivo puntual en vez del más reciente
    --fecha-referencia DD/MM/AAAA   "hoy" para los filtros de ETA (default: hoy real)
    --forzar                   escribir el CSV aunque haya validaciones con error
    --sin-correcciones         no aplicar data/correcciones/<CSV final>.csv
    --no-archivar              dejar el crudo en data/raw/ en vez de moverlo al backup
    --sin-crudo-avisa          en un lote, un agente sin crudo es advertencia y no falla
"""

import argparse
import datetime as dt
import shutil
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from motor import correcciones as mod_correcciones
from motor.config import DIR_CONFIGS, cargar_config_agente, cargar_config_global
from motor.escritor import escribir_csv
from motor.lector import leer_crudo
from motor.registro import cargar_registro, esta_activo, upsert
from motor.transformador import aplicar_transformaciones
from motor.validador import TIPOS_CRITICOS, validaciones_automaticas, validar


def elegir_entrada(dir_raw: Path, agente: str, extensiones, excluir=()) -> Path:
    carpeta = dir_raw / agente
    candidatos = [p for p in carpeta.glob("*") if p.is_file()
                  and p.suffix.lower() in extensiones and not p.name.startswith("~$")
                  and p not in excluir]
    if not candidatos:
        raise FileNotFoundError(f"No hay archivos {sorted(extensiones)} en {carpeta} — guardar ahí el crudo del agente")
    return max(candidatos, key=lambda p: p.stat().st_mtime)


def cargar_auxiliares(dir_raw: Path, agente: str, entrada_cfg: dict, formato_cfg: dict,
                      dir_backup=None):
    """Lee los archivos de apoyo declarados en `entrada.auxiliares` (los que en
    Excel eran otra solapa de la planilla de soporte). Devuelve
    ({nombre: (nombres, df)}, [rutas usadas]) — las rutas se excluyen después de
    la elección del crudo principal, para no confundir uno con otro."""
    auxiliares, usados = {}, []
    for nombre, cfg in (entrada_cfg.get("auxiliares") or {}).items():
        carpeta = dir_raw / agente
        patron = cfg.get("patron", "*")
        candidatos = [p for p in carpeta.glob(patron) if p.is_file() and not p.name.startswith("~$")]
        if not candidatos:
            # Los auxiliares se archivan junto con el crudo principal, así que al
            # reprocesar un día viejo con --entrada hay que traerlos de vuelta a
            # mano: no se leen del backup solos, porque usar el auxiliar de ayer
            # sin darse cuenta es justo el error que el archivado viene a evitar.
            archivado = Path(dir_backup) / agente if dir_backup else None
            pista = ""
            if archivado and any(archivado.glob(patron)):
                pista = (f"\n  Hay uno archivado en {archivado} — copiarlo a "
                         f"{carpeta} si querés reprocesar ese día.")
            raise FileNotFoundError(
                f"Falta el archivo auxiliar '{nombre}' del agente {agente}: "
                f"ningún archivo de {carpeta} matchea '{patron}'{pista}"
            )
        ruta = max(candidatos, key=lambda p: p.stat().st_mtime)
        usados.append(ruta)
        auxiliares[nombre] = leer_crudo(ruta, cfg, formato_cfg)
        print(f"Auxiliar '{nombre}': {ruta.name} ({auxiliares[nombre][1].height} filas)")
    return auxiliares, usados


def aplicar_correcciones(cfg, cfg_global, nombres, df, auxiliares):
    """Aplica `data/correcciones/<CSV final>.csv` sobre la salida ya armada.

    Es la vía por la que un error detectado en el destino deja de re-subirse: el
    portal sigue mandando el valor malo todos los días y esta capa lo pisa todos
    los días. Va al final a propósito — la fila del archivo ya está en el layout
    de salida, así que no hay que reprocesar nada para aplicarla.

    Devuelve `(df, tocadas, renombradas)` para el registro maestro. La clave de
    match es `registro.clave`.
    """
    ruta = Path(cfg_global["dir_correcciones"]) / cfg["salida"]["nombre"]
    avisos = auxiliares.setdefault("_avisos", [])
    sep, enc = cfg["salida"]["separador"], cfg["salida"]["encoding"]

    # Si alguien guardó este archivo desde Excel eligiendo "Usar formato de Excel"
    # en vez de mantener CSV, queda un .xlsx invisible para el motor (busca por
    # nombre exacto). Se resuelve solo ANTES de mirar si el .csv existe: nunca puede
    # tumbar la corrida (ver el try/except adentro de la función).
    dir_backup_correcciones = Path(cfg_global["dir_backup"]) / "correcciones"
    mod_correcciones.resolver_excel_pendiente(
        ruta, dir_backup_correcciones, sep, enc, cfg["formato"]["fecha"], avisos)

    if not ruta.exists():
        return df, set(), {}
    columnas_clave = (cfg.get("registro") or {}).get("clave")
    if not columnas_clave:
        # Se avisa solo si hay algo cargado: un esqueleto vacío no es un problema.
        if mod_correcciones.cargar(ruta, enc, sep, nombres, []):
            avisos.append({
                "nivel": "advertencia", "tipo": "correccion_sin_clave",
                "mensaje": (f"{ruta.name} tiene correcciones cargadas pero el agente no "
                            f"declara 'registro.clave': no hay con qué identificar la "
                            f"carga y no se aplicó ninguna"),
            })
        return df, set(), {}
    filas = mod_correcciones.cargar(ruta, enc, sep, nombres, avisos)
    return mod_correcciones.aplicar(nombres, df, filas, columnas_clave, avisos, ruta.name)


def actualizar_registro(cfg, cfg_global, nombres, df, fecha_ref, origen,
                        corregidas=frozenset(), renombradas=None):
    """Funde la corrida en el archivo maestro del agente y devuelve el resumen.

    Corre ANTES de decidir si el CSV se escribe: una corrida rechazada igual pasó
    por el portal, y perder esa foto sería perder el único rastro de qué había ese
    día. Un fallo acá no puede voltear la corrida — el CSV de salida es lo que
    importa — así que se avisa y se sigue.
    """
    cfg_registro = cfg.get("registro", {})
    if not esta_activo(cfg_registro):
        return None
    ruta = Path(cfg_global["dir_registro"]) / f"{cfg['agente']}.csv"
    sep, enc = cfg["salida"]["separador"], cfg["salida"]["encoding"]
    try:
        nombres_prev, filas_prev = cargar_registro(ruta, enc, sep)
        cols, df_reg, resumen = upsert(
            nombres, df, cfg_registro, nombres_prev, filas_prev, fecha_ref, origen,
            corregidas=corregidas, renombradas=renombradas)
        escribir_csv(cols, df_reg, ruta, sep, enc)
        corr = f" corregidas={resumen['corregidos']}" if resumen.get("corregidos") else ""
        print(f"Registro: {ruta.name} — {df_reg.height} cargas acumuladas "
              f"(nuevas={resumen['nuevos']} modificadas={resumen['modificados']} "
              f"sin cambios={resumen['sin_cambios']} históricas={resumen['historicos']}"
              f"{corr})")
        return resumen
    except Exception as e:
        print(f"  [AVISO] no se pudo actualizar el registro maestro: {e}")
        return None


def criticos_de(hallazgos) -> list:
    """Los hallazgos que no se pueden perder entre los demás avisos de la corrida.

    Se repiten al final, después del resumen, que es donde se mira antes de subir
    los CSV.
    """
    return [f"{h['tipo']}: {h['mensaje']}" for h in hallazgos
            if h.get("tipo") in TIPOS_CRITICOS]


def imprimir_criticos(resultados):
    """El bloque REVISAR ANTES DE SUBIR. No cambia estados ni código de salida."""
    lineas = [(r["agente"], m) for r in resultados for m in (r.get("criticos") or [])]
    if not lineas:
        return
    print()
    print("REVISAR ANTES DE SUBIR:")
    for agente, mensaje in lineas:
        print(f"  {agente}: {mensaje}")


def registrar(cfg_global, agente, entrada, filas_in, filas_out, estado, hallazgos, resumen_registro=None):
    """Dos archivos de log, con propósitos distintos:

    - `corridas.log`: una línea por corrida, para saber qué se procesó y cuándo.
    - `hallazgos.log`: el detalle de cada aviso/error. Es la lista de trabajo de
      la operación — p. ej. las cargas sin factura que hay que pedirle al
      forwarder. Sin esto los avisos se los lleva la consola y a los tres días
      nadie se acuerda.
    """
    errores = [h for h in hallazgos if h["nivel"] == "error"]
    ahora = dt.datetime.now()
    logs = Path(cfg_global["dir_logs"])
    logs.mkdir(parents=True, exist_ok=True)
    reg = ""
    if resumen_registro:
        reg = (f" | registro: nuevas={resumen_registro['nuevos']} "
               f"modificadas={resumen_registro['modificados']}")
    with open(logs / "corridas.log", "a", encoding="utf-8") as f:
        f.write(f"{ahora:%d/%m/%Y %H:%M:%S} | {agente} | {entrada} | "
                f"{filas_in}->{filas_out} | {estado} | "
                f"avisos={len(hallazgos) - len(errores)} errores={len(errores)}{reg}\n")
    if hallazgos:
        with open(logs / "hallazgos.log", "a", encoding="utf-8") as f:
            for h in hallazgos:
                marca = "ERROR" if h["nivel"] == "error" else "AVISO"
                f.write(f"{ahora:%d/%m/%Y %H:%M:%S} | {agente} | {marca} | "
                        f"{h['tipo']} | {h['mensaje']}\n")


def archivar_usados(cfg_global: dict, agente: str, rutas) -> list[str]:
    """Mueve a `back_up_data_used/<agente>/` los crudos que la corrida consumió.

    Dos razones, y la segunda es la que importa:

    - `data/raw/` no acumula: lo que hay ahí es lo que falta procesar.
    - `elegir_entrada` toma el archivo MÁS RECIENTE por fecha de modificación, y
      eso falla cuando se guardan varios de una sentada (dos descargas a dos
      segundos una de la otra, y gana la vieja). Vaciando raw después de cada
      corrida no hay con qué confundirse, y si un día la descarga no llegó el
      agente avisa en vez de re-subir el crudo de ayer.

    El crudo NO se borra: se conserva tal cual llegó, solo que en otra carpeta.
    Si el destino ya tiene un archivo con ese nombre y contenido distinto, se
    guarda con sufijo en vez de pisarlo.
    """
    destino = Path(cfg_global["dir_backup"]) / agente
    destino.mkdir(parents=True, exist_ok=True)
    movidos = []
    for ruta in rutas:
        ruta = Path(ruta)
        if not ruta.exists():
            continue
        final = destino / ruta.name
        if final.exists():
            if final.stat().st_size == ruta.stat().st_size and final.read_bytes() == ruta.read_bytes():
                ruta.unlink()            # ya archivado idéntico: sobra la copia
                movidos.append(ruta.name)
                continue
            i = 2
            while (destino / f"{ruta.stem} ({i}){ruta.suffix}").exists():
                i += 1
            final = destino / f"{ruta.stem} ({i}){ruta.suffix}"
        shutil.move(str(ruta), str(final))
        movidos.append(final.name)
    if movidos:
        print(f"Archivado en {destino}: {', '.join(movidos)}")
    return movidos


def procesar_agente(agente: str, cfg_global: dict, fecha_ref: dt.date,
                    entrada_manual=None, forzar=False, archivar=True,
                    sin_correcciones=False) -> dict:
    """Corre un agente de punta a punta. Devuelve el resumen de la corrida."""
    cfg = cargar_config_agente(agente)
    print(f"Agente:  {cfg['agente']}")
    auxiliares, usados = cargar_auxiliares(
        Path(cfg_global["dir_raw"]), agente, cfg["entrada"], cfg["formato"],
        cfg_global.get("dir_backup"))
    # `_avisos` es la lista donde las transformaciones y las correcciones dejan su
    # rastro para que termine en hallazgos.log.
    auxiliares["_avisos"] = []
    extensiones = set(cfg["entrada"].get("extensiones", [".xlsx", ".xlsm"]))
    entrada = Path(entrada_manual) if entrada_manual else elegir_entrada(
        Path(cfg_global["dir_raw"]), agente, extensiones, excluir=set(usados))

    print(f"Entrada: {entrada}")
    print(f"Fecha de referencia para filtros: {fecha_ref:%d/%m/%Y}")

    nombres, df = leer_crudo(entrada, cfg["entrada"], cfg["formato"])
    filas_in = df.height
    nombres, df = aplicar_transformaciones(
        nombres, df, cfg["transformaciones"], fecha_ref, cfg["formato"]["fecha"], auxiliares)

    # Las correcciones van ANTES de validar, para que una corrección rota rebote
    # como cualquier otra fila, y el registro se escribe una sola vez, con la foto
    # del portal y las correcciones adentro.
    tocadas, renombradas = set(), {}
    if not sin_correcciones:
        df, tocadas, renombradas = aplicar_correcciones(
            cfg, cfg_global, nombres, df, auxiliares)
    resumen_registro = actualizar_registro(
        cfg, cfg_global, nombres, df, fecha_ref, entrada.name,
        corregidas=tocadas, renombradas=renombradas)

    # Los avisos de las transformaciones y las correcciones van primero: son la
    # traza de por qué una fila difiere de lo que mandó el portal.
    hallazgos = list(auxiliares.get("_avisos", []))
    hallazgos += validar(nombres, df, cfg["validaciones"], cfg["formato"]["fecha"])
    # Los chequeos del contrato de números con el destino no se declaran en ningún
    # YAML: salen de `campos_canonicos`. Van después de los declarados para que el
    # agente hable primero.
    hallazgos += validaciones_automaticas(nombres, df, cfg)
    errores = [h for h in hallazgos if h["nivel"] == "error"]
    for h in hallazgos:
        marca = "ERROR" if h["nivel"] == "error" else "AVISO"
        print(f"  [{marca}] ({h['tipo']}) {h['mensaje']}")

    salida = None
    if errores and not forzar:
        estado = "RECHAZADO"
        print(f"\nRECHAZADO: {len(errores)} validaciones con error. "
              "Corregir el crudo o correr con --forzar (bajo tu responsabilidad).")
    else:
        salida = escribir_csv(nombres, df, Path(cfg_global["dir_processed"]) / cfg["salida"]["nombre"],
                              cfg["salida"]["separador"], cfg["salida"]["encoding"])
        estado = "FORZADO" if errores else "OK"
        print(f"\n{estado}: {filas_in} filas leídas -> {df.height} filas escritas en {salida}")

    registrar(cfg_global, cfg["agente"], entrada.name, filas_in, df.height, estado, hallazgos,
              resumen_registro)

    # Solo se archiva lo que efectivamente se cargó. Una corrida RECHAZADA deja el
    # crudo en raw para poder mirarlo y volver a correr, y un --entrada manual es
    # una reprocesada puntual: mover el archivo ahí sería una sorpresa desagradable.
    if (archivar and cfg_global.get("archivar_usados", True) and entrada_manual is None
            and estado in ("OK", "FORZADO")):
        archivar_usados(cfg_global, agente, [entrada] + list(usados))

    return {"agente": agente, "estado": estado, "filas_in": filas_in, "filas_out": df.height,
            "avisos": len(hallazgos) - len(errores), "errores": len(errores),
            "salida": salida, "motivo": "", "criticos": criticos_de(hallazgos)}


def agentes_de_configs() -> list[str]:
    """Todos los agentes, tomados de `configs/*.yaml`.

    Config-driven a propósito: sumar un agente nuevo es dejar su YAML en la carpeta
    y nada más. Una lista de nombres escrita en el `.bat` o en `global.yaml` sería
    una segunda fuente de verdad que hay que acordarse de actualizar, y el día que
    alguien se olvide el agente nuevo no se corre y nadie se entera.
    """
    return sorted(p.stem for p in DIR_CONFIGS.glob("*.yaml") if p.stem != "global")


def correr_lote(agentes, cfg_global, fecha_ref, forzar=False, archivar=True,
                sin_correcciones=False, sin_crudo_avisa=False) -> int:
    """Corre varios agentes sin abortar en el primero que falle.

    Los reportes son independientes entre sí: que a uno le falte el crudo o le
    rebote una validación no es motivo para dejar los otros sin generar. Al final
    se imprime el resumen, y el código de salida resume el peor caso para que un
    programador de tareas lo pueda leer.

    `sin_crudo_avisa` cambia SOLO cómo se cuenta el insumo que no llegó (estado
    `SIN CRUDO`, aviso en vez de error, no afecta el código de salida). Es el mismo
    evento con dos significados según quién corra: para un reporte que se baja de
    un portal todos los días, una carpeta vacía quiere decir "falta bajarlo" y tiene
    que gritar; para uno que llega por mail cuando hay novedades, es lo normal.
    """
    resultados = []
    for i, agente in enumerate(agentes, 1):
        separador = "=" * 70
        print(f"\n{separador}\n[{i}/{len(agentes)}] {agente}\n{separador}")
        try:
            resultados.append(procesar_agente(agente, cfg_global, fecha_ref, forzar=forzar,
                                              archivar=archivar,
                                              sin_correcciones=sin_correcciones))
        except Exception as e:
            # `FileNotFoundError` es el insumo que no llegó: la carpeta
            # `data/raw/<agente>` vacía o el auxiliar que no matchea. Cualquier
            # otra excepción es un problema de verdad y sigue siendo FALLA aunque
            # el flag esté puesto.
            if sin_crudo_avisa and isinstance(e, FileNotFoundError):
                print(f"\nSIN CRUDO: {e}")
                resultados.append({"agente": agente, "estado": "SIN CRUDO", "filas_in": 0,
                                   "filas_out": 0, "avisos": 1, "errores": 0,
                                   "salida": None, "motivo": str(e)})
                registrar(cfg_global, agente, "-", 0, 0, "SIN CRUDO",
                          [{"nivel": "advertencia", "tipo": "sin_crudo",
                            "mensaje": str(e)}])
                continue
            print(f"\nFALLA: {type(e).__name__}: {e}")
            traceback.print_exc(limit=1)
            resultados.append({"agente": agente, "estado": "FALLA", "filas_in": 0, "filas_out": 0,
                               "avisos": 0, "errores": 1, "salida": None, "motivo": str(e)})
            registrar(cfg_global, agente, "-", 0, 0, "FALLA",
                      [{"nivel": "error", "tipo": "excepcion", "mensaje": str(e)}])

    separador = "=" * 70
    print(f"\n{separador}\nRESUMEN DE LA CORRIDA  ({fecha_ref:%d/%m/%Y})\n{separador}")
    for r in resultados:
        extra = f"avisos={r['avisos']}" if r["estado"] in ("OK", "FORZADO") else r["motivo"]
        print(f"  {r['estado']:<10} {r['agente']:<20} "
              f"{r['filas_in']:>5} -> {r['filas_out']:<5}  {extra[:60]}")

    sin_crudo = [r for r in resultados if r["estado"] == "SIN CRUDO"]
    con_problemas = [r for r in resultados if r["estado"] not in ("OK", "SIN CRUDO")]
    con_avisos = [r for r in resultados if r["estado"] == "OK" and r["avisos"]]
    ok = len(resultados) - len(con_problemas) - len(sin_crudo)
    cuenta = f"\n{ok}/{len(resultados)} agentes OK"
    if sin_crudo:
        cuenta += f" - {len(sin_crudo)} sin crudo (no llegó el archivo)"
    print(f"{cuenta} - {len(con_problemas)} con problemas.")
    if sin_crudo:
        print(f"Sin crudo en data/raw/: {', '.join(r['agente'] for r in sin_crudo)}")
    if con_avisos:
        print(f"Con avisos (ver data/logs/hallazgos.log): {', '.join(r['agente'] for r in con_avisos)}")
    imprimir_criticos(resultados)

    if con_problemas:
        nombres = [f"{r['agente']} ({r['estado']})" for r in con_problemas]
        print(f"REVISAR: {', '.join(nombres)}")
        return 2
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reportes de forwarders -> CSV para el tablero de aduana")
    ap.add_argument("--agente")
    ap.add_argument("--lote", nargs="*", metavar="AGENTE",
                    help="corrida en lote; sin argumentos usa 'lote_diario' de global.yaml. "
                         "'--lote todos' corre todos los de configs/*.yaml")
    ap.add_argument("--entrada")
    ap.add_argument("--fecha-referencia", help="DD/MM/AAAA; default hoy")
    ap.add_argument("--forzar", action="store_true")
    # Las correcciones están ENCENDIDAS por defecto: apagarlas le devuelve al
    # destino el valor malo del portal, que es justo lo que la operación arregló.
    ap.add_argument("--sin-correcciones", action="store_true",
                    help="no aplicar data/correcciones/<CSV final>.csv")
    # El archivado también está ENCENDIDO por defecto: raw guarda lo pendiente,
    # no el historial. Se apaga para probar sin tocar los crudos.
    ap.add_argument("--no-archivar", action="store_true",
                    help="no mover el crudo consumido a back_up_data_used/<agente>/")
    ap.add_argument("--sin-crudo-avisa", action="store_true",
                    help="un agente sin crudo en data/raw/ queda en SIN CRUDO (advertencia) "
                         "en vez de FALLA, y no afecta el código de salida")
    args = ap.parse_args(argv)

    if (args.agente is None) == (args.lote is None):
        ap.error("indicar --agente <nombre> o --lote (no ambos)")
    if args.lote is not None and args.entrada:
        ap.error("--entrada solo aplica a la corrida de un agente")

    cfg_global = cargar_config_global()
    fecha_ref = (dt.datetime.strptime(args.fecha_referencia, "%d/%m/%Y").date()
                 if args.fecha_referencia else dt.date.today())

    if args.lote is not None:
        pedidos = args.lote
        # `todos` no es un agente: es la palabra que resuelve la lista completa desde
        # configs/. Si algún día hiciera falta uno con ese nombre, `--lote` sigue
        # aceptando los nombres uno por uno.
        if len(pedidos) == 1 and pedidos[0].lower() == "todos":
            pedidos = agentes_de_configs()
        agentes = pedidos or cfg_global.get("lote_diario") or []
        if not agentes:
            ap.error("no hay agentes en el lote: pasarlos por línea de comandos "
                     "o declarar 'lote_diario' en configs/global.yaml")
        return correr_lote(agentes, cfg_global, fecha_ref, args.forzar,
                           archivar=not args.no_archivar,
                           sin_correcciones=args.sin_correcciones,
                           sin_crudo_avisa=args.sin_crudo_avisa)

    r = procesar_agente(args.agente, cfg_global, fecha_ref, args.entrada, args.forzar,
                        archivar=not args.no_archivar,
                        sin_correcciones=args.sin_correcciones)
    imprimir_criticos([r])
    return 2 if r["estado"] == "RECHAZADO" else 0


if __name__ == "__main__":
    sys.exit(main())
