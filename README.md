# ¿A quién alcanza el fuego? Incendios en el Perú al 21 de octubre de 2026

**Autor:** Rodrigo Norabuena. Trabajo del curso Python Intermedio, Diplomatura de Especialización en Ciencia de Datos para las Ciencias Sociales y la Gestión Pública 2026, Pontificia Universidad Católica del Perú (PUCP). 21 de octubre de 2026.

**Dashboard:** [¿A quién alcanza el fuego?](https://claude.ai/artifact/L7bWfwPE94wdHdTsXhLNcy), un relato con mapa que cruza los focos de calor con la población y las edificaciones.

## Pregunta

¿Cuántas personas y edificaciones están a menos de X km de un incendio activo al 21 de octubre de 2026? ¿En qué distritos? ¿Cuánto cambia esa cifra según el radio (1, 2, 5 o 10 km) y según la fuente (WorldPop frente a Google Open Buildings)?

## Datos y fechas

| Dato | Versión y fecha | Fuente |
|---|---|---|
| Focos de calor VIIRS 375 m (S-NPP, NOAA-20, NOAA-21) | NRT, 1 jul – 21 oct 2026. Corte: **2026-10-22 01:52 UTC (21 oct, 20:52 hora de Lima)** | NASA FIRMS, Area API: `https://firms.modaps.eosdis.nasa.gov/api/area/` |
| Límites distritales (1873 distritos, INEI) | Shapefile `DISTRITOS.shp` del curso | Hugging Face, dataset `aquiro1994/ds-python-up`, `_data/Folium/` |
| Población (raster) | WorldPop R2025A, año 2026, 100 m, *constrained* | `https://data.worldpop.org/GIS/Population/Global_2015_2030/R2025A/2026/PER/v1/100m/constrained/per_pop_2026_CN_100m_R2025A_v1.tif` |
| Edificaciones | Google Open Buildings v3 (polígonos con confianza ≥ 0,75), leído del espejo GeoParquet de VIDA | `https://data.source.coop/vida/google-microsoft-open-buildings/geoparquet/by_country_s2/country_iso=PER/` |
| Relieve | AWS Terrain Tiles (Terrarium), zoom 8 | `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png` |

Los focos se bajan en bloques de 5 días por satélite (69 consultas). La API devuelve, para `/{N}/{FECHA}`, los datos de FECHA a FECHA + N − 1 (lo verificamos con `acq_date`), así que los bloques no se solapan. Además, se eliminan duplicados.

Para Open Buildings se midieron primero los tamaños. Los archivos CSV por celda S2 nivel 4 pesan de 100 a 190 MB cada uno. El espejo GeoParquet trae un `bbox` por fila con estadísticas por grupo de filas, y DuckDB solo lee los grupos que tocan cada zona. Por eso se eligió esa ruta (solo filas `bf_source = 'google'`).

## Método

1. **Limpieza de focos** (de 199 399 detecciones en el recuadro):
   - fuera del Perú: 64 405;
   - confianza baja (`confidence = l`): 10 413;
   - a ≤ 3 km de los volcanes Sabancaya, Ubinas, Misti, Ticsani, Huaynaputina y Yucamane: 53;
   - píxeles persistentes, es decir, la misma celda de ~375 m detectada en ≥ 10 días distintos a lo largo de ≥ 60 días (quemas de gas, fundiciones): 948 focos en 35 celdas.
   - Quedan **123 580 focos** en la temporada.
2. **Incendios:** se unen los focos a ≤ 1 km y ≤ 48 h (BallTree haversine y componentes conexas). Un incendio está **activo** si tiene un foco en las 24 h anteriores al corte. La huella es el número de píxeles únicos de ~375 m × 14,06 ha (no es área quemada).
3. **Filtro urbano y de fuentes fijas.** Un foco dentro de una ciudad suele ser industria, un botadero o calor urbano, no vegetación. Un incendio activo se marca y se excluye de la exposición, del ranking de distritos y de la prioridad si cumple alguna de estas condiciones:
   - **Urbano:** ≥ 5000 personas a 1 km de sus focos (≈ 1600 hab/km² en un círculo de 3,14 km²), o un foco en una celda de ~1 km con ≥ 1500 hab/km². Este es el umbral de "centro urbano" del *Degree of Urbanisation* (Eurostat / Comisión de Estadística de la ONU, 2020).
   - **Fuente fija o recurrente:** un incendio pequeño (≤ 2 focos, sin frente que avance) en un lugar que ya tuvo detecciones a ≤ 500 m al menos 30 días antes. La vegetación rara vez vuelve a arder en el mismo sitio un mes después; las plantas industriales, los botaderos y los hornos, sí.
   - Resultado: de 154 incendios activos se marcan 32 (1 urbano y 31 de fuente fija). Quedan **122 incendios activos de vegetación**.
4. **Exposición** para r = 1, 2, 5 y 10 km:
   - se toma un buffer de los focos de las últimas 48 h de cada incendio activo no marcado, en la proyección de área igual ESRI:102033;
   - los buffers se disuelven para no contar dos veces y se intersecan con los distritos;
   - personas = suma de WorldPop 2026 a 100 m dentro de la zona; edificaciones = número de polígonos de Open Buildings dentro de la zona.
   - Se usan solo las últimas 48 h porque muchos grupos de focos siguen "vivos" durante meses encadenando detecciones; usar toda su historia inflaría la exposición.
5. **Triangulación:** edificaciones × 3,5 personas por vivienda. Es una cifra de referencia del promedio de personas por hogar del Censo 2017 del INEI, pendiente de verificar. Se compara con WorldPop.
6. **Prioridad (0–100)** por incendio activo:
   `prioridad = 100 × (0,35·n(log(1 + personas a 5 km)) + 0,25·n(huella en ha) + 0,20·n(FRP total en 24 h) + 0,20·n(focos en 24 h))`, donde n() es la normalización mín–máx entre los incendios activos no marcados.
7. **Caso de estudio:** el incendio activo de vegetación (≥ 3 focos, huella ≥ 50 ha, sin marca) con más personas a 5 km. Para ese incendio se calcula, con `cKDTree` en metros, la distancia de cada edificación al foco más cercano.

## 3 hallazgos con números

1. **El radio cambia la respuesta.** Al corte había 122 incendios activos de vegetación. A 5 km de sus focos viven unas **206 740 personas** (WorldPop 2026). Con 1 km son 5838 y con 10 km, 938 478: el radio elegido multiplica la cifra por 161. Aparte quedan 32 incendios marcados como urbanos o de fuente fija, que habrían sumado 187 759 personas más a 5 km.
2. **Nauta concentra la exposición.** El distrito de Nauta (Loreto) reúne **28 841 personas** a 5 km de un incendio activo, el 14 % del total nacional. Los 10 primeros distritos suman el 68 %.
3. **Dos fuentes que no coinciden del todo.** Open Buildings cuenta **118 358 edificaciones** a 5 km. Con 3,5 personas por vivienda serían 414 253 personas, frente a 206 740 según WorldPop, es decir, 1,7 personas por edificación: no toda edificación es una vivienda. En el caso de estudio (Puerto Bermúdez, Oxapampa, Pasco: 5 focos, 70 ha de huella, 4073 personas a 5 km), la edificación más cercana está a **484 m** de un foco.

Las cifras salen de `outputs/hallazgos.json` y de `web/data/summary.json`.

## Mapa

![Personas a 5 km de un incendio activo por distrito](outputs/mapa_exposicion.png)

## Limitaciones

- **Un foco no es un incendio.** Es un píxel más caliente que su entorno. Puede ser una quema agrícola, una industria o un botadero. El filtro urbano y de fuentes fijas es una regla aproximada: puede dejar falsos positivos (por ejemplo, focos aislados en el desierto costero cerca de Lima o de Pisco) y quitar algunas quemas reales de chacras que vuelven a arder.
- **Las nubes esconden el fuego.** Si un incendio está bajo nubes al paso del satélite, no se detecta, y en la selva la nubosidad es frecuente.
- **Píxeles de 375 m.** La posición tiene una incertidumbre de cientos de metros, y un incendio pequeño puede ocupar solo una parte del píxel.
- **La huella no es área quemada.** Es el número de píxeles detectados × 14,06 ha.
- **WorldPop es un modelo.** Reparte la población proyectada según imágenes y covariables, no es un censo.
- **Open Buildings** depende de la detección automática y del umbral de confianza (≥ 0,75). Cuenta edificaciones, no viviendas, y las imágenes de base pueden tener años de antigüedad.
- **Un radio no es una zona de evacuación.** La distancia en línea recta no considera el viento, la pendiente, los ríos ni los caminos.
- **Hora de corte.** Las cifras corresponden al 21 de octubre de 2026 a las 20:52 (hora de Lima). Con cada paso de satélite cambian.

## Cómo reproducir

```bash
uv sync
cp .env.example .env    # y pegar la clave: FIRMS_MAP_KEY=...
uv run python -m pipeline.run_all
```

Las descargas quedan en `data/raw/` (no se versiona) y cada paso reutiliza lo que ya existe. Para un corte nuevo, borrar `data/raw/firms/` y `data/interim/`.

## Dashboard

[¿A quién alcanza el fuego?](https://claude.ai/artifact/L7bWfwPE94wdHdTsXhLNcy) cuenta el hallazgo en siete pasos: la temporada completa, el fuego de hoy, dónde vive la gente, los distritos donde se cruzan, el efecto del radio y las casas junto a un incendio. Después viene un explorador por incendio y distrito, y una tabla que se puede copiar en CSV.

La página está en `web/index.html` y lee los archivos de `web/data/` y `web/assets/`, que genera el pipeline. Para verla en local:

```bash
uv run python -m http.server 8000 --directory web
```

Usa deck.gl 9.0.38 y topojson-client 3.1.0 desde CDN.

## Cómo citar

Si usas este trabajo, cítalo así (APA 7):

> Norabuena, R. (2026). *¿A quién alcanza el fuego? Incendios en el Perú al 21 de octubre de 2026* [Software y conjunto de datos]. GitHub. https://github.com/rodnm/mapa-incendios-peru-2026

```bibtex
@software{norabuena_2026_incendios,
  author = {Norabuena, Rodrigo},
  title  = {¿A quién alcanza el fuego? Incendios en el Perú al 21 de octubre de 2026},
  year   = {2026},
  url    = {https://github.com/rodnm/mapa-incendios-peru-2026},
  note   = {Curso Python Intermedio, Diplomatura de Especialización en Ciencia de Datos para las Ciencias Sociales y la Gestión Pública 2026, Pontificia Universidad Católica del Perú}
}
```

GitHub también ofrece la cita con el botón **Cite this repository**, que lee el archivo `CITATION.cff`.

## Licencia

- **Código** (`pipeline/`, `web/index.html`): [MIT](LICENSE).
- **Textos, figuras y datos derivados** (este README, `outputs/`, `web/data/`, `web/assets/`): [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.es). Puedes reutilizarlos citando la autoría.
- **Datos de terceros:** mantienen su propia licencia. WorldPop es CC BY 4.0, Google Open Buildings es CC BY 4.0 u ODbL y los focos de NASA FIRMS son datos abiertos de la NASA.

## Monitor en vivo

**Página:** https://rodnm.github.io/mapa-incendios-peru-2026/

El monitor repite el cálculo de este trabajo con los datos más recientes de NASA FIRMS. Muestra los focos de las últimas 24 horas y de los últimos 7 días, los incendios activos de vegetación ordenados por prioridad, las personas y edificaciones a 1, 2, 5 y 10 km, los distritos más expuestos y la evolución de las cifras entre actualizaciones. La historia del 21 de octubre se publica en el mismo sitio, en [`historia/`](https://rodnm.github.io/mapa-incendios-peru-2026/historia/).

### Frecuencia

Un flujo de GitHub Actions (`.github/workflows/live.yml`) se ejecuta cada 3 horas, a los 30 minutos de las 00, 03, 06, …, 21 h UTC (19:30, 22:30, 01:30, … en hora de Lima), y también se puede lanzar a mano. Los satélites pasan sobre el Perú cerca de la 01:30 y de las 13:30 (hora de Lima), y FIRMS publica sus datos unas 3 horas después, así que la mayoría de las actualizaciones con datos nuevos llegan después de esos pasos. La página muestra la hora de los datos y avisa si tienen más de 12 horas.

### Diferencias con la historia v1.0.0

| | Historia (v1.0.0) | Monitor en vivo |
|---|---|---|
| Corte | Fijo: 2026-10-22 01:52 UTC (21 oct, 20:52 hora de Lima) | La hora de cada actualización |
| Periodo de focos | Temporada desde el 1 de julio de 2026 | Ventana móvil de 120 días (`LIVE_WINDOW_DAYS`) |
| Método | Limpieza, filtros urbano y de fuentes fijas, exposición a 1, 2, 5 y 10 km, prioridad | El mismo, con el mismo código |
| Caso de estudio | Sí | No |
| Archivos | `web/`, `outputs/` (congelados, etiqueta `v1.0.0`) | `build/live/`, publicado en GitHub Pages; no se versiona |

Los archivos del monitor siguen el mismo formato que `web/data/` y agregan a `summary.json` los campos `mode`, `generated_at`, `data_through`, `window_start` y `window_days`. Además incluyen `history.csv`, con una fila por actualización.

### Cómo funciona

1. **Descarga incremental.** Los focos se guardan por producto y por día en `data/raw/firms_live/`. Cada ejecución baja solo los días que faltan, más el día en curso y los 2 anteriores, porque FIRMS corrige los datos casi en tiempo real (NRT). Las consultas se reintentan con espera exponencial ante errores HTTP y límites de uso.
2. **Cachés.** Las edificaciones de Open Buildings se guardan en celdas de 0,25° (`data/raw/open_buildings/tiles_*`): la zona de cada incendio se redondea a esa grilla y cada celda se descarga una sola vez. WorldPop y los límites distritales se bajan una vez. El relieve y la capa de población del mapa se copian de `web/assets/`.
3. **Control de calidad** (`pipeline/checks.py`). Antes de publicar se verifica que FIRMS haya entregado focos de las últimas 48 horas, que las cifras estén en rangos plausibles, que estén todos los archivos con JSON válido y que el total no pase de 6 MB. La salida se escribe en una carpeta temporal y reemplaza a la anterior solo si pasa todos los controles. Si algo falla, el proceso termina con error, no se publica nada y el sitio sigue mostrando la última versión buena.
4. **Historial.** Al empezar, cada ejecución lee el `history.csv` publicado (`<LIVE_BASE_URL>data/history.csv`), le agrega una fila y lo limita a 2000 filas. Si el sitio no responde, usa la copia de `data/raw/live_state/`, que el flujo guarda en la caché de GitHub Actions. Así el historial se conserva sin commits en `main`. La variable de entorno `LIVE_BASE_URL` cambia la dirección del sitio; vacía, desactiva la lectura remota.

### Cómo ejecutarlo en local

```bash
uv sync
uv run python -m pipeline.run_all --mode live --out build/live   # usa FIRMS_MAP_KEY de .env
uv run python scripts/build_site.py                              # arma site/
uv run python -m http.server 8000 --bind 127.0.0.1 --directory site
```

La primera ejecución descarga unos 120 días de focos y las celdas de Open Buildings que hagan falta (unos 8 minutos); las siguientes reutilizan las cachés. `uv run python -m pipeline.checks build/live` vuelve a aplicar el control de calidad a una salida existente.

El sitio queda en `site/`: el monitor en `site/index.html`, sus datos en `site/data/` y la historia congelada en `site/historia/`. Las carpetas `build/` y `site/` no se versionan.

La versión congelada se protege así: `uv run python -m pipeline.run_all` (modo `snapshot`, el predeterminado) usa siempre el corte fijo y no escribe en `web/` ni en `outputs/` salvo que se agregue `--force`. Para comprobar que la versión 1.0.0 se reproduce sin tocar esas carpetas: `uv run python -m pipeline.run_all --mode snapshot --out build/snapshot`.

### Publicación

El flujo necesita el secreto `FIRMS_MAP_KEY` del repositorio y GitHub Pages configurado con la fuente **GitHub Actions**. Si el pipeline o el control de calidad fallan, el flujo no despliega.

### Limitaciones del monitor

- **Heurísticas automáticas.** Los filtros urbano y de fuentes fijas, la unión de focos en incendios y la prioridad se aplican sin revisión humana. Un foco aislado puede ser una quema agrícola, una industria o un error, y una quema real puede quedar excluida.
- **Latencia de los datos NRT.** Los focos llegan unas 3 horas después de cada paso del satélite, y FIRMS los corrige durante los días siguientes. Una cifra puede cambiar en la siguiente actualización aunque no haya fuego nuevo.
- **Retrasos del cron.** GitHub puede demorar las ejecuciones programadas varios minutos, o más en horas de mucha demanda, y desactiva los flujos programados de repositorios sin actividad durante 60 días.
- **Las mismas limitaciones de la historia:** nubes, píxeles de 375 m, WorldPop como modelo, edificaciones que no son viviendas y radios que no son zonas de evacuación. El monitor no reemplaza los reportes del COEN, INDECI, SERFOR ni SERNANP.
