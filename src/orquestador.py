#!/usr/bin/env python3
"""
Orquestador de produccion PMA.

Cada corrida (pensada para ejecutarse a diario dentro de una ventana):
  1. Calcula el mes objetivo: el mes SIGUIENTE al mes actual (PMA_N_MESES=1 por
     defecto), manejando el cambio de anio (dic -> ene del anio siguiente).
     Ej.: el ciclo de noviembre publica diciembre.
  2. Para cada objetivo revisa en el IDEAM:
       - si el archivo NO existe aun (404)                 -> pendiente (reintenta manana).
       - si existe pero es de una corrida ANTERIOR         -> pendiente (reintenta manana).
         (se compara la fecha de subida 'Last-Modified' contra el mes de ejecucion:
          el IDEAM reemplaza los archivos cada mes con su corrida mas reciente, y
          nosotros exigimos la corrida del mes actual, no la del mes pasado).
       - si existe y es de la corrida de ESTE mes          -> procesa y publica.
  3. Guarda el estado del ciclo para no reprocesar lo ya publicado ese mes.
     El estado se reinicia cuando cambia el mes de ejecucion (ciclo).

Codigos de salida:
  0 = todo lo disponible/fresco quedo publicado, o aun no hay datos frescos (reintenta manana)
  2 = habia datos disponibles y frescos pero el pipeline fallo (requiere atencion)

Variables de entorno:
  PMA_N_MESES         cuantos meses hacia adelante generar (default 1 = solo el siguiente)
  PMA_BASE_YEAR /     forzar el mes base en pruebas (default: hoy)
  PMA_BASE_MONTH
  PMA_REQUIRE_FRESH   "1" (default) exige que el archivo sea del mes actual segun Last-Modified.
                      "0" desactiva el chequeo de frescura (procesa con solo que exista).
  PMA_STATE_FILE      ruta del estado (default ../state/publicados.json)
  PMA_WINDOW_LAST_DAY ultimo dia de la ventana de publicacion (default 27). Si en ese dia
                      algun mes objetivo sigue sin publicarse, se marca 'alerta_fin_ventana'.
  Ademas las que usa resampling_PMA.py (GEO_*, PMA_DEPARTMENTS, PMA_HTTP_*, etc.)
"""
import os
import sys
import json
import time
import datetime
import subprocess
import email.utils
import urllib.request
import urllib.error

IDEAM_BASE_URL = (os.environ.get("PMA_IDEAM_BASE_URL", "").strip()
                  or "https://bart.ideam.gov.co/wrfideam/new_modelo/CPT/netcdf/PREC")
AQUI = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("PMA_STATE_FILE", os.path.join(AQUI, "..", "state", "publicados.json"))
RESULT_FILE = os.path.join(AQUI, "..", "outputs", "resultado.json")


def mes_base():
    y = os.environ.get("PMA_BASE_YEAR", "").strip()
    m = os.environ.get("PMA_BASE_MONTH", "").strip()
    if y and m:
        return int(y), int(m)
    hoy = datetime.date.today()
    return hoy.year, hoy.month


def meses_objetivo(year, month, n):
    """Los n meses siguientes a (year, month), con cambio de anio."""
    objetivos = []
    for i in range(1, n + 1):
        mm = month + i
        yy = year + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        objetivos.append((yy, mm))
    return objetivos


def url_ideam(year, month):
    return f"{IDEAM_BASE_URL}/ENSAMBLE_PREC_MENSUAL_{month:02d}_{year}.nc"


def ideam_info(year, month, intentos=3, espera=15):
    """
    Devuelve (existe, last_modified):
      existe        -> True si HTTP 200; False si 404 o no se pudo confirmar.
      last_modified -> datetime (UTC) de la cabecera Last-Modified, o None si no viene.
    """
    url = url_ideam(year, month)
    for intento in range(1, intentos + 1):
        try:
            # GET sin leer el cuerpo: solo estado y cabeceras.
            with urllib.request.urlopen(url, timeout=60) as resp:
                if resp.status == 200:
                    lm = resp.headers.get("Last-Modified")
                    dt = email.utils.parsedate_to_datetime(lm) if lm else None
                    return True, dt
                return False, None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False, None
            print(f"    IDEAM respondio HTTP {e.code} (intento {intento}/{intentos})")
        except Exception as e:
            print(f"    IDEAM error de conexion: {e} (intento {intento}/{intentos})")
        if intento < intentos:
            time.sleep(espera)
    return False, None


def es_del_ciclo(last_modified, base_y, base_m):
    """True si el archivo fue subido dentro del mes de ejecucion (corrida de este mes)."""
    if last_modified is None:
        return None  # el servidor no reporto fecha: no se puede verificar
    return (last_modified.year == base_y) and (last_modified.month == base_m)


def cargar_estado(ciclo):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            est = json.load(f)
        if est.get("ciclo") == ciclo:
            return est
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"ciclo": ciclo, "publicados": []}


def guardar_json(ruta, data):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ejecutar_pipeline(year, month):
    env = os.environ.copy()
    env["PMA_MONTH"] = str(month)
    env["PMA_YEAR"] = str(year)
    print(f"    -> Ejecutando pipeline para {year}-{month:02d} ...")
    res = subprocess.run([sys.executable, "resampling_PMA.py"], cwd=AQUI, env=env)
    return res.returncode


def main():
    n = int(os.environ.get("PMA_N_MESES") or 1)
    ultimo_dia = int(os.environ.get("PMA_WINDOW_LAST_DAY") or 27)
    exigir_fresco = os.environ.get("PMA_REQUIRE_FRESH", "1").strip().lower() not in ("0", "false", "no")
    modo_prueba = os.environ.get("PMA_SKIP_PUBLISH", "").strip().lower() in ("1", "true", "yes", "si")
    base_y, base_m = mes_base()
    ciclo = f"{base_y}-{base_m:02d}"
    objetivos = meses_objetivo(base_y, base_m, n)

    print("=" * 62)
    print(f"Ciclo de ejecucion: {ciclo}   (exigir corrida del mes: {exigir_fresco})")
    if modo_prueba:
        print("MODO PRUEBA: se generan productos SIN publicar y sin modificar el estado.")
    print(f"Meses objetivo ({n}): " + ", ".join(f"{y}-{m:02d}" for y, m in objetivos))
    print("=" * 62)

    estado = cargar_estado(ciclo)
    ya_publicados = set(estado.get("publicados", []))
    resultado = {"ciclo": ciclo, "objetivos": [f"{y}-{m:02d}" for y, m in objetivos],
                 "publicados": [], "ya_estaban": [], "pendientes": [],
                 "no_existe": [], "corrida_anterior": [], "errores": [],
                 "generados_sin_publicar": [], "modo_prueba": modo_prueba,
                 "alerta_fin_ventana": False}

    for (y, m) in objetivos:
        tag = f"{y}-{m:02d}"
        print(f"\n[{tag}]")
        if tag in ya_publicados:
            print("    ya se publico en este ciclo -> se omite.")
            resultado["ya_estaban"].append(tag)
            continue

        existe, last_mod = ideam_info(y, m)
        if not existe:
            print("    el IDEAM aun no publica este archivo -> pendiente (reintenta manana).")
            resultado["no_existe"].append(tag)
            continue

        fresco = es_del_ciclo(last_mod, base_y, base_m)
        lm_str = last_mod.date().isoformat() if last_mod else "sin fecha"
        if exigir_fresco and fresco is False:
            print(f"    existe pero es de una corrida ANTERIOR (subido {lm_str}) -> pendiente.")
            resultado["corrida_anterior"].append(tag)
            continue
        if exigir_fresco and fresco is None:
            print("    ADVERTENCIA: el servidor no reporto fecha de subida; no se pudo verificar frescura.")
            print("    Se procesa igual (revisa PMA_REQUIRE_FRESH si prefieres bloquear estos casos).")

        print(f"    disponible y de la corrida de este mes (subido {lm_str}) -> procesando.")
        code = ejecutar_pipeline(y, m)
        if code == 0 and modo_prueba:
            print(f"    OK generado {tag} (modo prueba, no publicado).")
            resultado["generados_sin_publicar"].append(tag)
        elif code == 0:
            print(f"    OK publicado {tag}.")
            ya_publicados.add(tag)
            resultado["publicados"].append(tag)
        else:
            print(f"    ERROR el pipeline fallo para {tag} (codigo {code}).")
            resultado["errores"].append(tag)

    # 'pendientes' = todo lo que aun no se pudo publicar por falta de datos frescos del IDEAM
    resultado["pendientes"] = resultado["no_existe"] + resultado["corrida_anterior"]
    hoy = datetime.date.today()
    es_ultimo_dia = (os.environ.get("PMA_BASE_MONTH", "").strip() == "" and hoy.day >= ultimo_dia)
    sin_publicar = resultado["pendientes"] + resultado["errores"]
    if es_ultimo_dia and sin_publicar and not modo_prueba:
        resultado["alerta_fin_ventana"] = True

    if not modo_prueba:
        estado["publicados"] = sorted(ya_publicados)
        guardar_json(STATE_FILE, estado)
    guardar_json(RESULT_FILE, resultado)

    print("\n" + "=" * 62)
    print("RESUMEN:")
    print(f"  Publicados ahora    : {resultado['publicados'] or '-'}")
    print(f"  Ya estaban          : {resultado['ya_estaban'] or '-'}")
    print(f"  Sin publicar (IDEAM): {resultado['no_existe'] or '-'}")
    print(f"  Corrida anterior    : {resultado['corrida_anterior'] or '-'}")
    print(f"  Errores             : {resultado['errores'] or '-'}")
    if resultado["alerta_fin_ventana"]:
        print(f"  ALERTA: ultimo dia de la ventana y quedan meses sin publicar: {sin_publicar}")
    print("=" * 62)

    sys.exit(2 if resultado["errores"] else 0)


if __name__ == "__main__":
    main()
