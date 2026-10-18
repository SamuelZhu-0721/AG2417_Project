"""Offline unit tests and real PostgreSQL/PostGIS/pgRouting integration tests.

TEST_DATABASE_URL must identify a server where this role can CREATE DATABASE.
The suite creates and drops its own uniquely named database. It never imports
into or truncates an existing application database.

Run: TEST_DATABASE_URL=postgresql://... python -m unittest test_database -v
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pyproj import Transformer
from shapely.geometry import LineString, MultiPoint, Point, box, shape
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api
import data_prep
from database import (Database, DEFAULT_DATABASE_URL, METRIC_SRID, _json_value, analyze,
                      bootstrap_configuration, import_dataset, initialize_database, migrate)
from scoring import calculate_effective_edge_length, compute_morphology_factors, isochrone_subgraph


class RequestTests(unittest.TestCase):
    def test_invalid_coordinates_and_times(self):
        for override in ({"lat": 91}, {"lon": -181}, {"lat": math.nan},
                         {"trip_minutes": 0}, {"trip_minutes": -1}, {"trip_minutes": math.inf},
                         {"trip_minutes": 121}):
            with self.subTest(override=override), self.assertRaises(ValidationError):
                api.ScoreRequest(**({"lat": 59.33, "lon": 18.06} | override))

    def test_invalid_weights(self):
        for weights in ({"amenity": -1}, {"green": math.inf}, {"transit": math.nan}, {"unknown": 1}):
            with self.subTest(weights=weights), self.assertRaises(ValidationError):
                api.ScoreRequest(lat=59, lon=18, weights=weights)

    def test_default_weights_are_independent(self):
        one, two = api.ScoreRequest(lat=59, lon=18), api.ScoreRequest(lat=59, lon=18)
        one.weights["green"] = 9
        self.assertEqual(two.weights["green"], 1 / 3)

    def test_osm_properties_are_valid_json(self):
        self.assertEqual(_json_value({"tag": [1, math.nan, math.inf]}), {"tag": [1, None, None]})
        json.dumps(_json_value({"missing": float("nan")}), allow_nan=False)

    def test_first_setup_writes_database_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch("database.BASE_DIR", Path(directory)), \
                patch("database.configured_url", return_value=None):
            self.assertEqual(bootstrap_configuration(), DEFAULT_DATABASE_URL)
            self.assertIn("localhost:5435/walkability", (Path(directory) / ".env").read_text())

    def test_first_setup_preserves_other_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch("database.BASE_DIR", Path(directory)), \
                patch("database.configured_url", return_value=None):
            path = Path(directory) / ".env"
            path.write_text("GOOGLE_ELEVATION_API_KEY=example-key\n")
            bootstrap_configuration()
            self.assertIn("GOOGLE_ELEVATION_API_KEY=example-key", path.read_text())
            self.assertIn("DATABASE_URL", path.read_text())

    def test_existing_database_configuration_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch("database.BASE_DIR", Path(directory)), \
                patch("database.configured_url", return_value="postgresql://example/other"):
            path = Path(directory) / ".env"
            path.write_text("DATABASE_URL=postgresql://example/other\n")
            previous = path.read_bytes()
            self.assertEqual(bootstrap_configuration(), "postgresql://example/other")
            self.assertEqual(path.read_bytes(), previous)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for PostGIS integration tests")
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_url = os.environ["TEST_DATABASE_URL"]
        cls.name = "walkability_test_" + uuid4().hex
        with psycopg.connect(cls.admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(cls.name)))
        params = conninfo_to_dict(cls.admin_url)
        params["dbname"] = cls.name
        cls.url = make_conninfo(**params)
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        try:
            cls.create_fixture()
            migrate(cls.url)
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()
        with psycopg.connect(cls.admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.name)))

    @classmethod
    def create_fixture(cls):
        cls.x, cls.y = Transformer.from_crs(4326, METRIC_SRID, always_xy=True).transform(18.0686, 59.3293)
        cls.graph = nx.MultiDiGraph(crs=f"EPSG:{METRIC_SRID}")
        offsets = {1: (0, 0), 2: (100, 0), 3: (100, 100), 4: (0, 100),
                   5: (200, 0), 6: (200, 100), 7: (-200, -200), 8: (-100, 0)}
        for node, (dx, dy) in offsets.items():
            cls.graph.add_node(node, x=cls.x + dx, y=cls.y + dy, elevation=float(node) * 5)
        for u, v in [(1, 2), (2, 3), (3, 4), (4, 1), (2, 5), (5, 6), (6, 3)]:
            for source, target in ((u, v), (v, u)):
                grade = 0.15 if (source, target) == (1, 2) else -0.15 if (source, target) == (2, 1) else 0
                cls.graph.add_edge(source, target, length=100.0, grade=grade,
                                   effective_length=calculate_effective_edge_length(100, grade))
        curve = LineString([(cls.x, cls.y), (cls.x - 10, cls.y + 50), (cls.x, cls.y + 100)])
        cls.graph.add_edge(1, 4, length=130.0, grade=0, effective_length=130.0, geometry=curve)
        cls.graph.add_edge(8, 1, length=100.0, grade=0, effective_length=100.0)
        ox.save_graphml(cls.graph, cls.directory / "stockholm_network.graphml")
        cls.amenities = gpd.GeoDataFrame({"name": ["Cafe", "Shop"]}, geometry=[
            Point(cls.x + 20, cls.y + 60), Point(cls.x + 150, cls.y + 40)], crs=METRIC_SRID)
        cls.greenspace = gpd.GeoDataFrame({"name": ["Park"]}, geometry=[
            box(cls.x + 10, cls.y + 30, cls.x + 50, cls.y + 80)], crs=METRIC_SRID)
        cls.transit = gpd.GeoDataFrame({"name": ["Stop A", "Stop B"]}, geometry=[
            Point(cls.x + 30, cls.y + 80), Point(cls.x + 170, cls.y + 50)], crs=METRIC_SRID)
        for layer in ("amenities", "greenspace", "transit"):
            getattr(cls, layer).to_file(cls.directory / f"stockholm_{layer}.geojson", driver="GeoJSON")
        pd.DataFrame({"node": list(cls.graph)}).to_csv(cls.directory / "reference_scores.csv", index=False)

    def setUp(self):
        import_dataset(self.url, self.directory, replace=True)
        self.db = Database(self.url).open()
        self.addCleanup(self.db.close)

    def raw(self, node=1, minutes=3):
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            return analyze(conn, node, minutes)

    def coordinates(self, node=1):
        data = self.graph.nodes[node]
        lon, lat = Transformer.from_crs(METRIC_SRID, 4326, always_xy=True).transform(data["x"], data["y"])
        return lat, lon

    def test_migrations_are_idempotent(self):
        migrate(self.url)
        migrate(self.url)
        with psycopg.connect(self.url) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM walkability.schema_migrations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM walkability.nodes").fetchone()[0], 8)

    def test_initializer_creates_missing_database_and_can_rerun(self):
        name = "walkability_bootstrap_" + uuid4().hex
        params = conninfo_to_dict(self.admin_url)
        params["dbname"] = name
        url = make_conninfo(**params)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                initialize_database(url)
                initialize_database(url)
            with psycopg.connect(url) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM walkability.nodes").fetchone()[0], 0)
                extensions = {r[0] for r in conn.execute("SELECT extname FROM pg_extension")}
                self.assertTrue({"postgis", "pgrouting"}.issubset(extensions))
        finally:
            with psycopg.connect(self.admin_url, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))

    def test_preparation_first_run_downloads_imports_and_calculates_in_sql(self):
        with psycopg.connect(self.url) as conn:
            conn.execute("DELETE FROM walkability.dataset")
        boundary = gpd.GeoDataFrame(geometry=[box(self.x - 500, self.y - 500, self.x + 500, self.y + 500)],
                                    crs=METRIC_SRID).to_crs(4326)

        def features(polygon, tags):
            if "public_transport" in tags:
                return self.transit.to_crs(4326)
            if "amenity" in tags:
                return self.amenities.to_crs(4326)
            return self.greenspace.to_crs(4326)

        with tempfile.TemporaryDirectory() as directory, patch("data_prep.DATA_DIR", directory), \
                patch("database.bootstrap_configuration", return_value=self.url), \
                patch("osmnx.geocode_to_gdf", return_value=boundary) as geocode, \
                patch("osmnx.graph_from_polygon", return_value=self.graph.copy()) as download, \
                patch("osmnx.project_graph", side_effect=lambda graph: graph), \
                patch("osmnx.features_from_polygon", side_effect=features) as feature_download, \
                patch.dict(os.environ, {"GOOGLE_ELEVATION_API_KEY": ""}), \
                patch("data_prep.build_reference_dataset", side_effect=AssertionError("Python reference calculation ran")), \
                contextlib.redirect_stdout(io.StringIO()):
            metadata = data_prep.main(["--reference-points", "4"])
            self.assertEqual(metadata["nodes_loaded"], 8)
            self.assertEqual(metadata["reference_points_loaded"], 4)
            self.assertTrue((Path(directory) / "stockholm_network.graphml").exists())
            self.assertTrue((Path(directory) / "stockholm_boundary.geojson").exists())
            self.assertEqual(feature_download.call_count, 3)
            geocode.assert_called_once()
            self.assertTrue(download.call_args.kwargs["retain_all"])
        self.assertTrue(self.db.score(*self.coordinates(), 3.0, {})["isochrone"])

    def test_preparation_existing_database_needs_no_source_files_or_downloads(self):
        revision = self.db.health()["revision"]
        with tempfile.TemporaryDirectory() as directory, patch("data_prep.DATA_DIR", directory), \
                patch("database.bootstrap_configuration", return_value=self.url), \
                patch("data_prep.download_network", side_effect=AssertionError("download attempted")), \
                contextlib.redirect_stdout(io.StringIO()):
            metadata = data_prep.main([])
            self.assertEqual(metadata["nodes_loaded"], 8)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(self.db.health()["revision"], revision)

    def test_preparation_reimports_caches_without_osm_requests(self):
        with patch("data_prep.DATA_DIR", str(self.directory)), \
                patch("database.bootstrap_configuration", return_value=self.url), \
                patch("osmnx.graph_from_polygon", side_effect=AssertionError("network redownloaded")), \
                patch("osmnx.features_from_polygon", side_effect=AssertionError("features redownloaded")), \
                patch("osmnx.geocode_to_gdf", side_effect=AssertionError("boundary redownloaded")), \
                contextlib.redirect_stdout(io.StringIO()):
            metadata = data_prep.main(["--reimport", "--reference-points", "3"])
        self.assertEqual(metadata["reference_points_loaded"], 3)

    def test_failed_refresh_preserves_network_cache_and_database(self):
        revision = self.db.health()["revision"]
        path = self.directory / "stockholm_network.graphml"
        previous = path.read_bytes()
        with patch("data_prep.DATA_DIR", str(self.directory)), \
                patch("database.bootstrap_configuration", return_value=self.url), \
                patch("data_prep.get_boundary_polygon", return_value=(box(18, 59, 19, 60), "EPSG:4326")), \
                patch("osmnx.graph_from_polygon", side_effect=RuntimeError("download failed")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                data_prep.main(["--refresh"])
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(self.db.health()["revision"], revision)

    def test_slope_direction_parallel_edges_and_spatial_factors_match_python(self):
        for minutes in (1, 3, 15):
            with self.subTest(minutes=minutes):
                raw = self.raw(minutes=minutes)
                subgraph = isochrone_subgraph(self.graph, 1, trip_minutes=minutes)
                morph = compute_morphology_factors(self.graph, 1, iso_subgraph=subgraph)
                for key, expected in vars(morph).items():
                    self.assertAlmostEqual(raw["morphology"][key], expected, places=8)
                hull = MultiPoint([(subgraph.nodes[n]["x"], subgraph.nodes[n]["y"]) for n in subgraph]).convex_hull
                reach = hull if hull.area > 0 else hull.buffer(10)
                self.assertEqual(raw["accessibility"]["amenity_count"], int(self.amenities.geometry.within(reach).sum()))
                expected_transit = int(self.transit.geometry.within(reach).sum())
                self.assertEqual(raw["accessibility"]["transit_stop_score"], expected_transit)
                expected_green = min(1, self.greenspace.geometry.intersection(reach).area.sum() / reach.area)
                self.assertAlmostEqual(raw["accessibility"]["green_space_ratio"], expected_green, places=8)
                self.assertEqual(len(raw["edges"]["features"]), subgraph.number_of_edges())
        # Slope-aware costs: the flat curved edge to node 4 is affordable, the
        # steep climb to node 5 is not. (Exact sets depend on the slope model.)
        reach_3 = set(isochrone_subgraph(self.graph, 1, trip_minutes=3))
        self.assertLessEqual({1, 4}, reach_3)
        self.assertNotIn(5, reach_3)
        # Node 8 has only an outgoing connection to the rest of the graph.
        self.assertNotIn(8, isochrone_subgraph(self.graph, 1, trip_minutes=15))

    def test_isolated_node_returns_finite_scores_and_polygon(self):
        result = self.db.score(*self.coordinates(7), 15.0, {})
        self.assertTrue(all(math.isfinite(result[key]) for key in ("final_score", "morphology_score", "accessibility_score")))
        self.assertEqual(result["isochrone"]["geometry"]["type"], "Polygon")
        self.assertEqual(result["edges"]["features"], [])
        self.assertEqual(result["factors"]["morphology"]["integration"], 0)

    def test_cost_bound_preserves_cheap_long_edges_and_zero_cost_arcs(self):
        path = self.directory / "stockholm_network.graphml"
        try:
            for length in (5.0, 0.0):
                graph = self.graph.copy()
                # Its geometric displacement is ~283m, but the imported cost
                # allows this arc in 0.1 minutes. A guessed radius would omit it.
                graph.add_edge(1, 7, length=length, grade=0, effective_length=length)
                ox.save_graphml(graph, path)
                import_dataset(self.url, self.directory, replace=True)
                actual = self.raw(minutes=0.1)
                expected = isochrone_subgraph(graph, 1, trip_minutes=0.1)
                self.assertIn(7, expected)
                self.assertEqual(len(actual["edges"]["features"]), expected.number_of_edges())
        finally:
            ox.save_graphml(self.graph, path)

    def test_single_node_network_with_no_edges(self):
        path, refs = self.directory / "stockholm_network.graphml", self.directory / "reference_scores.csv"
        try:
            graph = nx.MultiDiGraph(crs=f"EPSG:{METRIC_SRID}")
            graph.add_node(1, x=self.x, y=self.y)
            ox.save_graphml(graph, path)
            pd.DataFrame({"node": [1]}).to_csv(refs, index=False)
            import_dataset(self.url, self.directory, replace=True)
            result = self.db.score(*self.coordinates(1), 15.0, {})
            self.assertEqual(result["edges"]["features"], [])
            self.assertEqual(result["isochrone"]["geometry"]["type"], "Polygon")
            self.assertEqual(result["final_score"], 0)
        finally:
            ox.save_graphml(self.graph, path)
            pd.DataFrame({"node": list(self.graph)}).to_csv(refs, index=False)

    def test_references_use_the_same_sql_as_live_requests(self):
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            for row in conn.execute("SELECT * FROM walkability.reference_scores").fetchall():
                raw = analyze(conn, row["node"], 15.0)
                for group in ("morphology", "accessibility"):
                    for key, value in raw[group].items():
                        self.assertAlmostEqual(row[key], value)

    def test_layers_preserve_properties_and_wgs84(self):
        for name, count in (("amenities", 2), ("greenspace", 1), ("transit", 2), ("reference-points", 8)):
            layer = self.db.layer(name)
            self.assertEqual(len(layer["features"]), count)
            self.assertTrue(all(17 < shape(f["geometry"]).centroid.x < 19 for f in layer["features"]))
        self.assertEqual(self.db.layer("transit")["features"][1]["properties"]["name"], "Stop B")
        with self.assertRaises(ValueError):
            self.db.layer("not-a-layer")

    def test_cache_reused_for_weights_and_separated_by_walking_time(self):
        first = self.db.score(*self.coordinates(), 3.0, {"amenity": 1, "transit": 0, "green": 0})
        second = self.db.score(*self.coordinates(), 3.0, {"amenity": 0, "transit": 1, "green": 0})
        self.db.score(*self.coordinates(), 15.0, {})
        self.assertEqual(first["edges"], second["edges"])
        self.assertEqual(first["factors"], second["factors"])
        self.assertNotEqual(first["accessibility_score"], second["accessibility_score"])
        with psycopg.connect(self.url) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM walkability.route_cache").fetchone()[0], 2)

    def test_concurrent_same_and_different_routes(self):
        requests = [(1, 3.0), (1, 3.0), (2, 5.0), (7, 15.0)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda req: self.db.score(*self.coordinates(req[0]), req[1], {}), requests))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[3]["edges"]["features"], [])

    def count_cached_routes(self):
        with psycopg.connect(self.url) as conn:
            return conn.execute("SELECT count(*) FROM walkability.route_cache").fetchone()[0]

    def test_node_elevations_are_stored_and_summarised(self):
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            stored = {r["id"]: r["elevation"] for r in conn.execute("SELECT id, elevation FROM walkability.nodes")}
        self.assertEqual(stored[2], 10.0)
        lat, lon = self.coordinates(1)
        profile = self.db.score(lat, lon, 3, {"amenity": 1, "transit": 1, "green": 1}, 4.8, [])["elevation_profile"]
        self.assertTrue(profile["available"])
        self.assertGreater(profile["relief"], 0)

    def test_hazard_polygon_shrinks_the_reach_and_skips_the_cache(self):
        to_wgs = Transformer.from_crs(METRIC_SRID, 4326, always_xy=True)
        corners = [(self.x - 20, self.y + 80), (self.x + 20, self.y + 80),
                   (self.x + 20, self.y + 120), (self.x - 20, self.y + 120)]
        ring = [list(to_wgs.transform(px, py)) for px, py in corners]
        hazard = {"type": "Polygon", "coordinates": [ring + [ring[0]]]}  # covers node 4
        lat, lon = self.coordinates(1)
        weights = {"amenity": 1, "transit": 1, "green": 1}
        base = self.db.score(lat, lon, 3, weights, 4.8, [])
        cached_before = self.count_cached_routes()
        blocked = self.db.score(lat, lon, 3, weights, 4.8, [hazard])
        self.assertLess(len(blocked["edges"]["features"]), len(base["edges"]["features"]))
        self.assertEqual(self.count_cached_routes(), cached_before)

    def test_only_one_analyze_function_exists(self):
        with psycopg.connect(self.url) as conn:
            count = conn.execute("SELECT count(*) FROM pg_proc WHERE proname = 'analyze' "
                                 "AND pronamespace = 'walkability'::regnamespace").fetchone()[0]
        self.assertEqual(count, 1)

    def test_import_requires_replace_and_clears_cache(self):
        self.db.score(*self.coordinates(), 3.0, {})
        before = self.db.health()["revision"]
        with self.assertRaisesRegex(ValueError, "--replace"):
            import_dataset(self.url, self.directory)
        self.assertEqual(self.db.health()["revision"], before)
        import_dataset(self.url, self.directory, replace=True)
        self.assertNotEqual(self.db.health()["revision"], before)
        with psycopg.connect(self.url) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM walkability.route_cache").fetchone()[0], 0)

    def test_failed_import_rolls_back_existing_dataset_and_cache(self):
        before = self.db.score(*self.coordinates(), 3.0, {})
        revision = self.db.health()["revision"]
        path = self.directory / "stockholm_transit.geojson"
        saved = path.with_suffix(".backup")
        path.rename(saved)
        try:
            with self.assertRaises(FileNotFoundError):
                import_dataset(self.url, self.directory, replace=True)
        finally:
            saved.rename(path)
        self.assertEqual(self.db.health()["revision"], revision)
        self.assertEqual(self.db.score(*self.coordinates(), 3.0, {}), before)

    def test_outside_coverage_and_optional_boundary(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.db.score(58, 17, 15.0, {})
        polygon = box(self.x - 10, self.y - 10, self.x + 50, self.y + 50)
        with psycopg.connect(self.url) as conn:
            conn.execute("UPDATE walkability.dataset SET boundary = ST_GeomFromWKB(%s, 32634)", (polygon.wkb,))
        with self.assertRaisesRegex(ValueError, "outside"):
            self.db.score(*self.coordinates(2), 15.0, {})
        self.assertTrue(self.db.score(*self.coordinates(1), 15.0, {})["isochrone"])

    def test_api_uses_postgresql_without_loading_local_graph(self):
        with patch("api.configured_url", return_value=self.url), patch.dict(api._state, {}, clear=True), \
                patch("osmnx.load_graphml", side_effect=AssertionError("API loaded a local graph")):
            with TestClient(api.app) as client:
                self.assertEqual(client.get("/health").json()["routing"], "pgrouting")
                lat, lon = self.coordinates()
                response = client.post("/score", json={"lat": lat, "lon": lon, "trip_minutes": 3})
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["isochrone"]["type"], "Feature")
                self.assertEqual(len(body["edges"]["features"]), 5)
                self.assertIn("amenity_count", body["factors"]["accessibility"])
                for layer in ("amenities", "greenspace", "transit", "reference-points"):
                    self.assertEqual(client.get(f"/layers/{layer}").status_code, 200)
                self.assertEqual(client.post("/score", json={"lat": 58, "lon": 17}).status_code, 400)
                self.assertEqual(client.post("/score", json={"lat": lat, "lon": lon, "trip_minutes": -1}).status_code, 422)
            self.assertNotIn("database", api._state)


if __name__ == "__main__":
    unittest.main()