# Database implementation README

PostgreSQL is the source of truth after preparation. **PostGIS** stores geometry
and performs spatial queries; **pgRouting** searches the directed walking
network. Python downloads and imports data, handles HTTP requests, and combines
the raw database factors into the scores shown by the frontend.

There is one application database named `walkability` and an application schema
also named `walkability`. A database is the connection target; the schema groups
the application's tables and SQL function inside that database. The existing
`postgres` database is used only as the maintenance connection when creating a
missing application database.

## First-run pipeline

PostgreSQL is already expected to be running at `localhost:5435`, using
username `gis` and password `gis`. The application database is `walkability`.
From the repository root:

```bash
cd stockholm-walkability
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
python backend/data_prep.py
python run.py
```

Open **http://localhost:3000**. API health is at
**http://localhost:8000/health**, and API docs at **http://localhost:8000/docs**.
Ctrl+C in the launcher stops both servers. Preparation is a separate step;
`run.py` checks that the backend is healthy before starting the frontend.

On first setup, `data_prep.py`:

1. Writes `backend/.env` with the connection setting if it is missing,
   preserving any other configuration and respecting an existing `DATABASE_URL`.
2. Connects to the maintenance database `postgres`, creates `walkability` if
   absent, enables PostGIS and pgRouting, and applies application migrations.
3. Downloads the municipal boundary, walking network, amenities, green space
   and transit stops for missing source files. Existing caches are reused.
4. Imports those files into PostgreSQL and samples reference nodes. Geometry,
   routing and reference-factor calculations run in SQL.
5. Commits the dataset atomically. The backend then reads PostgreSQL for layers,
   reference factors, routing and spatial calculations.

Rerunning `python backend/data_prep.py` reuses a completed database without
requiring source files or network requests. If the database is missing but
local source files remain, preparation imports them without redownloading.
If both are missing, it downloads the source data automatically.

For separate terminals after preparation, activate the same environment and
run these commands from `stockholm-walkability/`:

```bash
# Terminal 1:
cd backend
python -m uvicorn api:app --reload --host 127.0.0.1 --port 8000
```

```bash
# Terminal 2:
python -m http.server 3000 --bind 127.0.0.1 --directory frontend
```

The frontend uses a public Mapbox token in `frontend/index.html`. Set it to your
own public token if the bundled token is unavailable.

## Which file does what?

Paths below are relative to the repository root. Source files are linked so
you can go directly from this guide to the implementation.

| File | Responsibility | When it runs |
| --- | --- | --- |
| [`stockholm-walkability/backend/data_prep.py`](data_prep.py) | Main preparation entry point. Initializes configuration/database, downloads or reuses OSM source files, then calls the database importer. Supports refresh and reimport. | Run first; rerun when updating the dataset. |
| [`stockholm-walkability/backend/database.py`](database.py) | PostgreSQL access layer. Creates the database, runs migrations, imports with `COPY`, builds SQL reference factors, manages the API connection pool, serves layers and coordinates scoring/cache access. | Called by preparation and by the running API. |
| [`stockholm-walkability/backend/sql/001_initial.sql`](sql/001_initial.sql) | Enables PostGIS/pgRouting, creates application tables/indexes, and defines `walkability.analyze`, which performs routing and spatial factor calculations. | Applied once by the migration runner in `database.py`. Its function runs for reference generation and uncached scores. |
| [`stockholm-walkability/backend/api.py`](api.py) | FastAPI interface. Validates requests, opens/closes the database pool, delegates scores/layers to `Database`, and converts failures into HTTP responses. | Backend server on port 8000. |
| [`stockholm-walkability/backend/scoring.py`](scoring.py) | Factor dataclasses, z-score normalization, morphology/accessibility composites and the final blend. Also contains the slope-cost helper used during import and legacy NetworkX calculations. | During import and score normalization; legacy graph calculations run only in file-backed mode. |
| [`stockholm-walkability/run.py`](../run.py) | Starts the API, waits for health, starts the frontend server, and stops both on shutdown or failure. Checks that ports are available. | Run after successful preparation. |
| [`stockholm-walkability/frontend/index.html`](../frontend/index.html) | Mapbox interface. Sends score requests and fetches layers over HTTP, displays results, and keeps comparison points in browser local storage. | In the browser, served on port 3000. |
| `stockholm-walkability/backend/.env` | Local connection settings. Created by preparation if needed and ignored by Git. Shell environment values take precedence. | Read by preparation and the API. |
| [`stockholm-walkability/backend/.env.example`](.env.example) | Documents connection, pool, timeout and optional elevation settings. It is a template, not the active configuration file. | Read when configuring the project. |
| [`stockholm-walkability/backend/requirements.txt`](requirements.txt) | Python dependencies, including Psycopg/pooling, OSMnx, GeoPandas, FastAPI and Uvicorn. | Installed into the virtual environment before preparation. |
| [`compose.yaml`](../../compose.yaml) | Optional PostgreSQL container with PostGIS and pgRouting, a persistent volume and a health check. | Only if using it in place of the existing PostgreSQL server. |
| [`stockholm-walkability/backend/test_database.py`](test_database.py) | Database/pipeline tests using synthetic data and isolated temporary databases, plus request-validation unit tests. | During development; see the test commands below. |
| [`stockholm-walkability/backend/test_scoring.py`](test_scoring.py) | Earlier scoring/GTFS test suite. It currently references functions absent from this checkout. | Legacy tests; see the limitation below. |
| [`README.md`](../../README.md) | Project overview and quick-start commands, with a link to this implementation guide. | Documentation only. |

### Main functions to follow in the code

| File | Function or class | What it does |
| --- | --- | --- |
| `data_prep.py` | `main()` | Selects normal preparation, refresh, reimport or legacy file-only mode. Skips downloads/import when a completed database already exists. |
| `data_prep.py` | `get_boundary_polygon()`, `download_network()`, `download_amenities_and_greenspace()`, `download_transit()` | Fetch or reuse the municipal boundary, walking graph and feature layers. |
| `data_prep.py` | `_atomic_write()`, `_save_layer()` | Publish each completed source file atomically. |
| `database.py` | `bootstrap_configuration()` | Resolves `DATABASE_URL` and creates the local `.env` setting when missing. |
| `database.py` | `initialize_database()`, `migrate()` | Create the target database if absent; check extension availability and apply unapplied SQL files. |
| `database.py` | `import_dataset()` | Project/validate data, bulk-load tables, generate references, clear old routes and commit a new dataset revision. |
| `database.py` | `analyze()` | Small Python wrapper around `SELECT walkability.analyze(...)`. |
| `database.py` | `Database.open()`, `close()`, `health()` | Manage the runtime connection pool and report dataset health. |
| `database.py` | `Database.layer()` | Build GeoJSON from stored feature/reference geometries. |
| `database.py` | `Database.score()` | Check coverage, find the nearest node, retrieve/calculate a route, load references and call Python score normalization. |
| `001_initial.sql` | `walkability.analyze()` | Use pgRouting and PostGIS to return six raw factors, the isochrone geometry and reachable edge GeoJSON. |
| `api.py` | `load_data()`, `close_data()`, `score_point()`, `get_layer()` | Connect FastAPI lifecycle and endpoints to the database access layer. |
| `scoring.py` | `calculate_effective_edge_length()` | Convert physical length and signed grade to the stored slope-adjusted routing cost during import. |
| `scoring.py` | `morphology_composite()`, `accessibility_composite()`, `final_walkability_score()` | Normalize factors against references, apply weights and combine the scores. |

## How the pieces connect

```mermaid
flowchart TD
    OSM["OpenStreetMap / local source caches"] --> Prep["data_prep.py"]
    Prep --> Import["database.py: initialize, migrate, import"]
    SQL["sql/001_initial.sql: schema and analyze function"] --> Import
    Import --> DB[("PostgreSQL: walkability")]
    Browser["frontend/index.html"] --> API["api.py: HTTP endpoints"]
    API --> Client["database.py: Database"]
    Client <--> DB
    Client --> Scores["scoring.py: normalize and weight"]
    Scores --> Response["JSON scores and GeoJSON"]
    Response --> Browser
```

### Preparation: sources to database

`data_prep.py` orchestrates the pipeline; it delegates database work to
`database.py`. The importer reads the cached GraphML and feature files,
converts geometry to EPSG:32634, calculates slope-adjusted edge costs and
streams rows through PostgreSQL `COPY` into staging tables. SQL then creates
the final geometries and repairs invalid feature geometry.

The importer selects reference nodes using a fixed random seed and calls the
same `walkability.analyze` function used by live scoring. It stores the six raw
factors for each reference node at a 15-minute walking time. The normal pipeline
does not read reference factors from CSV or calculate them with NetworkX.

Finally, it records a dataset revision, import time, counts, coverage and a
cost bound used for routing optimization. All table replacement and reference
generation share one transaction. The existing dataset stays intact if import
fails; a successful replacement clears the previous route cache.

### Runtime: a map click to a score

1. The browser posts latitude, longitude, walking minutes and accessibility
   weights to `POST /score`.
2. `api.py` validates coordinates, time and weights, then calls `Database.score()`.
3. PostgreSQL checks the point against the imported boundary or network extent
   and finds the nearest walking node using the indexed PostGIS `<->` operator.
4. The access layer checks `route_cache` for that node, walking time and speed.
   Requests for the same key are serialized so they can reuse one calculation.
5. On a cache miss, `walkability.analyze()` runs pgRouting, builds the reach
   polygon and computes morphology/accessibility factors with SQL. Its raw
   result and geometries are stored in the cache.
6. The access layer reads `reference_scores`. `scoring.py` z-scores the current
   factors against the reference sample, applies fixed morphology weights and
   the user's accessibility weights, then calculates
   `0.7 * accessibility + 0.3 * morphology`.
7. FastAPI returns the three scores, six raw factors, isochrone and reachable
   edges. The browser draws the geometry and updates its panels.

The current factor is included with the reference sample when calculating each
z-score, following the existing Python scoring functions. Scores are relative
z-score composites and can be negative; they are not a percentage or a 0–100
rating. Reference factors remain based on 15-minute walks even when a live
request uses another walking time.

Changing only slider weights reuses the cached raw route. Choosing a different
walking time or nearest node creates a different cache entry. The cache is
stored in PostgreSQL, survives API restarts, and has no time-based expiry;
dataset replacement invalidates it.

### Map layer requests

The four `GET /layers/amenities`, `/layers/greenspace`, `/layers/transit` and
`/layers/reference-points` endpoints call `Database.layer()`. PostGIS transforms
stored geometry to longitude/latitude and serializes GeoJSON. Reference points
join `reference_scores.node` to `nodes.id` for their locations. The frontend
never connects to PostgreSQL or receives its credentials.

## Configuration and database requirements

`backend/.env.example` documents the local defaults. Override `DATABASE_URL`
in `backend/.env` or your shell to select another server/database. An existing
value is preserved. `DATABASE_MAINTENANCE_DB` can override the maintenance
connection's database name, which defaults to `postgres`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://gis:gis@localhost:5435/walkability` during preparation | Server credentials and target database. |
| `DATABASE_MAINTENANCE_DB` | `postgres` | Existing database to connect to before creating the target database. |
| `DATABASE_POOL_SIZE` | `8` | Maximum API connections per process. |
| `DATABASE_STATEMENT_TIMEOUT_MS` | `30000` | API SQL statement timeout in milliseconds; does not apply to imports. |
| `GOOGLE_ELEVATION_API_KEY` | Unset | Optional elevation lookup when downloading a new walking graph. |

Normal preparation writes `DATABASE_URL`, so the API starts in database mode.
If that setting is absent entirely, `api.py` retains its legacy local-file
mode. `--skip-database` prepares files only; it does not unset an existing
database configuration.

The preparation role needs `CREATEDB` for a missing database and permission to
create the schema/extensions. PostGIS and pgRouting server packages must be
installed for that PostgreSQL version; preparation enables their extensions
with `CREATE EXTENSION IF NOT EXISTS` and reports missing packages before
attempting a download. The current server's missing pgRouting 3.8 package has
been installed in its existing container. Recreating that container requires
an image that includes pgRouting or reinstalling its server package.

The repository's Compose service is an optional replacement for a server that
is not already running. It includes both spatial extensions and uses the same
local port/credentials. Do not start it alongside the existing server on port
5435. Its Docker volume persists across container restarts and `compose down`.

The API does not apply DDL on startup. A separate runtime role needs usage on
the application schema, select on application tables, insert on `route_cache`,
execute on `walkability.analyze`, and temporary-table permission. With
`DATABASE_URL` configured, startup uses PostgreSQL and fails clearly if it is
unavailable. The current `/score` and four `/layers/*` response shapes are
preserved. `/health` reports storage, routing, revision and imported row counts.

## Tables and import behavior

All application tables live in the `walkability` schema:

| Table | Key and important columns | Purpose |
| --- | --- | --- |
| `nodes` | `id` = OSM node ID, `geom` = metric point | Routing vertices and indexed nearest-node lookup. |
| `edges` | Generated `id`; `source`, `target`, `edge_key`, `length`, `grade`, `effective_length`, `highway`, `geom` | Directed arcs, preserving separate directions and parallel edges. Unique on `(source, target, edge_key)`. |
| `features` | Generated `id`; `layer`, `geom`, `properties` JSONB, `departures_per_hour` | Amenities, green space and transit in one table, distinguished by layer. |
| `reference_scores` | `node`; `intersection_density`, `integration`, `avg_block_length`, `amenity_count`, `transit_stop_score`, `green_space_ratio` | Raw SQL reference factors for score normalization. |
| `dataset` | Single `singleton = true` row; `revision`, `imported_at`, `metadata` JSONB, optional `boundary` | Identifies the committed dataset and holds counts, network bounds and the minimum routing cost per metre. |
| `route_cache` | Composite primary key `(node, trip_minutes, walking_speed_kmh)`; `result` JSONB, `created_at` | Raw factors and GeoJSON independent of user weights. |
| `schema_migrations` | `version`, `applied_at` | Records which SQL files have been applied. Created by the Python migration runner. |

`edges.source`, `edges.target`, `reference_scores.node` and `route_cache.node`
are foreign keys to `nodes.id`. Feature geometry has no node foreign key:
spatial predicates associate it with the reachable area. Constraints reject
invalid layer names, negative/non-finite costs and negative transit frequencies.
GiST geometry indexes support spatial queries; source/target indexes support
edge joins, and partial GiST indexes separate the three feature layers.

The SQL function also uses connection-local temporary tables named
`walkability_reachable_nodes` and `walkability_reachable_edges`. These hold one
calculation's working graph and are dropped at transaction commit; they are
separate from the persistent `route_cache`.

### Local source files and caches

These paths are relative to `stockholm-walkability/backend/` by default:

| Path | Contents and role |
| --- | --- |
| `data/stockholm_network.graphml` | Downloaded walking graph, including node coordinates, edge lengths and any available grades. |
| `data/stockholm_amenities.geojson` | Downloaded point features for shops and selected amenities. |
| `data/stockholm_greenspace.geojson` | Downloaded park, garden, forest and grass polygons. |
| `data/stockholm_transit.geojson` | Downloaded OSM transit-stop/station points. |
| `data/stockholm_boundary.geojson` | Cached municipal boundary; imported when present. Older complete source caches can omit it. |
| `data/stockholm_{amenities,greenspace,transit}.gpkg` | Supported alternatives to the corresponding GeoJSON files; preferred when both formats exist. |
| `data/reference_scores.csv` | Legacy file-mode references. Normal database preparation generates references in SQL instead. |
| `cache/` | OSMnx HTTP download cache, separate from the database route cache. |

Local files are import/download caches. Once preparation succeeds, the
database-backed API can run without them. Keeping them allows a reimport
without downloading again. User-created comparison points remain in the
browser's local storage and are not saved in these tables.

Input files are `stockholm_network.graphml` and `stockholm_{amenities,
greenspace,transit}.gpkg` or `.geojson` (GeoPackage takes precedence). All three
feature datasets are required so an incomplete import cannot silently remove
a scoring category. The normal `data_prep.py` pipeline resamples ten nodes with
a fixed seed and calculates their reference values in SQL. Customize or refresh
preparation from `stockholm-walkability/`:

```bash
# Download fresh source data and replace the application dataset:
python backend/data_prep.py --refresh

# Reuse the source files and rebuild the application dataset:
python backend/data_prep.py --reimport

# Rebuild with a larger reference sample:
python backend/data_prep.py --reimport --reference-points 100

# Use a different download/cache directory:
python backend/data_prep.py --reimport --data-dir /path/to/source-data
```

`--replace-database` is accepted as an alias for `--reimport`. The advanced
`python backend/database.py init` command creates a missing database and applies
migrations without importing. `python backend/database.py import --replace`
imports source files without downloads; unlike normal preparation, it reuses
node IDs from an existing reference CSV unless `--reference-points` is given.

COPY and reference generation run in one transaction. A failed import rolls
back the previous dataset and cache. Replacement blocks active score
calculations until they finish, replaces all application data and invalidates
cached routes atomically. Running API workers see the new references on their
next request; a restart is unnecessary. Source files are never deleted.

Individual cache files are published atomically after successful downloads,
so a failed network refresh preserves the previous version of that file. A
partial download can be resumed by rerunning preparation with `--reimport`.
The preparation script downloads municipal OSM transit stops. Frequencies use
`departures_per_hour` when present, including zero, and otherwise default to
four per stop. New elevation downloads use `GOOGLE_ELEVATION_API_KEY`; without
it, new edges use zero grade. Imported GraphML grades are retained regardless
of that environment variable. The optional `--skip-database` mode only prepares
local files and Python reference scores for the earlier file-backed API mode.

## Calculations and performance

Storage uses EPSG:32634 (metres in Stockholm). GeoJSON responses are transformed
to EPSG:4326 for Mapbox. GiST indexes support nearest-node lookup and spatial
feature filtering. Invalid feature geometries are repaired with `ST_MakeValid`
during import.

`pgr_drivingDistance` computes the reachable nodes using directed
`effective_length` costs and a walking speed of 4.5 km/h. Each original arc is
kept separately, including its slope direction. Induced edges connect two
reachable nodes, as in the existing NetworkX implementation. Input edges are
filtered with an indexed envelope using an imported lower bound on cost per
metre of endpoint displacement. The triangle inequality guarantees that every
vertex on an affordable path lies in this envelope, including winding paths.
A zero-cost arc between different coordinates disables that optimization.

Morphology uses degree >= 3 in that directed multigraph, hull area, mean edge
length, and inverse mean physical shortest-path distance within the reachable
graph. This preserves the current Python formulas, including counting each
directed arc at both endpoints. Accessibility counts amenities and sums transit
frequencies strictly within the hull. Green space uses summed intersection
areas capped at one, preserving the current overlap handling. The reach shape
is a convex hull approximation; it can include places not connected to the
walking network. Isolated or collinear reachable nodes get a ten-metre buffer
so the API always returns a valid polygon. Morphology retains the existing
400-metre-circle area fallback for those degenerate hulls.

Both live requests and references call `walkability.analyze`, eliminating the
previous difference between buffered-node reference counts and hull-based
live counts. Scores can consequently differ from the old CSV normalization.
For database-backed live scoring, only z-score normalization and weighted
composites remain in Python. Slope costs are calculated once in Python during
import, then used as stored costs by pgRouting.

| Work | Implementation |
| --- | --- |
| Download OSM data and parse source files | Python: OSMnx/GeoPandas in `data_prep.py` and `database.py`. |
| Convert length/grade to routing cost | Python: `scoring.calculate_effective_edge_length()` during import. |
| Store/project/repair geometry and find the nearest node | PostgreSQL/PostGIS. |
| Find reachable nodes and shortest-path distances | pgRouting `pgr_drivingDistance`. |
| Build the convex hull, count features and intersect green space | PostGIS inside `walkability.analyze()`. |
| Calculate the six raw morphology/accessibility factors | SQL inside `walkability.analyze()`. |
| Normalize factors, apply weights and calculate the final score | Python: `scoring.py`. |
| Validate HTTP input and return responses | Python: FastAPI in `api.py`. |
| Render results and persist comparison pins | JavaScript in `frontend/index.html`. |

Each database connection has its own temporary routing tables. A bounded pool
allows concurrent calculations. Equal node/time/speed requests share a
persistent cache; slider weights are applied afterward, so changes reuse raw
route results. The first request still runs Dijkstra and serializes its edges.
Full map layers still return full GeoJSON. Speed improvements depend on
dataset size, cache state and database resources; they are not guaranteed by
moving the work into SQL alone.

On the existing Stockholm files (50,060 nodes, 136,580 arcs), three sampled
15-minute routes measured 39–101 ms on their first SQL request and 3–8 ms on
cached requests. The current Python route measured 26–78 ms. These local
measurements exclude API startup and HTTP overhead; Python's nearest-node
lookup was bypassed by supplying the known sample node. Caching substantially
helps slider recalculations; first-time SQL requests were still slightly
slower on this sample.

`DATABASE_POOL_SIZE` defaults to 8 per API process.
`DATABASE_STATEMENT_TIMEOUT_MS` defaults to 30000 for API queries. Imports
use a separate connection without that timeout. Walking time is validated
between zero (exclusive) and 120 minutes. Spatial coverage uses
`stockholm_boundary.geojson` if supplied; otherwise it uses the imported
network's metric extent. Browser comparison points continue to use the
existing local-storage mechanism.

## Tests

From `stockholm-walkability/`, with the virtual environment activated:

```bash
cd backend
python -m pip install httpx
TEST_DATABASE_URL='postgresql://gis:gis@localhost:5435/postgres' \
  python -m unittest test_database -v
```

The test role must have `CREATE DATABASE`. The suite creates a uniquely named
test database and drops that database afterward. The 25 tests include missing
database creation, first-run downloads/import, cached reruns, configuration
preservation, failed-refresh recovery, and SQL/Python factor
parity, directed slopes, parallel edges, isolated nodes, spatial properties,
reference consistency, rollback, cache invalidation, concurrency, input
validation and the API without local-file loading. Without `TEST_DATABASE_URL`,
only the input/property unit tests run and integration tests are skipped.

The pre-existing `test_scoring.py` currently cannot import missing
`load_gtfs_stops` and other earlier pipeline functions in this checkout. It is
left intact; its failures are independent of the new database suite.

## Where to make future changes

| Change | Files to start with |
| --- | --- |
| Downloaded OSM categories, boundary or elevation behavior | `data_prep.py`; use `--refresh` to fetch data with new download rules. |
| Import mappings, reference sampling or dataset metadata | `database.py`, especially `import_dataset()`. |
| Routing, reach geometry or raw spatial formulas | A new SQL migration updating `walkability.analyze()`, plus database parity tests. Reimport to rebuild references and clear cached results. |
| Table columns, constraints or indexes | A new file in `sql/`, with any matching importer/query changes in `database.py`. |
| Normalization or score weights | `scoring.py`; its composites are applied after cached raw results are loaded. |
| HTTP validation or response fields | `api.py`, with matching frontend request/display changes where needed. |
| Connection, pool size or query timeout | `.env` or shell environment; restart the API to load new runtime settings. |
| Server ports | `run.py` and the frontend's `API_BASE`; `compose.yaml` only controls the optional database container port. |

The migration runner executes `sql/*.sql` in filename order and records each
file's stem in `walkability.schema_migrations`. For an existing installation,
add a migration such as `002_update_routing.sql`; editing an already-applied
`001_initial.sql` will not apply that edit to the database. Run preparation to
apply new migrations. If they change raw calculations or data mappings, run
`python backend/data_prep.py --reimport` from `stockholm-walkability/` as well,
so stored references and cached routes agree with the new implementation.

Implementation references: [pgRouting driving distance](https://docs.pgrouting.org/3.8/en/pgr_drivingDistance.html),
[PostGIS indexed nearest-neighbor lookup](https://postgis.net/docs/geometry_distance_knn.html),
and the [official pgRouting Docker images](https://github.com/pgRouting/docker-pgrouting).
