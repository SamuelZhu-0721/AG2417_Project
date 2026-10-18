"""PostgreSQL storage, PostGIS analysis, and migration/import commands."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values, load_dotenv, set_key
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

BASE_DIR = Path(__file__).resolve().parent
METRIC_SRID = 32634
WALKING_SPEED_KMH = 4.8
DEFAULT_DATABASE_URL = "postgresql://gis:gis@localhost:5435/walkability"


def configured_url():
    load_dotenv(BASE_DIR / ".env")
    return os.getenv("DATABASE_URL") or None


def bootstrap_configuration():
    url = configured_url() or DEFAULT_DATABASE_URL
    path = BASE_DIR / ".env"
    if not dotenv_values(path).get("DATABASE_URL"):
        set_key(path, "DATABASE_URL", url)
    return url


def initialize_database(url):
    params = conninfo_to_dict(url)
    name = params.get("dbname")
    if not name:
        raise ValueError("DATABASE_URL must include a database name")
    maintenance = dict(params, dbname=os.getenv("DATABASE_MAINTENANCE_DB", "postgres"))
    maintenance.setdefault("connect_timeout", "5")
    with psycopg.connect(make_conninfo(**maintenance), autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            try:
                conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
            except psycopg.errors.DuplicateDatabase:
                pass
            print(f"Created PostgreSQL database: {name}")
    migrate(url)


def dataset_metadata(url):
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as conn:
        row = conn.execute("SELECT metadata FROM walkability.dataset").fetchone()
        return row["metadata"] if row else None


def migrate(url):
    with psycopg.connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(861945207)")
        available = {row[0] for row in conn.execute(
            "SELECT name FROM pg_available_extensions WHERE name IN ('postgis', 'pgrouting')")}
        missing = {"postgis", "pgrouting"} - available
        if missing:
            raise RuntimeError("The PostgreSQL server is missing extension packages: " + ", ".join(sorted(missing)))
        conn.execute("CREATE SCHEMA IF NOT EXISTS walkability")
        conn.execute("""CREATE TABLE IF NOT EXISTS walkability.schema_migrations (
                        version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
        applied = {row[0] for row in conn.execute("SELECT version FROM walkability.schema_migrations")}
        for path in sorted((BASE_DIR / "sql").glob("*.sql")):
            if path.stem not in applied:
                conn.execute(path.read_text())
                conn.execute("INSERT INTO walkability.schema_migrations (version) VALUES (%s)", (path.stem,))
        conn.execute("ALTER TABLE walkability.nodes ADD COLUMN IF NOT EXISTS elevation float8")


class Database:
    def __init__(self, url):
        self.pool = ConnectionPool(
            url, min_size=1, max_size=int(os.getenv("DATABASE_POOL_SIZE", "8")),
            timeout=10, open=False,
            kwargs={"row_factory": dict_row, "connect_timeout": 5,
                    "options": "-c statement_timeout=" + str(int(os.getenv("DATABASE_STATEMENT_TIMEOUT_MS", "30000")))},
        )

    def open(self):
        try:
            self.pool.open(wait=True, timeout=10)
            self.health()
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        self.pool.close()

    def health(self):
        with self.pool.connection() as conn:
            dataset = conn.execute("SELECT revision, imported_at, metadata FROM walkability.dataset").fetchone()
            if dataset is None:
                raise RuntimeError("No PostgreSQL dataset. Run `python data_prep.py` first.")
            return {"status": "ok", "storage": "postgresql", "routing": "pgrouting",
                    "revision": dataset["revision"], "imported_at": dataset["imported_at"].isoformat(),
                    **dataset["metadata"]}

    def layer(self, name):
        if name not in {"amenities", "greenspace", "transit", "reference-points"}:
            raise ValueError("Unknown map layer")
        with self.pool.connection() as conn:
            if name == "reference-points":
                rows = conn.execute("""SELECT r.node AS id, ST_AsGeoJSON(ST_Transform(n.geom, 4326))::jsonb AS geometry,
                                              to_jsonb(r) || jsonb_build_object('x', ST_X(n.geom), 'y', ST_Y(n.geom)) AS properties
                                       FROM walkability.reference_scores r JOIN walkability.nodes n ON n.id = r.node ORDER BY r.node""")
            else:
                rows = conn.execute("""SELECT id, ST_AsGeoJSON(ST_Transform(geom, 4326))::jsonb AS geometry, properties
                                       FROM walkability.features WHERE layer = %s ORDER BY id""", (name,))
            return {"type": "FeatureCollection", "features": [
                {"type": "Feature", **row} for row in rows
            ]}

    def score(self, lat, lon, minutes, weights, walking_speed_kmh=WALKING_SPEED_KMH, exclusion_polygons=None):
        from scoring import AccessibilityFactors, MorphologyFactors, accessibility_composite, morphology_composite, final_walkability_score
        exclusion_polygons = exclusion_polygons or []

        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock_shared(861945208)")
            
            meta_row = conn.execute("SELECT metadata FROM walkability.dataset").fetchone()
            if meta_row is None or not meta_row["metadata"]:
                raise RuntimeError("No PostgreSQL dataset. Run `python data_prep.py` first.")
            
            bounds = meta_row["metadata"].get("bounds")
            if not bounds or len(bounds) < 4:
                raise RuntimeError("Dataset bounding box metadata is missing or invalid.")

            coverage = conn.execute("""SELECT ST_Covers(
                                      ST_MakeEnvelope(%s, %s, %s, %s, 32634),
                                      ST_Transform(ST_SetSRID(ST_Point(%s, %s), 4326), 32634)) AS inside""",
                                    (bounds[0], bounds[1], bounds[2], bounds[3], lon, lat)).fetchone()
            
            if coverage is None or not coverage["inside"]:
                raise ValueError("Point is outside the imported study area.")
                
            nearest = conn.execute("""SELECT id FROM walkability.nodes
                                      ORDER BY geom <-> ST_Transform(ST_SetSRID(ST_Point(%s, %s), 4326), 32634)
                                      LIMIT 1""", (lon, lat)).fetchone()
            node = nearest["id"]
            
            if not exclusion_polygons:
                cache_key = f"{node}:{float(minutes)}:{float(walking_speed_kmh)}"
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (cache_key,))
                cached = conn.execute("""SELECT result FROM walkability.route_cache
                                         WHERE node = %s AND trip_minutes = %s AND walking_speed_kmh = %s""",
                                      (node, minutes, walking_speed_kmh)).fetchone()
                raw = cached["result"] if cached else analyze(conn, node, minutes, walking_speed_kmh, exclusion_polygons)
                if cached is None:
                    conn.execute("""INSERT INTO walkability.route_cache (node, trip_minutes, walking_speed_kmh, result)
                                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                 (node, minutes, walking_speed_kmh, Jsonb(raw)))
            else:
                raw = analyze(conn, node, minutes, walking_speed_kmh, exclusion_polygons)

            if "elevation" not in raw:
                raw["elevation"] = reach_elevation(conn, node, raw["isochrone"])
            
            references = conn.execute("SELECT * FROM walkability.reference_scores").fetchall()
            if not references:
                raise RuntimeError("PostgreSQL reference scores are missing. Reimport the dataset.")

        morph = MorphologyFactors(**raw["morphology"])
        access = AccessibilityFactors(**raw["accessibility"])
        morph_ref = [MorphologyFactors(r["intersection_density"], r["integration"], r["avg_block_length"]) for r in references]
        access_ref = [AccessibilityFactors(r["amenity_count"], r["transit_stop_score"], r["green_space_ratio"]) for r in references]
        morphology_score = morphology_composite(morph, morph_ref)
        accessibility_score = accessibility_composite(weights, access, access_ref)
        final_score = final_walkability_score(accessibility_score, morphology_score)

        return {
            "morphology_score": morphology_score, 
            "accessibility_score": accessibility_score,
            "final_score": final_score,
            "isochrone": {
                "type": "Feature", 
                "geometry": raw["isochrone"],
                "properties": {
                    "morphology_score": morphology_score,
                    "accessibility_score": accessibility_score, 
                    "final_score": final_score
                }
            },
            "edges": raw["edges"], 
            "factors": {    
                "morphology": raw["morphology"], 
                "accessibility": raw["accessibility"]
            },
            "elevation_profile": raw["elevation"]
        }


def analyze(conn, node, minutes=15.0, walking_speed_kmh=WALKING_SPEED_KMH, exclusion_polygons=None):
    exclusion_polygons = exclusion_polygons or []
    return conn.execute("SELECT walkability.analyze(%s, %s, %s, %s::jsonb) AS result",
                        (int(node), minutes, walking_speed_kmh, json.dumps(exclusion_polygons))).fetchone()["result"]


def reach_elevation(conn, node, isochrone):
    from scoring import elevation_summary
    origin = conn.execute("SELECT elevation FROM walkability.nodes WHERE id = %s", (int(node),)).fetchone()
    rows = conn.execute("""SELECT n.elevation FROM walkability.nodes n
                           WHERE n.elevation IS NOT NULL AND ST_Covers(
                               ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s), n.geom)""",
                        (json.dumps(isochrone), METRIC_SRID)).fetchall()
    return elevation_summary([r["elevation"] for r in rows], origin["elevation"] if origin else None)


def _json_value(value):
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _copy(conn, statement, rows):
    with conn.cursor().copy(statement) as copy:
        for row in rows:
            copy.write_row(row)


def import_dataset(url, data_dir=BASE_DIR / "data", replace=False, reference_points=None):
    import geopandas as gpd
    import numpy as np
    import osmnx as ox
    import pandas as pd
    from pyproj import Transformer
    from shapely.geometry import LineString, shape
    from scoring import calculate_effective_edge_length

    data_dir = Path(data_dir)
    graph = ox.load_graphml(data_dir / "stockholm_network.graphml")
    if not graph:
        raise ValueError("Cannot import an empty walking network")
    if not graph.is_directed():
        graph = graph.to_directed()
    transform = Transformer.from_crs(graph.graph["crs"], METRIC_SRID, always_xy=True)
    from shapely.ops import transform as transform_geometry

    def _elevation(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    nodes = [(int(n), *transform.transform(float(d["x"]), float(d["y"])), _elevation(d.get("elevation")))
             for n, d in graph.nodes(data=True)]
    if not all(math.isfinite(x) and math.isfinite(y) for _, x, y, _e in nodes):
        raise ValueError("Network contains non-finite coordinates")

    def edge_rows():
        for u, v, key, data in graph.edges(keys=True, data=True):
            length = float(data.get("length", 1))
            grade = float(data.get("grade") or 0)
            if math.isnan(grade):
                grade = 0.0
            geom = data.get("geometry")
            if geom is None:
                geom = LineString([(graph.nodes[n]["x"], graph.nodes[n]["y"]) for n in (u, v)])
            yield (int(u), int(v), int(key), length, grade, calculate_effective_edge_length(length, grade),
                   str(data.get("highway", "residential")), transform_geometry(transform.transform, geom).wkb)

    migrate(url)
    with psycopg.connect(url, row_factory=dict_row) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(861945207)")
        conn.execute("SELECT pg_advisory_xact_lock(861945208)")
        exists = conn.execute("SELECT 1 FROM walkability.dataset").fetchone()
        if exists and not replace:
            raise ValueError("A dataset already exists. Pass --replace to replace it atomically.")
        conn.execute("""TRUNCATE walkability.route_cache, walkability.reference_scores, walkability.features,
                        walkability.edges, walkability.nodes, walkability.dataset RESTART IDENTITY""")
        conn.execute("CREATE TEMP TABLE import_nodes (id bigint, x float8, y float8, elevation float8) ON COMMIT DROP")
        _copy(conn, "COPY import_nodes FROM STDIN", nodes)
        conn.execute("INSERT INTO walkability.nodes (id, geom, elevation) SELECT id, ST_SetSRID(ST_Point(x, y), 32634), elevation FROM import_nodes")
        conn.execute("""CREATE TEMP TABLE import_edges (source bigint, target bigint, edge_key bigint, length float8,
                        grade float8, effective_length float8, highway text, geom bytea) ON COMMIT DROP""")
        _copy(conn, "COPY import_edges FROM STDIN", edge_rows())
        conn.execute("""INSERT INTO walkability.edges (source, target, edge_key, length, grade, effective_length, highway, geom)
                        SELECT source, target, edge_key, length, grade, effective_length, highway,
                               ST_GeomFromWKB(geom, 32634) FROM import_edges""")

        conn.execute("CREATE TEMP TABLE import_features (layer text, geom bytea, properties jsonb) ON COMMIT DROP")
        counts = {}
        for layer in ("amenities", "greenspace", "transit"):
            candidates = [data_dir / f"stockholm_{layer}{ext}" for ext in (".gpkg", ".geojson")]
            path = next((p for p in candidates if p.exists()), None)
            if path is None:
                raise FileNotFoundError(f"Missing {layer} data in {data_dir}. Run data_prep.py first.")
            frame = gpd.read_file(path).to_crs(METRIC_SRID)
            counts[layer] = len(frame)

            def feature_rows():
                for _, row in frame.iterrows():
                    geom = row.geometry
                    if geom is None or geom.is_empty:
                        raise ValueError(f"{layer} contains an empty geometry")
                    properties = {col: _json_value(row[col]) for col in frame.columns if col != frame.geometry.name}
                    yield layer, geom.wkb, Jsonb(properties)

            _copy(conn, "COPY import_features FROM STDIN", feature_rows())
        conn.execute("""INSERT INTO walkability.features (layer, geom, properties)
                        SELECT layer, ST_MakeValid(ST_GeomFromWKB(geom, 32634)), properties FROM import_features""")

        boundary = None
        boundary_path = data_dir / "stockholm_boundary.geojson"
        if boundary_path.exists():
            document = json.loads(boundary_path.read_text())
            if document["type"] == "FeatureCollection":
                from shapely.ops import unary_union
                boundary = unary_union([shape(f["geometry"]) for f in document["features"]])
            else:
                boundary = shape(document["geometry"] if document["type"] == "Feature" else document)
            if boundary.is_empty or not boundary.is_valid or boundary.geom_type not in {"Polygon", "MultiPolygon"}:
                raise ValueError("Invalid study-area boundary")
            boundary = boundary.wkb

        conn.execute("ANALYZE walkability.nodes")
        conn.execute("ANALYZE walkability.edges")
        conn.execute("ANALYZE walkability.features")
        reference_path = data_dir / "reference_scores.csv"
        if reference_points is None and reference_path.exists():
            sample = [int(n) for n in pd.read_csv(reference_path)["node"].unique()]
        else:
            count = reference_points if reference_points is not None else 10
            if count <= 0:
                raise ValueError("Reference point count must be positive")
            node_ids = [n[0] for n in nodes]
            sample = np.random.default_rng(42).choice(node_ids, min(count, len(nodes)), replace=False)
        if len(sample) == 0:
            raise ValueError("Reference dataset cannot be empty")
        for node in sample:
            raw = analyze(conn, node, 15.0, 4.8, [])
            m, a = raw["morphology"], raw["accessibility"]
            conn.execute("""INSERT INTO walkability.reference_scores VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                         (int(node), m["intersection_density"], m["integration"], m["avg_block_length"],
                          a["amenity_count"], a["transit_stop_score"], a["green_space_ratio"]))
        bounds = conn.execute("SELECT ST_Extent(geom) AS extent FROM walkability.nodes").fetchone()["extent"]
        bounds = [float(v) for v in bounds.removeprefix("BOX(").removesuffix(")").replace(",", " ").split()]
        if bounds[0] == bounds[2]:
            bounds[0], bounds[2] = bounds[0] - 10, bounds[2] + 10
        if bounds[1] == bounds[3]:
            bounds[1], bounds[3] = bounds[1] - 10, bounds[3] + 10
        lower_bound = conn.execute("""SELECT min(e.effective_length / nullif(ST_Distance(s.geom, t.geom), 0)) AS bound
                                      FROM walkability.edges e JOIN walkability.nodes s ON s.id = e.source
                                      JOIN walkability.nodes t ON t.id = e.target""").fetchone()["bound"]
        metadata = {"nodes_loaded": len(nodes), "edges_loaded": graph.number_of_edges(),
                    "amenities_loaded": counts["amenities"], "greenspace_loaded": counts["greenspace"],
                    "transit_stops_loaded": counts["transit"], "reference_points_loaded": len(sample),
                    "metric_srid": METRIC_SRID, "bounds": bounds,
                    "elevation_coverage": round(sum(n[3] is not None for n in nodes) / len(nodes), 4),
                    "minimum_cost_per_metre": lower_bound}
        conn.execute("""INSERT INTO walkability.dataset (revision, metadata, boundary)
                        VALUES (%s, %s, ST_Transform(ST_GeomFromWKB(%s, 4326), 32634))""",
                     (str(uuid4()), Jsonb(metadata), boundary))
        return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Apply PostgreSQL/PostGIS/pgRouting migrations")
    importer = sub.add_parser("import", help="Import existing local files and rebuild reference scores")
    importer.add_argument("--data-dir", type=Path, default=BASE_DIR / "data")
    importer.add_argument("--replace", action="store_true", help="Replace the existing application dataset atomically")
    importer.add_argument("--reference-points", type=int, help="Resample this many reference nodes (default: reuse CSV nodes)")
    args = parser.parse_args()
    url = bootstrap_configuration()
    initialize_database(url)
    if args.command == "init":
        print("PostgreSQL schema ready.")
    else:
        print(json.dumps(import_dataset(url, args.data_dir, args.replace, args.reference_points), indent=2))


if __name__ == "__main__":
    main()