"""Fixtures comunes. Todo es sintético y corre sobre carpetas temporales."""

import datetime as dt
import sys
from pathlib import Path

import polars as pl
import pytest
import yaml

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "tools"))

DIR_CONFIGS = RAIZ / "configs"
FECHA_REF = dt.date(2026, 9, 15)
FMT = "%d/%m/%Y"


def df_de(columnas, filas):
    """(nombres, df) con el layout interno del motor: columnas c0..cN, todo Utf8."""
    datos = {f"c{i}": [f[i] for f in filas] for i in range(len(columnas))}
    return list(columnas), pl.DataFrame(datos, schema={f"c{i}": pl.Utf8 for i in range(len(columnas))})


def pasos_del_yaml(agente, ops):
    """Los pasos del YAML REAL del agente, en su orden, quedándose solo con `ops`.

    Se prueban los pasos de producción y no una copia escrita en el test: una copia
    seguiría pasando aunque alguien cambie el orden o un parámetro en el YAML.
    """
    cfg = yaml.safe_load((DIR_CONFIGS / f"{agente}.yaml").read_text(encoding="utf-8"))
    return [t for t in cfg["transformaciones"] if next(iter(t)) in ops]


@pytest.fixture
def cfg_global(tmp_path):
    """Un `global.yaml` apuntando todo a una carpeta temporal."""
    datos = tmp_path / "data"
    cfg = {f"dir_{n}": str(datos / n) for n in
           ("raw", "processed", "logs", "registro", "correcciones")}
    cfg["dir_backup"] = str(datos / "back_up_data_used")
    cfg["archivar_usados"] = True
    cfg["lote_diario"] = ["air_forwarder", "ocean_forwarder", "transit_report", "parts_shipper"]
    return cfg
