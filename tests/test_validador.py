"""Validaciones declaradas y chequeos automáticos del contrato de números con el destino."""

import pytest

from conftest import FMT, df_de
from motor.validador import colapso_destino, validaciones_automaticas, validar


def tipos(hallazgos):
    return [h["tipo"] for h in hallazgos]


def test_columnas_requeridas_avisa_un_cambio_de_layout():
    nombres, df = df_de(["HAWB", "ETA "], [["H1", "01/09/2026"]])
    h = validar(nombres, df, [{"columnas_requeridas": {"columnas": ["HAWB", "ETA", "MAWB"]}}], FMT)
    assert len(h) == 1 and "MAWB" in h[0]["mensaje"] and h[0]["nivel"] == "error"


def test_fechas_validas_lista_ejemplos():
    nombres, df = df_de(["ETA"], [["01/09/2026"], ["2026-09-01"], [""]])
    h = validar(nombres, df, [{"fechas_validas": {"columnas": ["ETA"]}}], FMT)
    assert len(h) == 1 and "2026-09-01" in h[0]["mensaje"]


def test_no_vacias_identifica_las_cargas_a_pedir():
    nombres, df = df_de(["JobNo", "Invoice#"], [["J1", "F1"], ["J2", ""], ["J3", " "]])
    h = validar(nombres, df, [{"no_vacias": {"columna": "Invoice#", "nivel": "advertencia",
                                             "identificar_por": "JobNo"}}], FMT)
    assert h[0]["nivel"] == "advertencia" and "J2, J3" in h[0]["mensaje"]


def test_sin_duplicados_y_sin_caracter():
    nombres, df = df_de(["Customer References", "Cantidad"], [["F1", "2"], ["F1", "6.300"]])
    h = validar(nombres, df, [{"sin_duplicados": {"columna": "Customer References"}},
                              {"sin_caracter": {"columna": "Cantidad", "caracter": "."}}], FMT)
    assert tipos(h) == ["sin_duplicados", "sin_caracter"]


def test_duplicados_consistentes():
    nombres, df = df_de(["FC", "ETA"], [["F1", "01/09/2026"], ["F1", "02/09/2026"], ["F2", "x"]])
    h = validar(nombres, df, [{"duplicados_consistentes": {"clave": "FC",
                                                           "columnas_iguales": ["ETA"]}}], FMT)
    assert len(h) == 1 and "F1" in h[0]["mensaje"]


def test_validacion_desconocida_falla():
    nombres, df = df_de(["a"], [["1"]])
    with pytest.raises(ValueError, match="Validación desconocida"):
        validar(nombres, df, [{"no_existe": {}}], FMT)


# ── Chequeos automáticos ────────────────────────────────────────────────────
@pytest.mark.parametrize("valor, esperado", [
    ("318447005512", "3.18447E+11"),
    ("20265476530", "20265476530"),     # 11 dígitos: todavía se ve entero
    ("e31877001", "E31877001"),
    ("²", "²"),                          # isdigit() es True, float() revienta
])
def test_colapso_destino(valor, esperado):
    assert colapso_destino(valor) == esperado


CFG = {"campos_canonicos": {"factura": "FC", "bl": "BL", "contenedor": "Container"}}


def test_dos_facturas_que_el_destino_lee_iguales():
    nombres, df = df_de(["FC", "BL", "Container"], [["318447005512", "", ""],
                                                    ["318447004801", "", ""]])
    h = validaciones_automaticas(nombres, df, CFG)
    assert "clave_destino_colapsada" in tipos(h)


def test_misma_factura_en_varias_piezas_no_es_un_problema():
    nombres, df = df_de(["FC", "BL", "Container"], [["F1", "B1", "C1"], ["F1", "B1", "C1"]])
    assert validaciones_automaticas(nombres, df, CFG) == []


def test_facturas_largas_con_bl_propio_solo_avisan():
    nombres, df = df_de(["FC", "BL", "Container"], [["318447005512", "B1", ""],
                                                    ["318447004801", "B2", ""]])
    assert tipos(validaciones_automaticas(nombres, df, CFG)) == ["numero_largo_identidad"]


@pytest.mark.parametrize("valor, rebota", [
    ("3,18447E+11", True), ("1e+16", True), ("1.5e+16", True),
    ("05E37", False),        # número de viaje de una naviera, no un número
    ("71E15", False), ("E31877001", False),
])
def test_notacion_cientifica_bloquea_la_salida(valor, rebota):
    nombres, df = df_de(["Voyage", ""], [[valor, ""]])
    h = validaciones_automaticas(nombres, df, {})
    assert (tipos(h) == ["notacion_cientifica"]) is rebota
    if rebota:
        assert h[0]["nivel"] == "error"
