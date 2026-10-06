"""Lectura de crudos: encabezados que se mueven, columnas que van y vienen, fechas mezcladas."""

import datetime as dt

import openpyxl
import pytest

from conftest import FMT
from motor.lector import canonicalizar, formatear_numero, leer_crudo

FORMATO = {"fecha": FMT, "numeros": {}}


def xlsx(tmp_path, filas, nombre="crudo.xlsx", hoja="data"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = hoja
    for f in filas:
        ws.append(list(f))
    ruta = tmp_path / nombre
    wb.save(ruta)
    return ruta


@pytest.mark.parametrize("preambulo", [0, 3, 22])
def test_encabezado_por_ancla_con_preambulo_de_largo_variable(tmp_path, preambulo):
    filas = [[f"filtro {i}", "valor"] for i in range(preambulo)]
    filas += [["HAWB", "ETA"], ["HX1", "05/09/2026"]]
    nombres, df = leer_crudo(xlsx(tmp_path, filas), {"fila_encabezado": {"ancla": "HAWB"}}, FORMATO)
    assert nombres == ["HAWB", "ETA"]
    assert df.rows() == [("HX1", "05/09/2026")]


def test_ancla_inexistente_falla_con_mensaje_claro(tmp_path):
    ruta = xlsx(tmp_path, [["otra", "cosa"], ["1", "2"]])
    with pytest.raises(ValueError, match="ningún título 'HAWB'"):
        leer_crudo(ruta, {"fila_encabezado": {"ancla": "HAWB", "buscar_hasta": 5}}, FORMATO)


@pytest.mark.parametrize("adelante", [[], [None], [None, None], ["A", "B"]])
def test_omitir_hasta_un_titulo_vengan_o_no_las_columnas_de_adelante(tmp_path, adelante):
    filas = [adelante + ["HAWB", "Shipper Name"], ["x"] * len(adelante) + ["HX1", "ACME"]]
    nombres, df = leer_crudo(xlsx(tmp_path, filas), {"columnas_omitir_iniciales": {"hasta": "HAWB"}},
                             FORMATO)
    assert nombres == ["HAWB", "Shipper Name"]
    assert df.rows() == [("HX1", "ACME")]


def test_titulos_vacios_en_el_medio_fallan_salvo_que_se_permitan(tmp_path):
    ruta = xlsx(tmp_path, [["Part", None, "Invoice"], ["P1", "x", "F1"]])
    with pytest.raises(ValueError, match="Títulos vacíos"):
        leer_crudo(ruta, {}, FORMATO)
    nombres, df = leer_crudo(ruta, {"permitir_titulos_vacios": True}, FORMATO)
    assert nombres == ["Part", "", "Invoice"]
    assert df.rows() == [("P1", "x", "F1")]


def test_columnas_fantasma_y_filas_vacias_al_final_se_recortan(tmp_path):
    ruta = xlsx(tmp_path, [["A", "B", None, None], ["1", "2", None, None], [None] * 4, [None] * 4])
    nombres, df = leer_crudo(ruta, {}, FORMATO)
    assert nombres == ["A", "B"] and df.height == 1


def test_fechas_reales_texto_en_dos_formatos_y_seriales_de_excel(tmp_path):
    formato = {"fecha": FMT, "numeros": {},
               "fecha_origen": ["%d-%b-%y %I:%M %p", "%d-%b-%y"],
               "columnas_fecha": ["Real", "Texto hora", "Texto", "Serial"]}
    filas = [["Real", "Texto hora", "Texto", "Serial", "Libre"],
             [dt.datetime(2026, 4, 27, 10, 48), "27-Apr-26 10:48 AM", "04-May-26", 46142,
              "04-May-26"]]
    _, df = leer_crudo(xlsx(tmp_path, filas), {}, formato)
    # La columna "Libre" no es de fecha: el texto se deja tal cual.
    assert df.rows() == [("27/04/2026", "27/04/2026", "04/05/2026", "30/04/2026", "04-May-26")]


def test_fecha_de_texto_que_no_matchea_queda_cruda():
    formato = {"fecha": FMT, "numeros": {}, "fecha_origen": ["%d.%m.%Y"], "columnas_fecha": ["ETA"]}
    assert canonicalizar("sin fecha", "ETA", formato) == "sin fecha"


def test_titulo_repetido_formatea_solo_la_ocurrencia_pedida(tmp_path):
    formato = {"fecha": FMT, "numeros": {}, "fecha_origen": ["%d.%m.%Y %H:%M"],
               "columnas_fecha": [{"columna": "Loaded", "ocurrencia": 1}]}
    filas = [["Loaded", "Loaded"], ["06.03.2026 00:00", "06.03.2026 00:00"]]
    _, df = leer_crudo(xlsx(tmp_path, filas), {}, formato)
    assert df.rows() == [("06/03/2026", "06.03.2026 00:00")]


def test_saltos_de_linea_dentro_de_la_celda_se_aplanan():
    assert canonicalizar("OSAKA SENSORS\nKK", "Shipper", FORMATO) == "OSAKA SENSORS KK"


@pytest.mark.parametrize("valor, decimales, miles, esperado", [
    (160.0, 2, False, "160,00"),
    (5508.3, 2, True, "5.508,30"),
    (6300.0, 0, False, "6300"),
    (110.88, None, False, "110,88"),
    (318447000913.0, None, False, "318447000913"),   # nunca en notación científica
])
def test_numeros_al_estilo_es_ar(valor, decimales, miles, esperado):
    assert formatear_numero(valor, decimales, miles) == esperado


def test_csv_de_portal_con_bom_y_comillas(tmp_path):
    ruta = tmp_path / "portal.csv"
    ruta.write_text('﻿HAWB,Descripción\nHX1,"CILINDRO 3"" NPT, ACERO"\n', encoding="utf-8")
    nombres, df = leer_crudo(ruta, {}, FORMATO)
    assert nombres == ["HAWB", "Descripción"]
    assert df.rows() == [("HX1", 'CILINDRO 3" NPT, ACERO')]
