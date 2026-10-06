# Freight Reports ETL

[![tests](https://github.com/DavidAvila4Logistics/freight-reports-etl/actions/workflows/tests.yml/badge.svg)](https://github.com/DavidAvila4Logistics/freight-reports-etl/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

A config-driven Python/Polars engine that turns the messy Excel reports sent by
freight forwarders into clean, validated CSV files for a customs-tracking
dashboard. Each forwarder gets **one YAML recipe** instead of a spreadsheet full of
formulas and macros.

> All data in this repository is fictitious. Run `tools/generar_datos_demo.py`
> to generate a realistic sample dataset.

---

## The problem

An importer receives a daily status report from each of its freight forwarders
(air, ocean, road/rail, spare parts). An operations analyst had to turn every one
of them into the exact CSV layout the customs dashboard loads: a fixed column
order, `DD/MM/YYYY` dates, comma decimals, no quoting. The work lived in a pile of
Excel templates, `VLOOKUP`s, nested `IF`s and a VBA macro, and every report was
messy in its own way:

| Forwarder | What arrives | What makes it messy |
|---|---|---|
| `air_forwarder` | "Shipment Profile Report" by e-mail | A filter preamble of variable length (the header moves rows), two empty leading columns that come and go, dates as real Excel dates *and* as text in two formats, invoice numbers mixed with internal references and codes from another series. |
| `ocean_forwarder` | Portal export **plus** a daily e-mail report | The child BL is blank in the portal and only exists in the second file, where the same invoice can appear twice and the first row is still empty; copy rows, several POs in one cell, one line per piece to aggregate. |
| `transit_report` | "In Transit Report" from a portal | Everything is text, extra columns in a different order, a duplicated column title holding different data, lines to collapse by part + invoice adding up quantities (the old macro). |
| `parts_shipper` | "Part Search Results" by e-mail | Raw Excel serial numbers in date columns, untitled columns in the middle, pieces without a packing location, invoice or arrival date. |

When something went wrong, the dashboard rejected the **whole** file an hour later.

## What the engine does

```
data/raw/<agent>/ ──► read ──► transform ──► operator ──► registry ──► validate ──► data/processed/<NN-NAME>.csv
   (latest file)       │        (YAML)      corrections    (upsert)       │                │
                header anchor,                 │              │      quality gates     cp1252 · ';' · CRLF
                dates, numbers   data/correcciones/   data/registro/   + automatic         │
                                  <NN-NAME>.csv       <agent>.csv     number checks   raw file moved to
                                                                                   back_up_data_used/
```

| Stage | Module | Highlights |
|---|---|---|
| 1. Read | `lector.py` | Header row found **by anchor** (`{ancla: "HAWB"}`) instead of a fixed row; leading columns dropped **up to a title**, whether they come or not; every cell canonicalized to text once: real dates, text dates in any declared format, Excel serials, es-AR numbers, flattened line breaks. |
| 2. Transform | `transformador.py` | 24 declarative operations: date window, `VLOOKUP`-style `cruzar` against an auxiliary file, invoice rules by prefix, majority-series cleanup, dedup by composite key with `sum` (the macro), exact output layout with `orden_columnas` (repeated titles by occurrence). |
| 3. Corrections | `correcciones.py` | Fixes typed by the operations team in a CSV with the output header. They win on **every** run, so a value the portal keeps sending wrong is never re-uploaded. Never adds rows. |
| 4. Registry | `registro.py` | One cumulative file per agent: a row per shipment (business key from the YAML) with first/last load date, state (`nuevo`, `modificado`, `sin_cambios`, `corregido`) and a content hash. It never deletes. |
| 5. Validate | `validador.py` | Declared checks per agent plus automatic ones that guard the number contract with the dashboard (see below). Errors block the output unless `--forzar`. |
| 6. Write & archive | `escritor.py`, `main.py` | The exact CSV contract; the consumed raw file is moved to a backup so an export that did not arrive is reported instead of re-processing yesterday's file. |

### One YAML per forwarder

Adding a forwarder means adding a YAML file, not code. A trimmed example:

```yaml
agente: air_forwarder
salida: {nombre: "01-AIR.csv"}
entrada:
  hoja: "Shipments"
  fila_encabezado: {ancla: "HAWB", buscar_hasta: 40}   # preamble of variable length
  columnas_omitir_iniciales: {hasta: "HAWB"}           # empty columns A/B, or not
formato:
  fecha_origen: ["%d-%b-%y %I:%M %p", "%d-%b-%y"]
transformaciones:
  - corregir_facturas:              # the nested-IF formula, invoice by invoice
      columna: "Customer References"
      eliminar_prefijos: ["9900", "REF"]
      reglas:
        - {prefijos: ["251XA", "261XA"], quitar: 2}
        - {prefijos: ["25", "26"], quitar: 2, anteponer: "7700"}
  - filtrar_ventana_fecha:          # ETA -15/+30, never drop a row with no invoice yet
      {columna: "ETA ", dias_atras: 15, dias_adelante: 30,
       conservar_si_vacia: "Customer References"}
validaciones:
  - no_vacias: {columna: "Customer References", nivel: advertencia, identificar_por: "HAWB"}
registro:
  clave: ["HAWB"]
```

### Quality gates

| Check | Why |
|---|---|
| `columnas_requeridas` | The forwarder changed its layout. |
| `fechas_validas` | A date that is not `DD/MM/YYYY` breaks the dashboard query. |
| `sin_duplicados`, `duplicados_consistentes` | Known rejection causes of the dashboard. |
| `no_vacias` + `identificar_por` | Turns "12 empty cells" into the list of shipments to chase. |
| `sin_caracter`, `patron_columna`, `columnas_iguales` | Format rules of specific columns. |
| *automatic:* `notacion_cientifica` | No cell can leave as `3,18447E+11`: the value has already lost digits. |
| *automatic:* `clave_destino_colapsada` | Two different 12-digit invoices that the dashboard reads with six significant digits would collide and reject the whole file. |

### Operator corrections

The analyst pastes the row in `data/correcciones/<NN-NAME>.csv`, deletes everything
except the key and the fixed value, and that value wins from then on:

```
HAWB;...;Customer References;...;_match;_motivo
HX1000005;;705123999;;;confirmed by e-mail with the forwarder
```

- an empty cell means *do not touch*; `<VACIO>` blanks it on purpose;
- `_match` (`HAWB=HX1000005`) is used when the column to fix **is** the key;
- one row can fix every piece of an invoice (`_match: Invoice Number=PS-880001`);
- a correction whose shipment is not in today's file is reported, never appended;
- a `.xlsx` saved by mistake instead of `.csv` is converted automatically;
- the registry renames the corrected shipment instead of duplicating it.

---

## Quickstart

```bash
python -m pip install -r requirements.txt

# 1. Generate fictitious raw reports for the four forwarders
python tools/generar_datos_demo.py

# 2. Run the daily batch (Windows: double-click run_all.bat)
python src/main.py --lote

# 3. Run the test suite
python -m pytest
```

The demo data deliberately includes every case above, so a single run exercises
every rule:

```
[1/4] air_forwarder
  Lector: 2 columnas antes de 'HAWB' se descartan
  Serie: 1 casillas homogeneizadas, 0 empates sin tocar
  Ventana: 1 filas retenidas sin 'Customer References'
  Correcciones (01-AIR.csv): 2 celdas en 2 cargas
Registro: air_forwarder.csv — 10 cargas acumuladas (nuevas=10 ... corregidas=2)
  [AVISO] (no_vacias) 'Customer References': 1 celdas vacías — HAWB: HX1000010
OK: 12 filas leídas -> 10 filas escritas en data\processed\01-AIR.csv
...
RESUMEN DE LA CORRIDA
  OK         air_forwarder           12 -> 10     avisos=4
  OK         ocean_forwarder         11 -> 6      avisos=3
  OK         transit_report           9 -> 5      avisos=1
  OK         parts_shipper           11 -> 6      avisos=1

4/4 agentes OK - 0 con problemas.
```

### Command line

```bash
python src/main.py --lote                         # agents in lote_diario (configs/global.yaml)
python src/main.py --lote todos                   # every configs/*.yaml
python src/main.py --agente parts_shipper         # a single agent
python src/main.py --agente air_forwarder --entrada path/to/file.xlsx --no-archivar
python tools/crear_esqueleto_correcciones.py      # correction files with the exact header
```

| Option | Effect |
|---|---|
| `--fecha-referencia DD/MM/AAAA` | "Today" for the date windows. |
| `--forzar` | Write the CSV even with validation errors (the finding stays in the log). |
| `--sin-correcciones` | Upload exactly what the portal sent. |
| `--no-archivar` | Leave the raw file in `data/raw/`. |
| `--sin-crudo-avisa` | In a batch, a missing raw file is a warning (`SIN CRUDO`), not a failure. |

Exit codes: `0` everything OK, `2` some agent was rejected or failed — ready for
Windows Task Scheduler (`run_all.bat /nopause`).

### Outputs

| File | Purpose |
|---|---|
| `data/processed/NN-NAME.csv` | What the dashboard loads: exact header, `;`, cp1252, CRLF, no quoting. |
| `data/registro/<agent>.csv` | Cumulative registry: every shipment ever loaded and its state. |
| `data/logs/corridas.log` | One line per run: file, rows in → out, status, warnings. |
| `data/logs/hallazgos.log` | Every finding, the analyst's work list. |
| `data/back_up_data_used/<agent>/` | Raw files already processed, kept as they arrived. |

---

## Project layout

```
configs/
  global.yaml             paths and the daily batch
  air_forwarder.yaml      one recipe per forwarder
  ocean_forwarder.yaml
  transit_report.yaml
  parts_shipper.yaml
src/
  main.py                 CLI: single agent, batch, summary and exit codes
  motor/
    config.py             YAML loading and defaults
    lector.py             Excel/CSV readers and cell canonicalization
    transformador.py      the declarative operations
    correcciones.py       operator corrections over the final CSV
    registro.py           cumulative registry (upsert by business key)
    validador.py          declared and automatic quality checks
    escritor.py           the output CSV contract
tools/
  generar_datos_demo.py   fictitious raw reports for the four forwarders
  crear_esqueleto_correcciones.py   correction files with the exact output header
tests/                    pytest suite, synthetic data only
```

The code, comments, YAML keys and log messages are in Spanish, the language of the
operations team that runs it.

## Tech stack

Python 3.12+ · Polars · openpyxl · PyYAML · pytest

## License

[MIT](LICENSE) © David Avila
