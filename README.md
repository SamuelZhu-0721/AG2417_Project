# Stockholm Walkability Explorer

A web GIS that scores walkability for any point in Stockholm Municipality. It builds a slope-aware walking-time area (isochrone), scores street form and access to services, lets users weight their priorities, compares locations and recalculates scores around user-drawn hazard areas.

Built for AG2417 Web and Mobile GIS at KTH.

## How it works

1. The user clicks the map and sets a walking time and speed.
2. The API snaps the click to the nearest network node and calls the PostGIS/pgRouting function `walkability.analyze`.
3. The function routes over slope-adjusted edge costs and returns raw factors, the isochrone and the reachable streets.
4. Python converts the raw factors to z-scores against a reference sample of Stockholm nodes and applies the user's weights.
5. The browser draws the result and adds it to the comparison dashboard.

### Scores

| Part | Factors |
| --- | --- |
| Accessibility (weighted by the user) | amenity count, transit stop count, green-space share of the reach area |
| Morphology | intersection density, integration (inverse mean depth), inverse mean block length |
| Final score | `0.7 x accessibility + 0.3 x morphology` |

Scores are relative. Zero means typical for Stockholm Municipality. Transit is a count of stops. Service frequency is not used.

### Slope

- Node elevations are sampled from a local 30 m raster (`data/output_hh.tif`) with bilinear interpolation.
- Nodes outside the raster get no elevation. They are never filled with zero.
- Edge grade is the elevation change divided by length. Grades are limited to +/-25%.
- Edge cost uses Tobler's hiking function, `v(g) = 6 * exp(-3.5 * |g + 0.05|)` km/h. Cost is `length * v(0) / v(g)`, so the walking speed chosen in the interface keeps its meaning on flat ground.
- Edges are directed, so reach is different uphill and downhill.

### Hazards

Users draw hazard areas on the map. The browser sends them as GeoJSON polygons. The database removes every edge that intersects a polygon and recomputes reach and scores. Requests with hazards bypass the route cache.

## Requirements

- Python 3.11 or later
- Docker (for the bundled PostgreSQL with PostGIS and pgRouting) or your own PostgreSQL with both extensions
- A Mapbox public token
- An elevation raster at `backend/data/output_hh.tif` (see Data)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt

docker compose up -d db              # PostGIS + pgRouting on 127.0.0.1:5435
cp backend/_env backend/.env         # then edit DATABASE_URL if needed
```

Set your Mapbox token in `frontend/index.html`. Restrict the token to your own domain.

### Prepare the data (once)

```bash
python backend/data_prep.py
```

This creates the schema, downloads and caches OpenStreetMap data, samples elevation, imports everything in one transaction and builds the reference scores.

| Option | Effect |
| --- | --- |
| `--reference-points N` | Size of the reference sample (default 2000) |
| `--refresh` | Re-download all source data |
| `--data-dir PATH` | Use another data directory |
| `--skip-database` | Prepare files only |

Changing the raster, the slope model or the scoring SQL changes the stored data. Re-run `data_prep.py` afterwards. The cached network re-samples elevation automatically when the raster changes.

### Run

```bash
python run.py
```

| Service | Address |
| --- | --- |
| Frontend | http://localhost:3000 |
| API | http://localhost:8000 |
| API documentation | http://localhost:8000/docs |

Press Ctrl+C to stop both servers.

## Project layout

```
run.py                     starts the API and the static frontend
compose.yaml               PostgreSQL + PostGIS + pgRouting
frontend/index.html        Mapbox GL JS client and dashboard
backend/
  api.py                   FastAPI routes and request validation
  database.py              pool, migrations, import, scoring, caching
  scoring.py               z-scores, slope cost, morphology and accessibility
  data_prep.py             downloads, elevation sampling, import
  sql/001..003_*.sql       schema and the analyze() function
  test_scoring.py          unit tests (no database needed)
  test_database.py         database and API tests (isolated temporary databases)
  DATABASE.md              database design notes
```

## API

| Method and path | Purpose |
| --- | --- |
| `POST /score` | Score a point. Body: `lat`, `lon`, `weights`, `trip_minutes`, `walking_speed_kmh`, `exclusion_polygons` |
| `GET /layers/amenities` | Amenity points |
| `GET /layers/greenspace` | Green-space polygons |
| `GET /layers/transit` | Transit stops |
| `GET /layers/reference-points` | Reference sample points |
| `GET /layers/boundary` | Study-area polygon |
| `GET /health` | Database status and dataset metadata |

Requests are validated. Points outside the municipal boundary are rejected with a 400 response.

## Data

| Data | Source |
| --- | --- |
| Boundary, walking network, amenities, green space, transit stops | OpenStreetMap contributors (ODbL), via OSMnx |
| Elevation | `output_hh.tif`, 30 m raster [state the original source and licence] |

All geometry is stored in EPSG:32634 (UTM zone 34N).

## Tests

```bash
cd backend
python -m unittest test_scoring -v       # fast, no database
python -m unittest test_database -v      # needs PostgreSQL with PostGIS and pgRouting
```

`test_database.py` creates and drops its own temporary databases. Check its header for the connection variable it reads. It needs `httpx` for the API client.

What the tests cover:

- z-scores, final score blend and weight-driven ranking
- intersection counting on two-way streets
- Tobler cost ratios, monotonic uphill cost, and uphill versus downhill reach
- bilinear sampling, nodata handling and grade clipping
- elevation summary
- SQL against Python parity for reach and factors
- elevation storage, hazard exclusion, route caching and request validation

## Troubleshooting

| Problem | Check |
| --- | --- |
| Slopes are all flat or green | `backend/data/output_hh.tif` exists. The import prints `elevation coverage`. Re-run `data_prep.py` |
| Low elevation coverage | The raster does not cover all of Stockholm Municipality |
| Port already in use | Stop the process on port 8000 or 3000 |
| API fails at start | Docker is running and `DATABASE_URL` in `backend/.env` is correct |
| Scores look odd or unstable | Re-import with a larger `--reference-points` |
| Map is blank | Mapbox token missing or restricted to another domain |

## Known limitations

- The 30 m raster still smooths short steep streets.
- Transit counts stops and ignores frequency.
- Scores are relative to the reference sample, not absolute.
- Morphology is a sum of three z-scores and accessibility a weighted mean, so the parts have different spreads.
- The reach polygon is a convex hull and can include water.
- Hazards are binary closures and are stored in the browser only.
- The API has no authentication or rate limiting and allows any origin. Do not expose it publicly as it stands.

## Licence and credits

[Add your licence.] Map data from OpenStreetMap contributors. Basemap and map library by Mapbox.