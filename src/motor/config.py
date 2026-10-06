"""Carga y validación de las configs YAML (una por agente + global.yaml)."""

from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[2]
DIR_CONFIGS = RAIZ / "configs"


def cargar_config_agente(agente: str) -> dict:
    ruta = DIR_CONFIGS / f"{agente}.yaml"
    if not ruta.exists():
        disponibles = sorted(p.stem for p in DIR_CONFIGS.glob("*.yaml") if p.stem != "global")
        raise FileNotFoundError(
            f"No existe la config '{ruta.name}'. Agentes disponibles: {', '.join(disponibles)}"
        )
    with open(ruta, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for clave in ("agente", "salida", "entrada"):
        if clave not in cfg:
            raise ValueError(f"{ruta.name}: falta la sección obligatoria '{clave}'")
    if "nombre" not in cfg["salida"]:
        raise ValueError(f"{ruta.name}: 'salida' debe declarar 'nombre' (nombre exacto del CSV)")
    cfg.setdefault("formato", {})
    cfg["formato"].setdefault("fecha", "%d/%m/%Y")
    cfg["formato"].setdefault("numeros", {})
    cfg.setdefault("transformaciones", [])
    cfg.setdefault("validaciones", [])
    # El registro maestro es opt-in por agente: sin sección `registro` el motor
    # no acumula historia.
    cfg.setdefault("registro", {})
    cfg["salida"].setdefault("separador", ";")
    cfg["salida"].setdefault("encoding", "cp1252")
    _avisar_clave_corregible(ruta.name, cfg["registro"])
    return cfg


def _avisar_clave_corregible(nombre_config: str, cfg_registro: dict):
    """Una columna que se corrige seguido NO debería ser parte de la clave.

    El registro maestro matchea las corridas por el valor de las columnas clave.
    Si una de esas columnas la completa o la pisa una corrección, al cambiar le
    cambia la identidad a la carga: el upsert no la encuentra y la da de alta como
    NUEVA en vez de modificarla. La misma carga termina dos veces en el registro,
    una con el dato viejo y otra con el nuevo.

    Va como AVISO y no como error a propósito: sacar una columna de la clave
    implica migrar el registro del agente, y esa es una decisión de la operación.
    """
    if not cfg_registro or cfg_registro.get("activo") is False:
        return
    choque = sorted(set(cfg_registro.get("clave") or []) &
                    set(cfg_registro.get("campos_corregibles") or []))
    if choque:
        print(f"  [AVISO CONFIG] {nombre_config}: {choque} está en 'registro.clave' Y en "
              "'campos_corregibles'. Al corregirse esa columna, la carga se duplica en el "
              "registro maestro. Conviene sacarla de la clave.")


def cargar_config_global() -> dict:
    ruta = DIR_CONFIGS / "global.yaml"
    cfg = {}
    if ruta.exists():
        with open(ruta, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    datos = RAIZ / "data"
    cfg.setdefault("dir_raw", str(datos / "raw"))
    cfg.setdefault("dir_processed", str(datos / "processed"))
    cfg.setdefault("dir_logs", str(datos / "logs"))
    cfg.setdefault("dir_registro", str(datos / "registro"))
    # Correcciones de fila que la operación carga a mano, un archivo por CSV final.
    # Como el registro, NO se regenera: es memoria del pipeline.
    cfg.setdefault("dir_correcciones", str(datos / "correcciones"))
    cfg.setdefault("dir_backup", str(datos / "back_up_data_used"))
    # Un crudo consumido se mueve al backup. Es lo que evita que una descarga que
    # no llegó pase inadvertida: sin archivo en raw el agente avisa, en vez de
    # reprocesar el del día anterior y re-subir datos viejos como si fueran de hoy.
    cfg.setdefault("archivar_usados", True)
    return cfg
