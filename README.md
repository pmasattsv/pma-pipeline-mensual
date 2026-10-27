# 🌧️ PMA Early Warnings — Recomendación Agroclimática Mensual

**Proyecto:** Plataforma de Monitoreo Agroclimático (PMA)
**Departamentos:** Caquetá · Amazonas · Putumayo
**Fuentes:** IDEAM (pronóstico mensual) + CHIRPS / AgERA5 (histórico)

Cada mes el proceso toma el pronóstico de precipitación del IDEAM **para el mes siguiente**,
lo cruza con la climatología histórica, genera los productos (cambio porcentual de
precipitación y riesgo climático según la categoría SPEI modal de años análogos) y
**publica los GeoTIFF en el GeoServer de AClimate**. Corre desatendido en GitHub Actions.

---

## ⚙️ Cómo funciona

Un único workflow, `.github/workflows/pma-produccion.yml`, corre **todos los días del 8 al 27
de cada mes a las 06:00 (hora Colombia)** y ejecuta `src/orquestador.py`:

1. Calcula el mes objetivo = **mes siguiente** al actual (en noviembre → diciembre;
   en diciembre → enero del año siguiente).
2. Consulta el archivo del IDEAM para ese mes:
   - si no existe todavía → queda **pendiente** y reintenta al día siguiente;
   - si existe pero su fecha de subida (`Last-Modified`) es de un mes anterior → es la corrida
     vieja del IDEAM, queda **pendiente**;
   - si existe y fue subido este mes → ejecuta `src/resampling_PMA.py` y **publica**.
3. Registra lo publicado en `state/publicados.json` para no repetirlo en el resto del ciclo
   (el estado se reinicia solo al cambiar de mes).
4. Guarda mapas y GeoTIFF como artefacto, hace un commit de bitácora (`logs/corridas.log`)
   que además mantiene activo el cron, y envía correos a **todos** los destinatarios:
   - **Pronóstico publicado:** explica los dos mapas y adjunta los 6 PNG.
   - **Publicación retrasada:** si una corrida falla. Se envía **una sola vez por ciclo**,
     aunque el proceso siga fallando en los reintentos diarios.
   - **Pronóstico no disponible:** si llega el día 27 y el mes siguiente no se publicó
     (el motivo cambia según si fue el IDEAM o una falla técnica). Una vez por ciclo.

   Los días en que el IDEAM todavía no ha liberado el archivo no se envía nada.
   Los textos de los correos están en `src/notificaciones.py` (firma, enlace a AClimate, etc.).

La publicación es "todo o nada": antes de subir se verifica que existan los 6 stores
(3 departamentos × 2 variables); si falta alguno, no se sube nada y la corrida queda en error.

---

## 📁 Estructura

```
.
├── .github/workflows/pma-produccion.yml  ← Workflow de producción (único)
├── src/orquestador.py                    ← Decide qué mes procesar y cuándo
├── src/resampling_PMA.py                 ← Procesa y publica UN mes
├── src/notificaciones.py                 ← Textos de los correos y control de avisos
├── config/destinatarios.txt              ← Correos que reciben las notificaciones
├── data/precip_spei_mensual.nc           ← Histórico CHIRPS/AgERA5 (mar-1981 a jun-2025)
├── data/departamentos.geojson            ← Límites de los 3 departamentos (Natural Earth 10m)
├── state/publicados.json                 ← Estado del ciclo (lo actualiza el bot)
├── logs/corridas.log                     ← Bitácora (la actualiza el bot)
├── outputs/                              ← Salidas (se crea en cada corrida; no se versiona)
├── requirements.txt                      ← Versiones fijas (Python 3.12)
└── env.example.txt                       ← Plantilla de credenciales para correr en local
```

---

## 📧 Destinatarios de los correos

Todos los correos (pronóstico publicado, publicación retrasada y pronóstico no disponible)
se envían a la lista de `config/destinatarios.txt`.
El archivo tiene **un correo por línea**; las líneas vacías y las que empiezan con `#` se ignoran.

```
# Destinatarios de las notificaciones del proceso PMA (exito, error y alertas).
d.agudelo@cgiar.org
otra.persona@ejemplo.org
```

### Agregar o quitar correos desde la web de GitHub

No hace falta instalar nada ni clonar el repositorio:

1. En la página del repositorio, abre la carpeta **`config`** y luego el archivo **`destinatarios.txt`**.
2. Haz clic en el **lápiz** (✏️ *Edit this file*), arriba a la derecha del contenido.
3. Edita la lista:
   - **Agregar:** escribe el correo nuevo en una línea nueva, al final.
   - **Quitar:** borra la línea completa de ese correo.
   - **Pausar temporalmente:** pon `#` al inicio de la línea (por ejemplo `# persona@ejemplo.org`)
     y para reactivarlo quita el `#`.
4. Haz clic en **Commit changes…**, escribe un mensaje corto (por ejemplo
   *"Agrega a persona@ejemplo.org a notificaciones"*), deja marcada la opción
   **Commit directly to the `main` branch** y confirma con **Commit changes**.

El cambio aplica desde la **siguiente corrida**; no hay que tocar nada más.

**Reglas para que no falle:**

- Un solo correo por línea: no separes varios con comas ni punto y coma en la misma línea.
- Debe quedar **al menos un correo** sin `#`.
- Si algún correo está mal escrito (sin `@`, con espacios, etc.) o el archivo queda sin correos,
  la corrida **falla al inicio** con un mensaje que indica el problema, y en ese caso no se envía
  ningún correo. Si una corrida aparece en rojo en la pestaña **Actions** justo en el paso
  *"Leer destinatarios"*, revisa este archivo.
- Los correos repetidos se envían una sola vez.

> ⚠️ Si el repositorio es público, estos correos quedan visibles para cualquiera.

---

## 🔐 Secrets (Settings → Secrets and variables → Actions)

| Secret             | Contenido                                                     |
| ------------------ | ------------------------------------------------------------- |
| `GEO_URL`          | URL REST del GeoServer de AClimate                            |
| `GEO_WORKSPACE`    | Workspace donde están los mosaicos                            |
| `GEO_USER`         | Usuario del GeoServer                                         |
| `GEO_PWD`          | Contraseña del GeoServer                                      |
| `GEO_STORE_PREFIX` | (opcional) prefijo de los stores; por defecto = workspace     |
| `GEO_FILE_PREFIX`  | (opcional) prefijo de los granules; por defecto = workspace   |
| `MAIL_USER`        | Correo emisor de notificaciones                               |
| `MAIL_PASSWORD`    | Contraseña del correo (Gmail: **App Password** con 2FA)       |

Los stores esperados en GeoServer se llaman `{GEO_STORE_PREFIX}_st_{departamento}_{variable}`,
con departamento ∈ {caqueta, amazonas, putumayo} y variable ∈ {pct_change, spei}.

---

## ▶️ Ejecución manual y pruebas

**Actions → PMA - Produccion → Run workflow.** Inputs (todos opcionales):

| Input           | Uso                                                                                   |
| --------------- | ------------------------------------------------------------------------------------- |
| `skip_publish`  | `1` = **modo prueba**: genera mapas sin publicar, sin tocar el estado y sin correos    |
| `base_month/year` | Simula otro mes base (se procesa el siguiente a ese)                                |
| `require_fresh` | `0` = procesa aunque el archivo del IDEAM sea de una corrida anterior                  |

Para revisar los mapas de una prueba, descarga el artefacto de la corrida.

### En local

```bash
pip install -r requirements.txt
export PMA_MONTH=12 PMA_YEAR=2026 PMA_SKIP_PUBLISH=1
python src/resampling_PMA.py            # un mes puntual, sin publicar
# o el ciclo completo, sin publicar:
PMA_SKIP_PUBLISH=1 python src/orquestador.py
```

Para publicar desde local exporta `GEO_URL`, `GEO_WORKSPACE`, `GEO_USER` y `GEO_PWD`
(o copia `env.example.txt` a `env.txt` para las credenciales).

### Variables de entorno

| Variable              | Default                     | Descripción                                        |
| --------------------- | --------------------------- | -------------------------------------------------- |
| `PMA_MONTH/PMA_YEAR`  | — (requeridas en el script) | Mes objetivo de `resampling_PMA.py`                |
| `PMA_DEPARTMENTS`     | `Caquetá,Amazonas,Putumayo` | Departamentos a procesar                           |
| `PMA_K`               | `3`                         | Nº de años análogos para la moda SPEI              |
| `PMA_N_MESES`         | `1`                         | Meses hacia adelante que procesa el orquestador    |
| `PMA_REQUIRE_FRESH`   | `1`                         | Exigir que el archivo del IDEAM sea del mes actual |
| `PMA_WINDOW_LAST_DAY` | `27`                        | Día en que se dispara la alerta si no se publicó   |
| `PMA_SKIP_PUBLISH`    | vacío                       | `1` = no publica                                   |
| `PMA_IDEAM_BASE_URL`  | servidor del IDEAM          | Sobrescribe la URL base (pruebas)                  |

---

## 📦 Productos generados (por departamento y mes)

| Archivo                                   | Contenido                                           |
| ----------------------------------------- | --------------------------------------------------- |
| `cambio_pct_MM_YYYY_{depto}.tif`          | Cambio % de precipitación vs climatología           |
| `spei_moda_MM_YYYY_{depto}.tif`           | Categoría SPEI modal (1 seco, 2 normal, 3 húmedo)   |
| `mapa_cambio_pct_MM_YYYY_{depto}.png`     | Mapa del cambio porcentual                          |
| `mapa_riesgo_clima_MM_YYYY_{depto}.png`   | Mapa de riesgo climático (categoría SPEI)           |
| `qc_pixels_MM_YYYY_{depto}.png`           | Diagnóstico de píxeles corregidos por el QC         |

Los GeoTIFF (EPSG:4326, float32, LZW) se publican como granules
`{GEO_FILE_PREFIX}_st_{depto}_{variable}_YYYYMM.tif` y quedan disponibles vía WMS/WCS en AClimate.

---

## Datos

- `data/departamentos.geojson` se extrajo de Natural Earth 10m admin-1
  (`ne_10m_admin_1_states_provinces`, campos `name` → `departamento` e `iso_3166_2`).
- El histórico cubre marzo de 1981 a junio de 2025, así que la climatología usa 44 o 45 años
  según el mes.

## Licencia

Uso libre para instituciones y personas interesadas en servicios agroclimáticos.
Si lo usas o adaptas, cita el repositorio y las fuentes originales (IDEAM, CHIRPS, AgERA5, Natural Earth).
