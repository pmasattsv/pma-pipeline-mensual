#!/usr/bin/env python3
"""
PMA Early Warnings - Recomendacion agroclimatica estacional.
Procesa UN mes objetivo: descarga el pronostico del IDEAM, lo cruza con el
historico CHIRPS/AgERA5, genera productos por departamento y los publica en
GeoServer (AClimate). En produccion lo invoca src/orquestador.py.

Variables de entorno:
    PMA_MONTH, PMA_YEAR      (REQUERIDAS) mes y anio objetivo
    PMA_DEPARTMENTS          lista separada por comas (default: Caquetá,Amazonas,Putumayo)
    PMA_K                    numero de anios analogos para la moda SPEI (default 3)
    PMA_SKIP_PUBLISH         "1" genera los productos pero NO publica en GeoServer
    PMA_IDEAM_BASE_URL       sobreescribe la URL base del IDEAM (pruebas / cambio de servidor)
    PMA_HTTP_RETRIES/BACKOFF reintentos y espera (s) de la descarga del IDEAM (default 4 / 20)
    GEO_USER, GEO_PWD, GEO_URL, GEO_WORKSPACE      requeridas para publicar
    GEO_STORE_PREFIX, GEO_FILE_PREFIX              opcionales (default = GEO_WORKSPACE)
    PMA_GRANULE_TAG          sufijo opcional para el nombre de los granules

Uso local (sin publicar):
    export PMA_MONTH=12 PMA_YEAR=2026 PMA_SKIP_PUBLISH=1
    python src/resampling_PMA.py
"""
import os
import re
import sys
import glob
import time
import shutil
import warnings
from zipfile import ZipFile

import matplotlib
matplotlib.use("Agg")   # backend headless: guarda los PNG sin pantalla

import requests
import numpy as np
import xarray as xr
import rioxarray  # noqa: F401  (registra el accessor .rio)
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import ListedColormap, BoundaryNorm, LinearSegmentedColormap, TwoSlopeNorm
from netCDF4 import Dataset
from xarray.backends import NetCDF4DataStore
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from shapely.geometry import mapping
from pyproj import Geod

warnings.filterwarnings("ignore")

# ============================================================
# PARAMETROS
# ============================================================
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_month_env = os.environ.get("PMA_MONTH", "").strip()
_year_env = os.environ.get("PMA_YEAR", "").strip()
if not _month_env or not _year_env:
    raise SystemExit("ERROR: faltan PMA_MONTH y/o PMA_YEAR (mes y anio objetivo).")
MONTH = int(_month_env)
YEAR = int(_year_env)
if not 1 <= MONTH <= 12:
    raise SystemExit(f"ERROR: PMA_MONTH fuera de rango: {MONTH}")

# Pronostico IDEAM. Plantilla: ENSAMBLE_PREC_MENSUAL_{MM}_{YYYY}.nc
IDEAM_BASE_URL = (os.environ.get("PMA_IDEAM_BASE_URL", "").strip()
                  or "https://bart.ideam.gov.co/wrfideam/new_modelo/CPT/netcdf/PREC")

# Historico de precipitacion y SPEI (versionado en el repo)
HIST_PATH = os.path.join(REPO_DIR, "data", "precip_spei_mensual.nc")

# Limites departamentales (extraidos de Natural Earth 10m admin-1, ver README)
DEPTS_PATH = os.path.join(REPO_DIR, "data", "departamentos.geojson")

# Departamentos: nombre -> slug usado en archivos y stores de GeoServer
DEPT_SLUGS = {
    "Caquet\u00e1": "caqueta",
    "Amazonas": "amazonas",
    "Putumayo": "putumayo",
}
_dept_env = os.environ.get("PMA_DEPARTMENTS", "").strip()
DEPARTMENTS = [d.strip() for d in _dept_env.split(",") if d.strip()] or list(DEPT_SLUGS)
_desconocidos = [d for d in DEPARTMENTS if d not in DEPT_SLUGS]
if _desconocidos:
    raise SystemExit(f"ERROR: departamentos no soportados: {_desconocidos}. "
                     f"Validos: {list(DEPT_SLUGS)}")

# Control de calidad (QC)
QC_UPPER_FACTOR = 3.0   # si pred > factor * clim -> se trunca
QC_CAP_FACTOR = 1.1     # cap: pred truncada a cap * clim

# Top-K analogos para la moda de SPEI
K = int(os.environ.get("PMA_K", 3))

OUTPUT_DIR = os.path.join(REPO_DIR, "outputs")

MONTH_STR = f"{MONTH:02d}"
PERIOD_TAG = f"{MONTH_STR}_{YEAR}"
os.makedirs(OUTPUT_DIR, exist_ok=True)
print(f"Periodo objetivo: {MONTH_STR}/{YEAR}")
print(f"Departamentos: {', '.join(DEPARTMENTS)}")
print(f"Directorio de salida: {OUTPUT_DIR}")


def union_geom(gdf):
    """Union de geometrias compatible con geopandas viejo y nuevo."""
    return gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union


# ============================================================
# 1. Pronostico IDEAM
# ============================================================
ideam_url = f"{IDEAM_BASE_URL}/ENSAMBLE_PREC_MENSUAL_{MONTH_STR}_{YEAR}.nc"
print(f"Descargando pronostico IDEAM: {ideam_url}")

_intentos = int(os.environ.get("PMA_HTTP_RETRIES", 4))
_espera = int(os.environ.get("PMA_HTTP_BACKOFF", 20))
r = None
for _i in range(1, _intentos + 1):
    try:
        r = requests.get(ideam_url, timeout=180)
        r.raise_for_status()
        break
    except Exception as _e:
        print(f"  Intento {_i}/{_intentos} de descarga fallo: {_e}")
        if _i == _intentos:
            raise
        time.sleep(_espera)

nc = Dataset("inmemory.nc", memory=r.content)
ds = xr.open_dataset(NetCDF4DataStore(nc)).rename({"latitude": "y", "longitude": "x"})
ds = ds.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=False)
ds = ds.rio.write_crs("EPSG:4326", inplace=False)
print(f"Pronostico cargado. Variables: {list(ds.data_vars)}")

# ============================================================
# 2. Historico
# ============================================================
if not os.path.exists(HIST_PATH):
    raise SystemExit(f"ERROR: no se encontro el historico: {HIST_PATH}")
print(f"Cargando historico: {HIST_PATH}")
hist = xr.open_dataset(HIST_PATH).transpose("time", "y", "x")
ds_filter = hist.sel(time=hist["time"].dt.month == MONTH)
print(f"Anios historicos disponibles para el mes {MONTH_STR}: {len(ds_filter.time)}")

# ============================================================
# 3. Alineacion de grillas
# ============================================================
lat_min, lat_max = float(ds_filter["y"].min()), float(ds_filter["y"].max())
lon_min, lon_max = float(ds_filter["x"].min()), float(ds_filter["x"].max())

if ds["y"][0] < ds["y"][-1]:
    ds_clip = ds.sel(y=slice(lat_min, lat_max), x=slice(lon_min, lon_max))
else:
    ds_clip = ds.sel(y=slice(lat_max, lat_min), x=slice(lon_min, lon_max))

precip = ds_filter["precip"].rio.reproject_match(ds_clip, resampling=Resampling.bilinear)
spei = ds_filter["spei"].rio.reproject_match(ds_clip, resampling=Resampling.nearest).round()
print(f"Grillas alineadas: {dict(ds_clip.sizes)}")

# ============================================================
# 4. Limites departamentales (archivo local)
# ============================================================
if not os.path.exists(DEPTS_PATH):
    raise SystemExit(f"ERROR: no se encontro el archivo de limites: {DEPTS_PATH}")
gdf_all = gpd.read_file(DEPTS_PATH).set_crs("EPSG:4326", allow_override=True)
depts = {}
for dept in DEPARTMENTS:
    gdf = gdf_all[gdf_all["departamento"] == dept].copy()
    if gdf.empty:
        raise SystemExit(f"ERROR: {dept} no esta en {DEPTS_PATH}")
    gdf["geometry"] = gdf.geometry.buffer(0)
    depts[dept] = gdf
print("Limites cargados.")

# ============================================================
# 5. Control de calidad
# ============================================================
clim = precip.mean("time", skipna=True)
pred = ds_clip["Prediccion(mm)"].clip(min=0)
pred_qc = xr.where(pred > QC_UPPER_FACTOR * clim, QC_CAP_FACTOR * clim, pred)

changed = (pred_qc != pred) & pred.notnull()
n_changed = int(changed.sum())
n_total = int(pred.notnull().sum())
print(f"QC: {n_changed} / {n_total} pixeles modificados ({100 * n_changed / max(n_total, 1):.2f}%)")

# ============================================================
# 6. Cambio porcentual
# ============================================================
eps = 1e-6
cambio_pct = xr.where(clim > eps, (pred_qc - clim) / clim * 100, 0)
chg_hist = xr.where(clim > eps, (precip - clim) / clim * 100, 0)
print(f"Cambio porcentual: [{float(cambio_pct.min()):.1f}%, {float(cambio_pct.max()):.1f}%]")

# ============================================================
# 7. Top-K analogos y moda SPEI
# ============================================================
print(f"Calculando Top-{K} analogos por pixel...")
diff = xr.apply_ufunc(np.abs, chg_hist - cambio_pct).transpose("time", "y", "x")
spei2 = spei.transpose("time", "y", "x")

d = diff.values
T, Y, X = d.shape
N = Y * X
d2 = d.reshape(T, N)

idx = np.argpartition(d2, K - 1, axis=0)[:K, :]
dK = np.take_along_axis(d2, idx, axis=0)
ordK = np.argsort(dK, axis=0)
idxK = np.take_along_axis(idx, ordK, axis=0)

spei_np = spei2.values.reshape(T, N)
spei_topK = spei_np[idxK, np.arange(N)]

mode = np.full(N, np.nan, dtype=np.float32)
for j in range(N):
    a = spei_topK[:, j]
    a = a[np.isfinite(a)]
    if a.size == 0:
        continue
    vals, cnts = np.unique(a.astype(np.int32), return_counts=True)
    mode[j] = vals[np.argmax(cnts)]

spei_mode = xr.DataArray(
    mode.reshape(Y, X), dims=("y", "x"),
    coords={"y": diff["y"], "x": diff["x"]}, name="spei_moda",
)
spei_mode = spei_mode.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=False)
spei_mode = spei_mode.rio.write_crs("EPSG:4326", inplace=False)

cats, cnt = np.unique(mode[np.isfinite(mode)].astype(int), return_counts=True)
label_map = {1: "Muy seco", 2: "Normal", 3: "Muy humedo"}
for c, n in zip(cats, cnt):
    print(f"  Cat {c} ({label_map.get(c, '?')}): {n} pixeles ({100 * n / N:.1f}%)")


# ============================================================
# 8. Utilidades de mapas
# ============================================================
def add_north_arrow(ax, lon, lat, size_deg=0.3):
    """Triangulo bicolor (negro/blanco)."""
    half = size_deg * 0.35
    ax.add_patch(plt.Polygon(
        [[lon, lat], [lon - half, lat - size_deg], [lon + half, lat - size_deg]],
        closed=True, facecolor="0.15", edgecolor="0.15", linewidth=0.6, zorder=25))
    ax.add_patch(plt.Polygon(
        [[lon, lat], [lon + half, lat - size_deg], [lon, lat - size_deg * 0.5]],
        closed=True, facecolor="white", edgecolor="0.15", linewidth=0.6, zorder=26))
    ax.text(lon, lat + size_deg * 0.25, "N", ha="center", va="bottom", fontsize=9,
            fontweight="bold", color="0.15", zorder=27)


def add_geodesic_scalebar(ax, lon_start, lat_bar, length_km=200, height_deg=0.08, linewidth=0.8):
    """Barra de escala geodesica de 4 segmentos alternos (negro/blanco)."""
    geod = Geod(ellps="WGS84")
    length_m = length_km * 1000
    ndiv = 4
    seg_m = length_m / ndiv
    h = height_deg
    lon_curr = lon_start
    for i in range(ndiv):
        lon_next, _, _ = geod.fwd(lon_curr, lat_bar, 90, seg_m)
        ax.add_patch(mpatches.Rectangle(
            (lon_curr, lat_bar), (lon_next - lon_curr), h,
            facecolor=("0.15" if i % 2 == 0 else "1.0"),
            edgecolor="0.15", linewidth=linewidth, zorder=20, transform=ax.transData))
        lon_curr = lon_next
    lon_end, _, _ = geod.fwd(lon_start, lat_bar, 90, length_m)
    ax.add_patch(mpatches.Rectangle(
        (lon_start, lat_bar), (lon_end - lon_start), h,
        fill=False, edgecolor="0.15", linewidth=linewidth, zorder=21, transform=ax.transData))
    lon_mid, _, _ = geod.fwd(lon_start, lat_bar, 90, length_m / 2)
    for x_txt, txt in ((lon_start, "0"), (lon_mid, f"{length_km // 2:.0f}"), (lon_end, f"{length_km:.0f} km")):
        ax.text(x_txt, lat_bar + h * 1.5, txt, ha="center", va="bottom",
                fontsize=7.5, color="0.15", zorder=22)


def clip_raster_exact(da, geom):
    """
    Recorte en 2 pasos: clip_box al bounding box y luego geometry_mask
    (NaN exacto fuera del poligono). Devuelve float32.
    """
    minx, miny, maxx, maxy = gpd.GeoSeries([geom], crs="EPSG:4326").total_bounds
    da_bb = da.astype("float32").rio.clip_box(minx=minx, miny=miny, maxx=maxx, maxy=maxy)
    mask = geometry_mask(
        [mapping(geom)],
        out_shape=(da_bb.sizes["y"], da_bb.sizes["x"]),
        transform=da_bb.rio.transform(),
        invert=True, all_touched=False,
    )
    return da_bb.where(mask)


def write_cog_north_up(da, path):
    """Escribe GeoTIFF float32 LZW con filas norte->sur."""
    if da.y.values[0] < da.y.values[-1]:
        da = da.isel(y=slice(None, None, -1)).rio.write_transform(inplace=False)
    da.rio.to_raster(path, driver="GTiff", dtype="float32", compress="LZW", nodata=float("nan"))


def plot_boundary(ax, gdf):
    gdf.boundary.plot(ax=ax, linewidth=2.2, color="white", zorder=5)
    gdf.boundary.plot(ax=ax, linewidth=1.1, color="0.15", zorder=6,
                      capstyle="round", joinstyle="round")


FUENTE = ("Fuente: Generado a partir de CHIRPS y AgERA5. "
          "L\u00edmite: Divisi\u00f3n departamental, Natural Earth a 10m.")

# Colormap cambio porcentual
cmap_div = LinearSegmentedColormap.from_list(
    "cambio_pct",
    [(0.00, "#db1b1b"), (0.25, "#f97c2b"), (0.50, "#ecfc99"), (0.75, "#2fb47c"), (1.00, "#1ea0c4")],
    N=256,
)
VMIN, VMAX = -100, 100
norm_div = TwoSlopeNorm(vmin=VMIN, vcenter=0, vmax=VMAX)   # ancla el 0 al centro del colormap
ticks_cb = list(range(VMIN, VMAX + 1, 20))

# Riesgo climatico (categorias SPEI): 1=Verano (seco), 2=Normal, 3=Invierno (humedo)
SPEI_CATS = np.array([1, 2, 3])
SPEI_BOUNDS = np.array([0.5, 1.5, 2.5, 3.5])
SPEI_LABELS = {1: "Verano\n(seco)", 2: "Normal", 3: "Invierno\n(H\u00famedo)"}
cmap_spei = ListedColormap(["#db1b1b", "#ecfc99", "#1ea0c4"])
norm_spei = BoundaryNorm(SPEI_BOUNDS, cmap_spei.N)

da_chg_base = cambio_pct.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=False).rio.write_crs("EPSG:4326", inplace=False)
da_spei_base = spei_mode.astype("float32").rio.write_crs("EPSG:4326", inplace=False)

# ============================================================
# 9. Productos por departamento
# ============================================================
generados = []   # (slug, tif_cambio, tif_spei)
for dept in DEPARTMENTS:
    gdf = depts[dept]
    slug = DEPT_SLUGS[dept]
    geom = union_geom(gdf)
    print(f"\nProcesando {dept}...")

    # ---- Mapa de diagnostico QC (con titulo: es interno) ----
    fig, ax = plt.subplots(figsize=(7, 4))
    minx, miny, maxx, maxy = gdf.total_bounds
    changed.astype("int8").rio.write_crs("EPSG:4326").rio.clip_box(
        minx=minx, miny=miny, maxx=maxx, maxy=maxy
    ).plot(ax=ax, cmap="Reds", add_colorbar=False)
    gdf.boundary.plot(ax=ax, linewidth=1.3, color="black", zorder=5)
    ax.set_title(f"Pixeles modificados por QC - {dept} ({MONTH_STR}/{YEAR})")
    ax.set_xlabel("Longitud"); ax.set_ylabel("Latitud")
    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, f"qc_pixels_{PERIOD_TAG}_{slug}.png"), dpi=150)
    plt.close(fig)

    # ---- Recortes ----
    da_chg_clip = clip_raster_exact(da_chg_base, geom)
    da_spei_clip = clip_raster_exact(da_spei_base, geom)

    # ---- GeoTIFFs ----
    tif_chg = os.path.join(OUTPUT_DIR, f"cambio_pct_{PERIOD_TAG}_{slug}.tif")
    tif_spei = os.path.join(OUTPUT_DIR, f"spei_moda_{PERIOD_TAG}_{slug}.tif")
    write_cog_north_up(da_chg_clip, tif_chg)
    write_cog_north_up(da_spei_clip, tif_spei)
    generados.append((slug, tif_chg, tif_spei))

    # ---- Posicion de norte y escala (relativas al extent) ----
    x0_d, x1_d = float(da_chg_clip.x.min()), float(da_chg_clip.x.max())
    y0_d, y1_d = float(da_chg_clip.y.min()), float(da_chg_clip.y.max())
    dx, dy = x1_d - x0_d, y1_d - y0_d
    LON_SCALE, LAT_SCALE = x0_d + 0.03 * dx, y0_d + 0.04 * dy
    LON_NORTH, LAT_NORTH = x0_d + 0.09 * dx, y0_d + 0.33 * dy
    SIZE_NORTH = 0.18 * dy

    # ---- Mapa cambio porcentual (sin titulo) ----
    fig, ax = plt.subplots(figsize=(9, 6))
    im = da_chg_clip.plot(
        ax=ax, cmap=cmap_div, norm=norm_div, add_colorbar=True,
        cbar_kwargs=dict(shrink=0.85, pad=0.02, extend="both", ticks=ticks_cb,
                         label="Cambio porcentual (%)"),
    )
    im.colorbar.set_ticks(ticks_cb)
    im.colorbar.set_ticklabels([str(t) for t in ticks_cb])
    plot_boundary(ax, gdf)
    add_geodesic_scalebar(ax, lon_start=LON_SCALE, lat_bar=LAT_SCALE, length_km=200, height_deg=0.10)
    add_north_arrow(ax, lon=LON_NORTH, lat=LAT_NORTH, size_deg=SIZE_NORTH)
    ax.set_title("")
    ax.set_xlabel("Longitud (\u00b0)"); ax.set_ylabel("Latitud (\u00b0)")
    ax.text(0.01, 0.01, FUENTE, transform=ax.transAxes, fontsize=5.5, va="bottom")
    plt.tight_layout()
    png_chg = os.path.join(OUTPUT_DIR, f"mapa_cambio_pct_{PERIOD_TAG}_{slug}.png")
    plt.savefig(png_chg, dpi=400, bbox_inches="tight")
    plt.close(fig)

    # ---- Mapa de riesgo climatico (SPEI modal, sin titulo) ----
    fig, ax = plt.subplots(figsize=(9, 6))
    im = da_spei_clip.plot(
        ax=ax, cmap=cmap_spei, norm=norm_spei, add_colorbar=True,
        cbar_kwargs=dict(ticks=SPEI_CATS, shrink=0.85, pad=0.02, extend="neither", extendrect=True),
    )
    im.colorbar.set_ticklabels([SPEI_LABELS[i] for i in SPEI_CATS])
    im.colorbar.set_label("")
    plot_boundary(ax, gdf)
    add_geodesic_scalebar(ax, lon_start=LON_SCALE, lat_bar=LAT_SCALE, length_km=200, height_deg=0.10)
    add_north_arrow(ax, lon=LON_NORTH, lat=LAT_NORTH, size_deg=SIZE_NORTH)
    ax.set_title("")
    ax.set_xlabel("Longitud (\u00b0)"); ax.set_ylabel("Latitud (\u00b0)")
    ax.text(0.01, 0.01, FUENTE, transform=ax.transAxes, fontsize=5.5, va="bottom")
    plt.tight_layout()
    png_riesgo = os.path.join(OUTPUT_DIR, f"mapa_riesgo_clima_{PERIOD_TAG}_{slug}.png")
    plt.savefig(png_riesgo, dpi=400, bbox_inches="tight")
    plt.close(fig)
    print(f"  OK: {os.path.basename(png_chg)} | {os.path.basename(png_riesgo)}")

print("\nTodos los departamentos procesados.")
for f in sorted(glob.glob(os.path.join(OUTPUT_DIR, f"*{PERIOD_TAG}*"))):
    print(f"  {os.path.basename(f):55s} {os.path.getsize(f) / 1024:8.1f} KB")

# ============================================================
# 10. Preparar granules para GeoServer (SOLO los de este periodo)
# ============================================================
file_prefix = (os.environ.get("GEO_FILE_PREFIX", "").strip()
               or os.environ.get("GEO_WORKSPACE", "").strip()
               or "pma")
_tag = os.environ.get("PMA_GRANULE_TAG", "").strip()
_tag_suffix = f"_{_tag}" if _tag else ""   # con guiones, para no confundir la deteccion de tiempo YYYYMM
timestamp = f"{YEAR}{MONTH_STR}"

base_path_geoserver = os.path.join(OUTPUT_DIR, "geoserver")
shutil.rmtree(base_path_geoserver, ignore_errors=True)   # nunca arrastrar granules de otros meses

VARIABLES = {"pct_change": 1, "spei": 2}   # var -> posicion del tif en la tupla de `generados`
granules = []   # (var, slug, carpeta)
for slug, tif_chg, tif_spei in generados:
    for var, pos in VARIABLES.items():
        src = (slug, tif_chg, tif_spei)[pos]
        dst_dir = os.path.join(base_path_geoserver, var, slug)
        os.makedirs(dst_dir, exist_ok=True)
        new_name = f"{file_prefix}_st_{slug}_{var}_{timestamp}{_tag_suffix}.tif"
        shutil.copyfile(src, os.path.join(dst_dir, new_name))
        granules.append((var, slug, dst_dir))
        print(f"  granule: {var}/{slug}/{new_name}")

esperados = len(DEPARTMENTS) * len(VARIABLES)
if len(granules) != esperados:
    raise SystemExit(f"ERROR: se esperaban {esperados} granules y se prepararon {len(granules)}.")

# ============================================================
# 11. Publicacion en GeoServer
# ============================================================
if os.environ.get("PMA_SKIP_PUBLISH", "").strip().lower() in ("1", "true", "yes", "si"):
    print(">>> PMA_SKIP_PUBLISH activo: productos generados; se OMITE la publicacion en GeoServer.")
    sys.exit(0)

from geoserver.catalog import Catalog  # noqa: E402


def load_credentials():
    """GEO_USER/GEO_PWD desde el entorno; si no, desde env.txt (raiz del repo o src/)."""
    keys = ["GEO_USER", "GEO_PWD"]
    cfg = {k: os.environ.get(k, "").strip() for k in keys}
    if all(cfg.values()):
        return cfg
    for path in (os.path.join(REPO_DIR, "env.txt"), os.path.join(REPO_DIR, "src", "env.txt")):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() in keys and not cfg.get(k.strip()):
                            cfg[k.strip()] = v.strip()
            break
    faltan = [k for k in keys if not cfg.get(k)]
    if faltan:
        raise SystemExit(f"ERROR: faltan credenciales {faltan}. Definelas como variables de entorno "
                         f"o copia env.example.txt a env.txt.")
    return cfg


cred = load_credentials()
gs_url = os.environ.get("GEO_URL", "").strip()
ws_name = os.environ.get("GEO_WORKSPACE", "").strip()
store_prefix = os.environ.get("GEO_STORE_PREFIX", "").strip() or ws_name
if not gs_url or not ws_name:
    raise SystemExit("ERROR: faltan GEO_URL y/o GEO_WORKSPACE (GitHub Secrets o export en local).")

print("\n" + "=" * 75)
print(f"Conectando a GeoServer | workspace: {ws_name}")
print("=" * 75)

catalog = Catalog(gs_url, username=cred["GEO_USER"], password=cred["GEO_PWD"])
workspace = catalog.get_workspace(ws_name)
if not workspace:
    raise SystemExit(f"ERROR: workspace no encontrado: {ws_name}")

# Verificar TODOS los stores antes de subir nada: o se publica el mes completo o nada.
stores = {}
faltantes = []
for var, slug, _ in granules:
    store_name = f"{store_prefix}_st_{slug}_{var}"
    try:
        stores[(var, slug)] = catalog.get_store(store_name, workspace)
    except Exception:
        faltantes.append(store_name)
if faltantes:
    raise SystemExit(f"ERROR: stores inexistentes en GeoServer: {faltantes}. No se publico nada.")

zip_root = os.path.join(base_path_geoserver, "zip_mosaic")
os.makedirs(zip_root, exist_ok=True)
publicados = 0
for var, slug, folder in granules:
    tifs = sorted(glob.glob(os.path.join(folder, "*.tif")))
    if len(tifs) != 1:
        raise SystemExit(f"ERROR: se esperaba 1 tif en {folder} y hay {len(tifs)}.")
    zip_path = os.path.join(zip_root, f"{var}_{slug}.zip")
    with ZipFile(zip_path, "w") as z:
        z.write(tifs[0], os.path.basename(tifs[0]))
    store = stores[(var, slug)]
    catalog.harvest_uploadgranule(zip_path, store)   # lanza UploadError si GeoServer no responde 202
    publicados += 1
    print(f"  OK harvest {os.path.basename(tifs[0])} -> {store.name}")

if publicados != esperados:
    raise SystemExit(f"ERROR: se publicaron {publicados} de {esperados} granules.")
print(f"\nPublicacion completa: {publicados}/{esperados} granules.")
