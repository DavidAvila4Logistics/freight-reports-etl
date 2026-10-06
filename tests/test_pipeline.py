"""De punta a punta: datos demo -> lote de los cuatro agentes -> CSV, registro y logs."""

import datetime as dt
from pathlib import Path

import pytest

import generar_datos_demo
import main as motor_main

REF = dt.date(2026, 9, 15)


def leer_csv(ruta):
    lineas = Path(ruta).read_bytes().decode("cp1252").split("\r\n")
    filas = [l.split(";") for l in lineas if l]
    return filas[0], [dict(zip(filas[0], f)) for f in filas[1:]]


@pytest.fixture
def corrida(cfg_global, monkeypatch, capsys):
    """Genera los datos demo y corre el lote completo. Devuelve (código, cfg_global)."""
    monkeypatch.setattr(motor_main, "cargar_config_global", lambda: cfg_global)
    generar_datos_demo.generar(Path(cfg_global["dir_raw"]), Path(cfg_global["dir_correcciones"]), REF)
    codigo = motor_main.main(["--lote", "--fecha-referencia", f"{REF:%d/%m/%Y}"])
    return codigo, cfg_global, capsys.readouterr().out


def salida(cfg_global, nombre):
    return leer_csv(Path(cfg_global["dir_processed"]) / nombre)


def test_el_lote_demo_termina_ok(corrida):
    codigo, _, out = corrida
    assert codigo == 0
    assert "4/4 agentes OK - 0 con problemas." in out


def test_air_forwarder(corrida):
    _, cfg, _ = corrida
    encabezado, filas = salida(cfg, "01-AIR.csv")
    assert encabezado[0] == "HAWB"                       # las dos columnas vacías se fueron
    por_hawb = {f["HAWB"]: f for f in filas}
    # Fuera de la ventana -15/+30: la vieja y la lejana.
    assert set(por_hawb) == {f"HX10000{i:02d}" for i in (1, 2, 3, 4, 5, 6, 7, 10, 11, 12)}
    refs = {h: f["Customer References"] for h, f in por_hawb.items()}
    assert refs["HX1000001"] == "1XA0101J, 1XA0102J"
    assert refs["HX1000002"] == "7700123456"
    assert refs["HX1000004"] == "705630022"
    assert refs["HX1000005"] == "705123999"             # lo completó la corrección
    assert refs["HX1000006"] == "705610001, 705610002, 705610003"
    assert refs["HX1000010"] == ""                      # sin factura: retenida
    assert por_hawb["HX1000011"]["ETA "] == "29/09/2026"  # ETA de la corrección
    assert por_hawb["HX1000011"]["Shipper Name"] == "QUEBEC ELECTRIC, LTD"
    assert por_hawb["HX1000012"]["Shipper Name"] == "OSAKA SENSORS KK"
    assert por_hawb["HX1000001"]["Estimated Departure"] == "14/09/2026"
    assert por_hawb["HX1000001"]["Gross Weight kgs"] == "160,00"


def test_ocean_forwarder(corrida):
    _, cfg, _ = corrida
    _, filas = salida(cfg, "02-OCEAN.csv")
    piezas = {(f["Invoice#"], f["Product Id"]): f for f in filas}
    assert set(piezas) == {("F31800020650", "PX204411"), ("F31800020650", "PX204412"),
                           ("F31800020650", "PX204413"), ("318447000913", "418KTM07"),
                           ("E31877001", "PX301234"), ("", "PX402836")}
    cons = piezas[("318447000913", "418KTM07")]
    assert (cons["HAWB/HBL"], cons["MAWB/MBL"]) == ("OFHB100002", "OFHB100002")
    assert cons["Qty shipped"] == "3"
    straight = piezas[("E31877001", "PX301234")]
    assert straight["HAWB/HBL"] == straight["MAWB/MBL"] == "SEAU7770003"
    assert {f["PoNumber"] for f in filas if f["JobNo"] == "OFS100001"} == {"4500012345"}
    assert straight["ATD"].count("/") == 2               # el ISO con hora quedó en DD/MM/AAAA


def test_transit_report(corrida):
    _, cfg, _ = corrida
    encabezado, filas = salida(cfg, "03-TRANSIT.csv")
    assert len(encabezado) == 44 and encabezado.count("Loaded for pre-carriage") == 2
    assert encabezado[-2:] == ["Reference", "Comment"]
    assert "Incoterms del embarque" not in encabezado
    cantidades = {(f["Nº de parte"], f["Nº de factura comercial"]): f["Cantidad"] for f in filas}
    assert cantidades == {("R123456", "INV-2601001"): "6", ("R654321", "INV-2601001"): "6300",
                          ("R123456", "INV-2601002"): "4", ("R999999", "INV-2601004"): "3",
                          ("R101010", "INV-2601005"): "8"}
    primera = filas[0]
    i1, i2 = [i for i, t in enumerate(encabezado) if t == "Loaded for pre-carriage"]
    linea = Path(cfg["dir_processed"], "03-TRANSIT.csv").read_bytes().decode("cp1252")
    valores = linea.split("\r\n")[1].split(";")
    assert valores[i1].count("/") == 2 and valores[i2].endswith(" 00:00")
    assert primera["Reference"] == primera["Nº de pedido"]


def test_parts_shipper(corrida):
    _, cfg, _ = corrida
    encabezado, filas = salida(cfg, "04-PARTS.csv")
    assert encabezado.count("") == 2
    claves = [(f["Invoice Number"], f["Part Number"], f["Load ID"]) for f in filas]
    assert claves == [("PS-880001", "AA10001", "L-5001"), ("PS-880001", "AA10002", "L-5001"),
                      ("PS-880001", "AA10003", "L-5001"), ("PS-880001", "AA10001", "L-5002"),
                      ("PS-880002", "AA10004", "L-5003"), ("PS-880004", "AA10009", "L-5006")]
    assert filas[0]["Quantity"] == "2"                   # ganó la fila completa
    assert all(f["At Packing Receiving"].count("/") == 2 for f in filas)   # seriales convertidos


def test_registro_logs_y_archivado(corrida):
    _, cfg, _ = corrida
    registro = Path(cfg["dir_registro"])
    assert sorted(p.name for p in registro.glob("*.csv")) == [
        "air_forwarder.csv", "ocean_forwarder.csv", "parts_shipper.csv", "transit_report.csv"]
    _, air = leer_csv(registro / "air_forwarder.csv")
    corregidas = {f["HAWB"] for f in air if f["_estado"] == "corregido"}
    assert corregidas == {"HX1000005", "HX1000011"}
    logs = Path(cfg["dir_logs"])
    assert len((logs / "corridas.log").read_text(encoding="utf-8").splitlines()) == 4
    assert "HX1000010" in (logs / "hallazgos.log").read_text(encoding="utf-8")
    # Los crudos consumidos se mueven al backup: raw queda con lo que falta procesar.
    assert not any(Path(cfg["dir_raw"]).rglob("*.xlsx"))
    assert len(list(Path(cfg["dir_backup"]).rglob("*.xlsx"))) == 5


def test_segunda_corrida_sin_crudos(corrida, capsys):
    codigo, cfg, _ = corrida
    # Sin crudo nuevo, la corrida diaria tiene que gritar: no se bajó el reporte.
    assert motor_main.main(["--lote", "--fecha-referencia", f"{REF:%d/%m/%Y}"]) == 2
    assert "REVISAR" in capsys.readouterr().out
    # Para los reportes que llegan por mail cuando hay novedades, es solo un aviso.
    assert motor_main.main(["--lote", "--sin-crudo-avisa"]) == 0
    assert "4 sin crudo" in capsys.readouterr().out


def test_mismo_crudo_dos_veces_no_duplica_el_registro(cfg_global, monkeypatch, capsys):
    monkeypatch.setattr(motor_main, "cargar_config_global", lambda: cfg_global)
    for _ in range(2):
        generar_datos_demo.generar(Path(cfg_global["dir_raw"]),
                                   Path(cfg_global["dir_correcciones"]), REF)
        assert motor_main.main(["--agente", "parts_shipper",
                                "--fecha-referencia", f"{REF:%d/%m/%Y}"]) == 0
    out = capsys.readouterr().out
    assert "nuevas=0 modificadas=0 sin cambios=6" in out
    _, registro = leer_csv(Path(cfg_global["dir_registro"]) / "parts_shipper.csv")
    assert len(registro) == 6


def test_validacion_con_error_rechaza_y_no_archiva(cfg_global, monkeypatch, capsys):
    monkeypatch.setattr(motor_main, "cargar_config_global", lambda: cfg_global)
    generar_datos_demo.generar(Path(cfg_global["dir_raw"]), Path(cfg_global["dir_correcciones"]), REF)
    # Un reporte del mail sin la factura de OFS100002: el cruce no encuentra el BL
    # hijo y una fila Consolidated se subiría sin documento de transporte.
    dir_agente = Path(cfg_global["dir_raw"], "ocean_forwarder")
    _, aux = generar_datos_demo.ocean_forwarder(REF)
    aux["daily_report"] = [f for f in aux["daily_report"] if "OFS100002" not in f]
    generar_datos_demo._guardar(dir_agente / "daily_report.xlsx", aux)
    fecha = ["--fecha-referencia", f"{REF:%d/%m/%Y}"]

    assert motor_main.main(["--agente", "ocean_forwarder"] + fecha) == 2
    out = capsys.readouterr().out
    assert "RECHAZADO" in out and "'HAWB/HBL': 1 celdas vacías" in out
    assert not Path(cfg_global["dir_processed"], "02-OCEAN.csv").exists()
    assert len(list(dir_agente.glob("*.xlsx"))) == 2      # el crudo queda para volver a correr

    # --forzar escribe igual, bajo responsabilidad de quien corre, y sí archiva.
    assert motor_main.main(["--agente", "ocean_forwarder", "--forzar"] + fecha) == 0
    assert "FORZADO" in capsys.readouterr().out
    assert Path(cfg_global["dir_processed"], "02-OCEAN.csv").exists()
    assert not list(dir_agente.glob("*.xlsx"))


def test_agente_desconocido_lista_los_disponibles(cfg_global, monkeypatch):
    monkeypatch.setattr(motor_main, "cargar_config_global", lambda: cfg_global)
    with pytest.raises(FileNotFoundError, match="air_forwarder, ocean_forwarder"):
        motor_main.main(["--agente", "no_existe"])
