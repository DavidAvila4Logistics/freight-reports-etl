"""Transformaciones: las reglas de las planillas y macros, ahora declaradas en YAML.

Los casos que dependen de un agente corren los pasos de su YAML real
(`pasos_del_yaml`), no una copia.
"""

import pytest

from conftest import FECHA_REF, FMT, df_de, pasos_del_yaml
from motor.transformador import aplicar_transformaciones


def correr(nombres, df, pasos, aux=None):
    aux = {"_avisos": []} if aux is None else aux
    nombres, df = aplicar_transformaciones(nombres, df, pasos, FECHA_REF, FMT, aux)
    return nombres, df, aux


def columna(nombres, df, titulo):
    return df[f"c{nombres.index(titulo)}"].to_list()


# ── air_forwarder: la fórmula de "corrige facturas" ──────────────────────────
@pytest.mark.parametrize("crudo, esperado", [
    ("261XA0101J, 261XA0102J", "1XA0101J, 1XA0102J"),   # todas las de la casilla, no solo la 1ª
    ("26123456", "7700123456"),                         # año afuera + serie adelante
    ("261XA0101J", "1XA0101J"),                         # gana la regla 1 aunque matchee '26'
    ("0705620011", "705620011"),                        # el cero de más
    ("9900001234, 705630022", "705630022"),             # referencia interna: se borra
    ("REF-88812", ""),                                  # queda vacía: el motor no inventa
    ("705600111, 0705600111", "705600111"),             # repetida después de normalizar
    ("D700123", "D700123"),                             # cualquier otra, intacta
    ("", ""),
])
def test_corregir_facturas_del_forwarder_aereo(crudo, esperado):
    nombres, df = df_de(["HAWB", "Customer References"], [["HX1", crudo]])
    nombres, df, _ = correr(nombres, df, pasos_del_yaml("air_forwarder", {"corregir_facturas"}))
    assert columna(nombres, df, "Customer References") == [esperado]


def test_homogeneizar_serie_borra_el_codigo_que_rompe_la_serie():
    nombres, df = df_de(["Customer References"], [
        ["705610001, 705610002, 705610003, 75610004"],   # 4 códigos: se limpia
        ["705610001, 75610004"],                         # 2 códigos: no hay serie
        ["705610001, 705610002, 6500000777, 6500000778"],  # empate: no se toca y se avisa
    ])
    nombres, df, aux = correr(nombres, df, pasos_del_yaml("air_forwarder", {"homogeneizar_serie"}))
    assert columna(nombres, df, "Customer References") == [
        "705610001, 705610002, 705610003", "705610001, 75610004",
        "705610001, 705610002, 6500000777, 6500000778"]
    assert [a["tipo"] for a in aux["_avisos"]] == ["serie_ambigua"]


def test_ventana_de_eta_y_cargas_sin_factura_retenidas():
    nombres, df = df_de(["HAWB", "Customer References", "ETA "], [
        ["H-dentro", "F1", "20/09/2026"],
        ["H-vieja", "F2", "01/08/2026"],
        ["H-lejos", "F3", "30/10/2026"],
        ["H-sin-eta", "F4", ""],
        ["H-sin-factura", "", "01/08/2026"],
    ])
    nombres, df, aux = correr(nombres, df, pasos_del_yaml("air_forwarder", {"filtrar_ventana_fecha"}))
    assert columna(nombres, df, "HAWB") == ["H-dentro", "H-sin-eta", "H-sin-factura"]
    assert [a["tipo"] for a in aux["_avisos"]] == ["sin_factura_retenida"]


# ── ocean_forwarder: el VLOOKUP y el BL madre/hijo ───────────────────────────
def test_cruce_con_el_reporte_del_mail_y_bl_madre_hijo():
    nombres, df = df_de(["JobNo", "TYPE", "MAWB/MBL", "HAWB/HBL", "Invoice#"], [
        ["J1", "Consolidated", "OCLU001", "", "F1"],
        ["J2", "Consolidated", "OCLU002", "", "F2"],
        ["J3", "Straight", "SEAU003", "", "F3"],
    ])
    aux = {"_avisos": [], "daily_report": df_de(["JobNo", "Invoice#", "House No"], [
        ["J1", "F1", "HB1"],
        ["J2", "F2", ""],       # la primera fila de F2 todavía no tiene el BL hijo
        ["J2", "F2", "HB2"],
    ])}
    pasos = pasos_del_yaml("ocean_forwarder", {"cruzar", "copiar_columna_si"})
    nombres, df, _ = correr(nombres, df, pasos, aux)
    assert columna(nombres, df, "HAWB/HBL") == ["HB1", "HB2", "SEAU003"]
    assert columna(nombres, df, "MAWB/MBL") == ["HB1", "HB2", "SEAU003"]


def test_cruce_sin_omitir_vacios_se_comporta_como_vlookup():
    nombres, df = df_de(["Invoice#", "HAWB/HBL"], [["F2", ""]])
    aux = {"daily_report": df_de(["Invoice#", "House No"], [["F2", ""], ["F2", "HB2"]])}
    pasos = [{"cruzar": {"auxiliar": "daily_report", "clave_local": "Invoice#",
                         "clave_externa": "Invoice#", "traer": {"HAWB/HBL": "House No"}}}]
    nombres, df, _ = correr(nombres, df, pasos, aux)
    assert columna(nombres, df, "HAWB/HBL") == [""]


def test_una_linea_por_pieza_sumando_y_sin_agrupar_filas_sin_factura():
    nombres, df = df_de(["JobNo", "Invoice#", "Product Id", "Qty shipped", "PoNumber"], [
        ["J1", "F1", "418KTM07", "1", "PO1 PO2"],
        ["J1", "F1", "418KTM07", "1", ""],
        ["J1", "F1", "418KTM07", "1", ""],
        ["J1", "F1", "PX204411", "400", ""],
        ["J4", "", "PX402836", "5", ""],
        ["J5", "", "PX402836", "7", ""],
    ])
    pasos = pasos_del_yaml("ocean_forwarder", {"deduplicar", "primer_token", "rellenar_vacias"})
    nombres, df, _ = correr(nombres, df, pasos)
    assert df.rows() == [
        ("J1", "F1", "418KTM07", "3", "PO1"),
        ("J1", "F1", "PX204411", "400", "PO1"),
        ("J4", "", "PX402836", "5", "PO1"),
        ("J5", "", "PX402836", "7", "PO1"),
    ]


@pytest.mark.parametrize("crudo, esperado", [
    ("3.18447000913e+11", "318447000913"), ("318447000913.0", "318447000913"),
    ("E31877001", "E31877001"), ("", ""),
])
def test_normalizar_entero(crudo, esperado):
    nombres, df = df_de(["Invoice#"], [[crudo]])
    nombres, df, _ = correr(nombres, df, pasos_del_yaml("ocean_forwarder", {"normalizar_entero"}))
    assert columna(nombres, df, "Invoice#") == [esperado]


# ── transit_report: la macro ──────────────────────────────────────────────────
def test_la_macro_colapsa_parte_mas_factura_sumando_la_cantidad():
    nombres, df = df_de(["Nº de parte", "Nº de factura comercial", "Cantidad", "Lista"], [
        ["R1", "INV1", "2", "PL-01"],
        ["R1", "INV1", "2", "PL-02"],
        ["R1", "INV1", "2", "PL-03"],
        ["R2", "INV1", "6300", "PL-04"],
        ["R1", "INV2", "4", "PL-05"],
    ])
    nombres, df, _ = correr(nombres, df, pasos_del_yaml("transit_report", {"deduplicar"}))
    # `criterio: primera`: queda la primera fila del grupo, como RemoveDuplicates.
    assert df.rows() == [("R1", "INV1", "6", "PL-01"), ("R2", "INV1", "6300", "PL-04"),
                         ("R1", "INV2", "4", "PL-05")]


def test_la_clave_compuesta_lleva_separador():
    # Concatenadas sin separador, las dos claves serían '412033778418KTM07'.
    nombres, df = df_de(["a", "b", "q"], [["41203377", "8418KTM07", "1"],
                                           ["412033778", "418KTM07", "1"]])
    pasos = [{"deduplicar": {"columnas": ["a", "b"], "sumar": ["q"]}}]
    nombres, df, _ = correr(nombres, df, pasos)
    assert df.height == 2


def test_deduplicar_mas_datos_se_queda_con_la_fila_completa():
    nombres, df = df_de(["Invoice Number", "Part Number", "Load ID", "Quantity", "Shipped"], [
        ["F1", "P1", "L1", "", ""],
        ["F1", "P1", "L1", "2", "11/09/2026"],
    ])
    nombres, df, _ = correr(nombres, df, pasos_del_yaml("parts_shipper", {"deduplicar"}))
    assert df.rows() == [("F1", "P1", "L1", "2", "11/09/2026")]


def test_orden_columnas_con_titulo_repetido_y_columnas_nuevas():
    nombres, df = df_de(["Nº de pedido", "Extra", "Loaded", "Loaded"],
                        [["P1", "x", "17/09/2026", "17.09.2026 00:00"]])
    pasos = [{"agregar_columna": {"nombre": "Reference", "desde": "Nº de pedido"}},
             {"agregar_columna": {"nombre": "Comment", "valor": ""}},
             {"orden_columnas": {"columnas": [
                 "Reference", {"columna": "Loaded", "ocurrencia": 2}, "Nº de pedido",
                 {"columna": "Loaded", "ocurrencia": 1}, "Comment"]}}]
    nombres, df, _ = correr(nombres, df, pasos)
    assert nombres == ["Reference", "Loaded", "Nº de pedido", "Loaded", "Comment"]
    assert df.rows() == [("P1", "17.09.2026 00:00", "P1", "17/09/2026", "")]


def test_columna_ambigua_sin_ocurrencia_falla_en_vez_de_elegir():
    nombres, df = df_de(["Loaded", "Loaded"], [["a", "b"]])
    with pytest.raises(ValueError, match="ambigua"):
        correr(nombres, df, [{"orden_columnas": {"columnas": ["Loaded"]}}])


# ── parts_shipper: filtros ────────────────────────────────────────────────────
def test_filtros_del_exportador_de_repuestos():
    nombres, df = df_de(["Packing Receiving Location", "Planned Arrival at Port of Unload",
                         "Invoice Number"], [
        ["PACK-N", "20/09/2026", "F1"],
        ["", "20/09/2026", "F2"],          # sin lugar de empaque
        ["PACK-N", "", "F3"],              # sin arribo: acá también se descarta
        ["PACK-N", "01/08/2026", "F4"],    # ya arribó
        ["PACK-N", "20/09/2026", ""],      # sin factura
    ])
    pasos = pasos_del_yaml("parts_shipper", {"filtrar_no_vacias", "filtrar_ventana_fecha"})
    nombres, df, _ = correr(nombres, df, pasos)
    assert columna(nombres, df, "Invoice Number") == ["F1"]


def test_operacion_desconocida_falla_con_la_lista_de_disponibles():
    nombres, df = df_de(["a"], [["1"]])
    with pytest.raises(ValueError, match="Transformación desconocida"):
        correr(nombres, df, [{"no_existe": {}}])
