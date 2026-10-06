"""Genera crudos ficticios para los cuatro agentes en data/raw/<agente>/.

Todo es inventado: forwarders, guías, facturas, piezas y buques. Cada crudo trae a
propósito lo desordenado que su YAML resuelve, así una sola corrida del lote pasa
por todas las reglas:

- air_forwarder: preámbulo de largo variable, dos columnas vacías adelante, fechas
  reales y de texto en dos formatos, facturas con prefijos a corregir, referencias
  que no son facturas, una serie rota, un ';' y un salto de línea dentro de celdas.
- ocean_forwarder: el BL hijo vacío en el portal y presente en un segundo archivo
  (con la factura repetida y la primera fila sin dato), filas copia, POs múltiples,
  piezas repetidas a sumar y una factura numérica de 12 dígitos.
- transit_report: todo texto, columnas de más y en otro orden, un título repetido,
  líneas a colapsar sumando cantidad y cantidades "2.0".
- parts_shipper: seriales de Excel en columnas de fecha, dos columnas sin título,
  piezas sin lugar de empaque, sin factura o fuera de ventana, y una pieza duplicada.

Además deja `data/correcciones/01-AIR.csv` con dos correcciones de la operación.

Las fechas son relativas a la fecha de referencia (hoy por defecto), así la
ventana de ETA -15/+30 descarta siempre las mismas filas.

Uso:
    python tools/generar_datos_demo.py
    python tools/generar_datos_demo.py --fecha-referencia 15/09/2026
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

import openpyxl

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from motor.config import cargar_config_global  # noqa: E402

# Abreviaturas en inglés fijas: `strftime("%b")` depende del locale de la PC.
_MESES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _mes(d: dt.date) -> str:
    return _MESES[d.month - 1]


def _guardar(ruta: Path, hojas: dict):
    """{nombre de hoja: [filas]} -> .xlsx. La primera hoja es la activa."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for nombre, filas in hojas.items():
        ws = wb.create_sheet(nombre)
        for f in filas:
            ws.append(list(f))
    wb.save(ruta)
    return ruta


def _serial_excel(d: dt.date) -> int:
    """La fecha como la guarda Excel en una celda sin formato."""
    return (d - dt.date(1899, 12, 30)).days


# ── 01-AIR ───────────────────────────────────────────────────────────────────
AIR_COLUMNAS = ["HAWB", "Shipper Name", "MAWB", "Consignee Name", "Customer References",
                "Pickup at Shippers", "Estimated Departure", "ETA ", "Arrived at Destination",
                "Consol ATD", "Gross Weight kgs", "Chargeable weight kgs", "Volume m3", "Pieces"]


def air_forwarder(ref: dt.date):
    def d(n):
        return ref + dt.timedelta(days=n)

    def texto_hora(x):      # "27-Apr-26 10:48 AM"
        return f"{x.day:02d}-{_mes(x)}-{x:%y} 10:48 AM"

    def texto(x):           # "04-May-26"
        return f"{x.day:02d}-{_mes(x)}-{x:%y}"

    # (HAWB, shipper, MAWB, referencias, días al ETA o None, kg)
    cargas = [
        ("HX1000001", "NORDIC PARTS AB", "176-11112222", "261XA0101J, 261XA0102J", 5, 160.0),
        ("HX1000002", "BAVARIA COMPONENTS GMBH", "176-11112222", "26123456", 3, 48.5),
        ("HX1000003", "LYON HYDRAULICS SAS", "020-33334444", "0705620011", -2, 312.25),
        ("HX1000004", "MIDWEST CASTINGS INC", "020-33334444", "9900001234, 705630022", 8, 1200.0),
        ("HX1000005", "OHIO GEARS LLC", "157-55556666", "REF-88812", 10, 22.0),
        ("HX1000006", "TORINO FILTRI SPA", "157-55556666",
         "705610001, 705610002, 705610003, 75610004", 12, 87.3),
        ("HX1000007", "KANSAS SEALS CO", "176-77778888", "705600111, 0705600111", 1, 15.75),
        ("HX1000008", "IOWA BEARINGS INC", "176-77778888", "251XB0099K", -40, 410.0),
        ("HX1000009", "SAO PAULO PECAS LTDA", "020-99990000", "705777001", 50, 95.0),
        ("HX1000010", "MONTERREY TOOLS SA", "020-99990000", "", -30, 64.4),
        ("HX1000011", "QUEBEC ELECTRIC; LTD", "157-12121212", "705888002", None, 33.0),
        ("HX1000012", "OSAKA SENSORS\nKK", "157-12121212", "D700123", 20, 5.125),
    ]
    filas = [
        ["Shipment Profile Report"],
        ["Company:", "ACME MACHINERY"],
        ["Transport Mode:", "AIR"],
        ["Consignee:", "ACME MACHINERY ARGENTINA SA"],
        ["Date range:", "Last 60 days"],
        [],
        [None, None] + AIR_COLUMNAS,
    ]
    for hawb, shipper, mawb, refs, eta, kg in cargas:
        salida = d((eta if eta is not None else 20) - 6)
        filas.append([
            None, None, hawb, shipper, mawb, "ACME MACHINERY ARGENTINA SA", refs,
            dt.datetime.combine(salida - dt.timedelta(days=2), dt.time(9, 30)),   # fecha real
            texto_hora(salida),
            texto(d(eta)) if eta is not None else "",
            dt.datetime.combine(d(eta), dt.time(14, 0)) if eta is not None and eta < 0 else None,
            texto_hora(salida),
            kg, round(kg * 1.2, 3), round(kg / 167, 4), 1 + int(kg // 100),
        ])
    return {"Filters": [["Filter", "Value"], ["Mode", "AIR"]], "Shipments": filas}


# ── 02-OCEAN ─────────────────────────────────────────────────────────────────
OCEAN_COLUMNAS = ["JobNo", "ModeOfTransport", "TYPE", "MAWB/MBL", "HAWB/HBL", "Invoice#",
                  "InvoiceDate", "PoNumber", "Product Id", "Qty shipped", "Container",
                  "Pickup date", "ETD", "ETA", "ATD", "ATA", "Weight", "Volume"]


def ocean_forwarder(ref: dt.date):
    def d(n):
        return ref + dt.timedelta(days=n)

    def texto(x):           # "27 Jul 2026"
        return f"{x.day:02d} {_mes(x)} {x.year}"

    # (job, tipo, MBL, factura, PO, producto, qty, contenedor, días al ETA)
    lineas = [
        ("OFS100001", "Consolidated", "OCLU5550001", "F31800020650", "4500012345 4500012346",
         "PX204411", 400, "OCLU1234567", 6),
        ("OFS100001", "Consolidated", "OCLU5550001", "F31800020650", "", "PX204412", 200,
         "OCLU1234567", 6),
        ("OFS100001", "Consolidated", "OCLU5550001", "F31800020650", "4500012345",
         "PX204413", 300, "OCLU1234567", 6),
        # fila copia, sin Product Id: se descarta
        ("OFS100001", "Consolidated", "OCLU5550001", "F31800020650", "4500012345", "", 0,
         "OCLU1234567", 6),
        # tres equipos con serial distinto: la misma pieza, qty 1 cada uno
        ("OFS100002", "Consolidated", "OCLU5550002", 318447000913, "4500099001", "418KTM07", 1,
         "OCLU7654321", 9),
        ("OFS100002", "Consolidated", "OCLU5550002", 318447000913, "4500099001", "418KTM07", 1,
         "OCLU7654321", 9),
        ("OFS100002", "Consolidated", "OCLU5550002", 318447000913, "4500099001", "418KTM07", 1,
         "OCLU7654321", 9),
        ("OFS100003", "Straight", "SEAU7770003", "E31877001", "4500077003", "PX301234", 12,
         "SEAU3456789", 2),
        # sin factura todavía: se conserva aunque el ETA quede fuera de la ventana
        ("OFS100004", "Straight", "SEAU7770004", "", "4500077004", "PX402836", 5,
         "SEAU9876543", -25),
        ("OFS100005", "Consolidated", "OCLU5550005", "F31800020777", "4500012399", "PX502345", 8,
         "OCLU1112223", 40),
        ("OFS100006", "Straight", "SEAU7770006", "E31877002", "4500077006", "PX209001", 2,
         "SEAU4445556", -30),
    ]
    cuerpo = [OCEAN_COLUMNAS]
    for job, tipo, mbl, fc, po, prod, qty, cont, eta in lineas:
        cuerpo.append([
            job, "Ocean", tipo, mbl, None, fc, texto(d(eta - 40)), po or None, prod or None,
            qty, cont, texto(d(eta - 35)), texto(d(eta - 30)), texto(d(eta)),
            f"{d(eta - 30):%Y-%m-%d}T00:00:00",    # ATD llega en ISO con hora
            None, round(qty * 12.5 + 110.88, 2), round(qty * 0.0123 + 0.819, 3),
        ])

    # El reporte del mail: título en la fila 1, columna sin título en el medio, y la
    # factura de OFS100002 dos veces — la primera todavía sin BL hijo.
    diario = [
        ["Daily Report - Desktop"],
        ["JobNo", "Invoice#", "House No", "Status", None, "Remarks"],
        ["OFS100001", "F31800020650", "OFHB100001", "In transit", "x", ""],
        ["OFS100002", 318447000913, None, "Booked", "", "HBL pending"],
        ["OFS100002", 318447000913, "OFHB100002", "In transit", "x", ""],
        ["OFS100005", "F31800020777", "OFHB100005", "Booked", "", ""],
    ]
    return {"data": cuerpo}, {"daily_report": diario}


# ── 03-TRANSIT ───────────────────────────────────────────────────────────────
TRANSIT_COLUMNAS = [
    "Nº de pedido", "Dirección de proveedor del embarque", "Estado actual del embarque",
    "Fecha de estado actual del embarque", "Nº de orden de Compra", "Nº de parte", "Cantidad",
    "Nº de factura comercial", "Nº de lista de empaque", "Pedir unidad",
    "Incoterms del embarque", "Fecha de embarque", "Fecha de vencimiento",
    "Fecha de notificación", "Fecha de disponibilidad del embarque", "Recolección del artículo",
    "Tipo de orden", "Destino del contenedor", "Tipo de referencia", "Nivel de servicio",
    "Referencia", "Descripción del tipo de contenedor", "Nº de contenedor", "Transportista",
    "Nombre del buque", "Nº de vuelo/viaje", "BL/AWB", "House BL/AWB", "Nº de PFR",
    "Loaded for pre-carriage", "In-gate POL", "Horario de recolección", "Puerto de orígen",
    "Hora de llegada del buque", "Puerto de destino", "Vessel/Flight arrival",
    "Discharged from vessel", "Loaded for pre-carriage", "Arrived at rail yard",
    "Enroute to delivery by truck", "Nº de contrato",
    "Fecha estimada de llegada al lugar de entrega", "Hora de llegada", "Lugar de entrega",
    "Destino final", "Delivery dispatched",
]


def transit_report(ref: dt.date):
    def d(n):
        return ref + dt.timedelta(days=n)

    def con_hora(x):
        return f"{x:%d.%m.%Y} 00:00"

    def sin_hora(x):
        return f"{x:%d.%m.%Y}"

    # (pedido, parte, cantidad, factura, lista de empaque, días al buque o None)
    lineas = [
        ("P-7001", "R123456", 2.0, "INV-2601001", "PL-01", 7),
        ("P-7001", "R123456", 2.0, "INV-2601001", "PL-02", 7),
        ("P-7001", "R123456", 2.0, "INV-2601001", "PL-03", 7),
        ("P-7001", "R654321", 6300.0, "INV-2601001", "PL-04", 7),
        ("P-7002", "R123456", 4.0, "INV-2601002", "PL-05", 7),
        ("P-7003", "R777777", 1.0, "", "PL-06", 7),             # sin factura: no se sube
        ("P-7004", "R888888", 10.0, "INV-2601003", "PL-07", 45),  # buque fuera de ventana
        ("P-7005", "R999999", 3.0, "INV-2601004", "PL-08", None), # todavía sin despachar
        ("P-7006", "R101010", 8.0, "INV-2601005", "PL-09", -5),
    ]
    filas = [TRANSIT_COLUMNAS]
    for pedido, parte, qty, fc, lista, buque in lineas:
        llega = d(buque) if buque is not None else None
        base = llega or d(20)
        estado = "In transit" if llega else "Scheduled for pickup"
        filas.append([
            pedido, "HAMBURG WORKS GMBH, HAMBURG", estado, con_hora(d(-1)), f"OC-{pedido[2:]}",
            parte, qty, fc or None, lista, "PCS", "FCA", sin_hora(base - dt.timedelta(days=25)),
            sin_hora(base + dt.timedelta(days=10)), con_hora(base - dt.timedelta(days=30)),
            sin_hora(base - dt.timedelta(days=28)), con_hora(base - dt.timedelta(days=27)),
            "Standard", "BUENOS AIRES TERMINAL", "PO",
            "Economy", f"REF-{pedido}", "40' HC", "TGHU1234567", "OCEAN LINE A",
            "SEA STAR, OCEAN PEARL", "102W, 104S", "OLAU900100", "OLHB900100", "PFR-1",
            con_hora(base - dt.timedelta(days=26)), con_hora(base - dt.timedelta(days=24)),
            con_hora(base - dt.timedelta(days=27)), "HAMBURG",
            sin_hora(llega) if llega else None, "BUENOS AIRES",
            con_hora(llega) if llega else None, None,
            con_hora(base - dt.timedelta(days=26)),       # segundo "Loaded for pre-carriage"
            None, None, "CT-1",
            sin_hora(base + dt.timedelta(days=3)) if llega else None, None,
            "PLANTA 01", "PLANTA 01", None,
        ])
    return {"In Transit": filas}


# ── 04-PARTS ─────────────────────────────────────────────────────────────────
PARTS_COLUMNAS = ["Part Number", "Part Description", "Quantity", "Invoice Number", "Packer ID",
                  "Load ID", "Case Number", "Container", "Packing Receiving Location",
                  "Reqt Ship Date", "At Packing Receiving", "At Packing Laydown",
                  "At Packing Loaded", "Shipped  Packing", "Actual Shipped", None, None,
                  "Planned At Port", "Actual At Port", "Planned Departure", "Actual Departure",
                  "Planned Arrival at Port of Unload", "Actual Arrival at Port of Unload",
                  "Vessel"]


def parts_shipper(ref: dt.date):
    def d(n):
        return ref + dt.timedelta(days=n)

    # (parte, factura, packer, load, caja, lugar de empaque, días al arribo o None)
    piezas = [
        ("AA10001", "PS-880001", "PK-01", "L-5001", "C-001", "PACK-NORTE", 10),
        ("AA10002", "PS-880001", "PK-01", "L-5001", "C-001", "PACK-NORTE", 10),
        ("AA10003", "PS-880001", "PK-02", "L-5001", "C-002", "PACK-NORTE", 10),
        ("AA10001", "PS-880001", "PK-03", "L-5002", "C-003", "PACK-NORTE", 18),  # otro Load
        ("AA10004", "PS-880002", "", "L-5003", "C-004", "PACK-SUR", 4),          # sin Packer ID
        ("AA10005", "PS-880002", "PK-04", "L-5003", "C-005", "", 4),             # sin empaque
        ("AA10006", "PS-880003", "PK-05", "L-5004", "C-006", "PACK-SUR", None),  # sin arribo
        ("AA10007", "PS-880003", "PK-05", "L-5004", "C-007", "PACK-SUR", -20),   # ya arribó
        ("AA10008", "", "PK-06", "L-5005", "C-008", "PACK-SUR", 6),              # sin factura
        ("AA10009", "PS-880004", "PK-07", "L-5006", "C-009", "PACK-NORTE", 25),
    ]
    filas = [PARTS_COLUMNAS]
    for parte, fc, packer, load, caja, lugar, arribo in piezas:
        base = d(arribo if arribo is not None else 15)
        filas.append([
            parte, f"PART {parte[-3:]}", 2, fc or None, packer or None, load, caja, "MSCU1234567",
            lugar or None,
            base - dt.timedelta(days=40),                          # fecha real
            _serial_excel(base - dt.timedelta(days=38)),           # serial de Excel
            _serial_excel(base - dt.timedelta(days=37)),
            base - dt.timedelta(days=36),
            _serial_excel(base - dt.timedelta(days=35)),
            base - dt.timedelta(days=34),
            "A" if parte.endswith("1") else None, "OK",            # columnas sin título
            base - dt.timedelta(days=30), None, base - dt.timedelta(days=28), None,
            d(arribo) if arribo is not None else None, None, "OCEAN PEARL",
        ])
    # La misma pieza otra vez, con menos datos: el dedup se queda con la completa.
    incompleta = list(filas[1])
    incompleta[2], incompleta[13] = None, None
    filas.insert(2, incompleta)
    return {"Part Search Results": filas, "Sheet2": [], "Sheet3": []}


# ── Correcciones de la operación ─────────────────────────────────────────────
def correcciones_air(ref: dt.date) -> str:
    """Dos correcciones sobre 01-AIR: la factura que el portal nunca mandó bien y un
    ETA que llegó vacío. El encabezado es el de la salida + `_match` y `_motivo`."""
    vacia = [""] * len(AIR_COLUMNAS)
    fc = list(vacia)
    fc[0], fc[4] = "HX1000005", "705123999"
    eta = list(vacia)
    eta[0], eta[7] = "HX1000011", f"{ref + dt.timedelta(days=14):%d/%m/%Y}"
    lineas = [AIR_COLUMNAS + ["_match", "_motivo"],
              fc + ["", "factura confirmada por mail con el forwarder"],
              eta + ["", "ETA informado por la aerolínea"]]
    return "".join(";".join(l) + "\r\n" for l in lineas)


def generar(dir_raw: Path, dir_correcciones: Path, ref: dt.date) -> list[Path]:
    dir_raw, dir_correcciones = Path(dir_raw), Path(dir_correcciones)
    creados = [
        _guardar(dir_raw / "air_forwarder" / "Shipment Profile Report.xlsx", air_forwarder(ref)),
    ]
    cuerpo, auxiliares = ocean_forwarder(ref)
    creados.append(_guardar(dir_raw / "ocean_forwarder" / "Active Shipments.xlsx", cuerpo))
    creados.append(_guardar(dir_raw / "ocean_forwarder" / "daily_report.xlsx",
                            auxiliares))
    creados.append(_guardar(dir_raw / "transit_report" / "In Transit Report.xlsx",
                            transit_report(ref)))
    creados.append(_guardar(dir_raw / "parts_shipper" / "Part Search Results.xlsx",
                            parts_shipper(ref)))

    dir_correcciones.mkdir(parents=True, exist_ok=True)
    ruta = dir_correcciones / "01-AIR.csv"
    ruta.write_bytes(correcciones_air(ref).encode("cp1252"))
    creados.append(ruta)
    return creados


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fecha-referencia", help="DD/MM/AAAA; default hoy")
    args = ap.parse_args(argv)
    ref = (dt.datetime.strptime(args.fecha_referencia, "%d/%m/%Y").date()
           if args.fecha_referencia else dt.date.today())
    cfg = cargar_config_global()
    for ruta in generar(Path(cfg["dir_raw"]), Path(cfg["dir_correcciones"]), ref):
        print(f"  {ruta.relative_to(RAIZ) if ruta.is_relative_to(RAIZ) else ruta}")
    print(f"Datos demo generados (fecha de referencia {ref:%d/%m/%Y}). "
          f"Siguiente paso: python src/main.py --lote")
    return 0


if __name__ == "__main__":
    sys.exit(main())
