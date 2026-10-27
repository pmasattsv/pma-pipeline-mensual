#!/usr/bin/env python3
"""
Arma los correos de la corrida a partir de outputs/resultado.json y decide cuales enviar.
Lo ejecuta el workflow (paso "Preparar correos") desde la raiz del repo.

Correos (todos van a config/destinatarios.txt):
  - OK              : se publico el pronostico (con la explicacion de los mapas).
  - Retraso         : hubo un error. Se envia UNA sola vez por ciclo, aunque el
                      proceso siga fallando en los reintentos diarios.
  - Sin pronostico  : ultimo dia de la ventana y el mes no se publico. Una vez por ciclo.

El control de "una vez por ciclo" se guarda en state/publicados.json
(claves error_notificado / alerta_notificada) y se reinicia al cambiar de ciclo.

Escribe las salidas en $GITHUB_OUTPUT (o en stdout si no existe, para pruebas locales).
"""
import os
import json
import datetime

RESULTADO = "outputs/resultado.json"
ESTADO = "state/publicados.json"
ENLACE_ACLIMATE = "https://amazonia.aclimate.org/scenario"
FIRMA = "Programa Mundial de Alimentos (PMA)"
DEPTOS = "Caquetá, Amazonas y Putumayo"

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def sumar(ym, k):
    y, m = map(int, ym.split("-"))
    t = y * 12 + (m - 1) + k
    return f"{t // 12}-{t % 12 + 1:02d}"


def largo(ym):
    y, m = map(int, ym.split("-"))
    return f"{MESES[m - 1]} de {y}"


def corto(ym):
    y, m = map(int, ym.split("-"))
    return f"{MESES[m - 1]} {y}"


def nombre_mes(ym):
    return MESES[int(ym.split("-")[1]) - 1]


def cargar_estado(ciclo):
    try:
        with open(ESTADO, encoding="utf-8") as f:
            est = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        est = {}
    if est.get("ciclo") != ciclo:
        est = {"ciclo": ciclo, "publicados": []}
    return est


def guardar_estado(est):
    os.makedirs(os.path.dirname(ESTADO), exist_ok=True)
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(est, f, ensure_ascii=False, indent=2)


def pie_tecnico(texto, run_url):
    return ("\n\n---\n"
            f"Para quien administre el sistema: {texto}\n"
            f"Detalle de la corrida: {run_url}")


def main():
    run_url = "{}/{}/actions/runs/{}".format(
        os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        os.environ.get("GITHUB_REPOSITORY", "pmasattsv/pma-pipeline-mensual"),
        os.environ.get("GITHUB_RUN_ID", "local"))
    modo_prueba = os.environ.get("MODO_PRUEBA", "").strip().lower() == "true"

    try:
        with open(RESULTADO, encoding="utf-8") as f:
            r = json.load(f)
        falla_temprana = False
    except (FileNotFoundError, json.JSONDecodeError):
        r, falla_temprana = {}, True

    hoy = datetime.date.today()
    ciclo = r.get("ciclo") or f"{hoy.year}-{hoy.month:02d}"
    objetivo = (r.get("objetivos") or [sumar(ciclo, 1)])[0]
    publicados = r.get("publicados", [])
    pendientes = r.get("pendientes", [])
    errores = r.get("errores", [])
    if falla_temprana:
        errores = ["la corrida fallo antes de terminar (no se genero resultado.json)"]

    # ---------------- Decidir que se notifica ----------------
    alerta = bool(r.get("alerta_fin_ventana")) and not modo_prueba
    notificar_alerta = False
    notificar_error = False
    if not modo_prueba and (alerta or errores):
        est = cargar_estado(ciclo)
        if alerta and not est.get("alerta_notificada"):
            notificar_alerta = True
            est["alerta_notificada"] = True
        # Si sale el correo de "sin pronostico", ese ya cubre el error del dia.
        if errores and not alerta and not est.get("error_notificado"):
            notificar_error = True
            est["error_notificado"] = True
        if notificar_alerta or notificar_error:
            guardar_estado(est)

    # ---------------- Correo OK ----------------
    pub_corto = " y ".join(corto(p) for p in publicados) or corto(objetivo)
    pub_largo = " y ".join(largo(p) for p in publicados) or largo(objetivo)
    asunto_ok = f"Pronóstico agroclimático de {pub_corto} – {DEPTOS}"
    cuerpo_ok = f"""Cordial saludo,

Compartimos el pronóstico agroclimático para {pub_largo} en los departamentos de {DEPTOS}.

En los adjuntos encontrará dos mapas por departamento:

• Cambio porcentual de la precipitación: compara la lluvia pronosticada para el mes con el promedio histórico de ese mismo mes. Los valores negativos (tonos naranja y rojo) indican menos lluvia de lo normal; los positivos (tonos verde y azul), más lluvia de lo normal.

• Riesgo climático: clasifica cada zona como Verano (seco), Normal o Invierno (húmedo), a partir de los años del registro histórico cuyo comportamiento de lluvia fue más parecido al pronosticado.

Estos mapas son una orientación para la planificación y deben usarse junto con la información local y los boletines oficiales.

Fuentes: pronóstico de precipitación del IDEAM; registro histórico CHIRPS y AgERA5 (1981–2025).
Los mapas también se pueden consultar en AClimate Amazonía: {ENLACE_ACLIMATE}

Este es un mensaje automático.

{FIRMA}"""

    # ---------------- Correo de retraso (error) ----------------
    asunto_retraso = f"Pronóstico agroclimático de {corto(objetivo)} – publicación retrasada"
    cuerpo_retraso = f"""Cordial saludo,

El pronóstico agroclimático de {largo(objetivo)} para {DEPTOS} presentó un inconveniente técnico durante su generación y su publicación se retrasará.

El sistema lo seguirá intentando automáticamente cada día y, en cuanto quede disponible, recibirá el correo habitual con los mapas. No es necesario que haga nada.

Este es un mensaje automático.

{FIRMA}""" + pie_tecnico(
        (f"la corrida del ciclo {ciclo} falló antes de terminar. " if falla_temprana else
         f"falló el procesamiento o la publicación de {objetivo} (ciclo {ciclo}); ver el log. ") +
        "Este aviso se envía una sola vez por ciclo; los reintentos siguen a diario.", run_url)

    # ---------------- Correo sin pronostico (fin de ventana) ----------------
    siguiente = sumar(objetivo, 1)
    if pendientes:
        motivo = "porque el IDEAM no liberó a tiempo el pronóstico de precipitación en el que se basa"
        si_luego = "Si el IDEAM lo publica más adelante, el pronóstico podrá generarse y se lo enviaremos."
        detalle = f"pendiente por IDEAM: {', '.join(pendientes)}"
    else:
        motivo = "por un inconveniente técnico que no se pudo resolver dentro del periodo de publicación"
        si_luego = "Si el inconveniente se resuelve, el pronóstico podrá generarse y se lo enviaremos."
        detalle = f"errores: {', '.join(errores) or '-'}"
    asunto_sin = f"Pronóstico agroclimático de {corto(objetivo)} – no disponible este mes"
    cuerpo_sin = f"""Cordial saludo,

Le informamos que este mes no se publicará el pronóstico agroclimático de {largo(objetivo)} para {DEPTOS}, {motivo}.

{si_luego} De lo contrario, la próxima publicación será la de {largo(siguiente)}, a partir del 8 de {nombre_mes(sumar(ciclo, 1))}.

Este es un mensaje automático.

{FIRMA}""" + pie_tecnico(
        f"terminó la ventana del ciclo {ciclo} (días 8 al 27) sin publicar {objetivo} ({detalle}). "
        "Si se resuelve, se puede lanzar a mano desde Actions.", run_url)

    # ---------------- Resumen para el log y la bitacora ----------------
    j = lambda k: ", ".join(r.get(k, [])) or "-"
    resumen = "\n".join([
        f"Ciclo de ejecucion:     {ciclo}",
        f"Mes objetivo:           {objetivo}",
        f"Publicados ahora:       {j('publicados')}",
        f"Ya estaban publicados:  {j('ya_estaban')}",
        f"Pendientes (IDEAM):     {j('pendientes')}",
        f"Errores:                {', '.join(errores) or '-'}",
        f"Modo prueba:            {modo_prueba}",
        f"Correo retraso:         {'se envia' if notificar_error else 'no'}",
        f"Correo sin pronostico:  {'se envia' if notificar_alerta else 'no'}",
    ])
    print(resumen)

    salidas = {
        "periodo": ciclo,
        "publicados": str(len(publicados)),
        "errores": str(len(errores)),
        "notificar_error": str(notificar_error).lower(),
        "notificar_alerta": str(notificar_alerta).lower(),
        "asunto_ok": asunto_ok, "cuerpo_ok": cuerpo_ok,
        "asunto_retraso": asunto_retraso, "cuerpo_retraso": cuerpo_retraso,
        "asunto_sin": asunto_sin, "cuerpo_sin": cuerpo_sin,
    }
    destino = os.environ.get("GITHUB_OUTPUT")
    with (open(destino, "a", encoding="utf-8") if destino else open(os.devnull, "w")) as f:
        for k, v in salidas.items():
            f.write(f"{k}<<__FIN_{k}__\n{v}\n__FIN_{k}__\n")
    if not destino:
        for k, v in salidas.items():
            print(f"\n===== {k} =====\n{v}")


if __name__ == "__main__":
    main()
