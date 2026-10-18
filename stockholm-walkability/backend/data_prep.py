"""
data_prep.py
Initialize PostgreSQL, download/cache Stockholm County OSM data, import it, and
build reference scores with PostGIS/pgRouting. Uses local raster output_hh.tif for elevations.
"""

from __future__ import annotations
import os
from pathlib import Path
import dotenv

dotenv.load_dotenv(Path(__file__).parent / ".env")

import geopandas as gpd
from shapely.geometry import Point
import json
import tempfile
import networkx as nx
import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer

from scoring import (
    AccessibilityFactors,
    MorphologyFactors,
    compute_morphology_factors,
    isochrone_subgraph,
    calculate_effective_edge_length,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

STOCKHOLM_PLACE = "Stockholm Municipality, Sweden"
N_REFERENCE_POINTS = 2000


def _atomic_write(path, write):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        write(temporary)
        os.replace(temporary, path)


def _layer_path(layer):
    for extension in (".gpkg", ".geojson"):
        path = Path(DATA_DIR) / f"stockholm_{layer}{extension}"
        if path.exists():
            return path
    return Path(DATA_DIR) / f"stockholm_{layer}.geojson"


def _save_layer(frame, path):
    from database import _json_value
    if Path(path).suffix == ".gpkg":
        _atomic_write(path, lambda temporary: frame.to_file(temporary, driver="GPKG"))
    else:
        document = frame.to_crs("EPSG:4326").to_json(default=_json_value)
        _atomic_write(path, lambda temporary: temporary.write_text(document, encoding="utf-8"))


def get_boundary_polygon(refresh=False):
    import osmnx as ox
    from shapely.geometry import shape
    path = Path(DATA_DIR) / "stockholm_boundary.geojson"
    if path.exists() and not refresh:
        document = json.loads(path.read_text())
        polygon = shape(document["features"][0]["geometry"])
        if polygon.is_empty or not polygon.is_valid:
            raise ValueError("Invalid cached Stockholm boundary; rerun with --refresh")
        return polygon, "EPSG:4326"
    print(f"Fetching official administrative boundary for: {STOCKHOLM_PLACE} ...")
    gdf = ox.geocode_to_gdf(STOCKHOLM_PLACE).to_crs("EPSG:4326")
    poly = gdf.geometry.iloc[0]
    _save_layer(gdf, path)
    return poly, "EPSG:4326"


ELEVATION_METHOD = "bilinear-v1"
MAX_GRADE = 0.25   # cap implausible grades caused by DEM noise / short edges


def _elevation_stamp(tiff_path):
    stat = Path(tiff_path).stat()
    return f"{ELEVATION_METHOD}:{stat.st_size}:{int(stat.st_mtime)}"


def _bilinear(arr, col, row):
    h, w = arr.shape
    inside = (col >= -0.5) & (col <= w - 0.5) & (row >= -0.5) & (row <= h - 0.5)
    col, row = np.clip(col, 0, w - 1), np.clip(row, 0, h - 1)
    c0 = np.minimum(np.floor(col).astype(int), w - 2)
    r0 = np.minimum(np.floor(row).astype(int), h - 2)
    fc, fr = col - c0, row - r0
    out = (arr[r0, c0] * (1 - fc) * (1 - fr) + arr[r0, c0 + 1] * fc * (1 - fr)
           + arr[r0 + 1, c0] * (1 - fc) * fr + arr[r0 + 1, c0 + 1] * fc * fr)
    out[~inside] = np.nan
    return out


def add_local_raster_elevations(G, tiff_path):
    print(f"Sampling elevations (bilinear) from local raster: {tiff_path}...")
    with rasterio.open(tiff_path) as src:
        arr = src.read(1).astype(float)
        if src.nodata is not None:
            arr[arr == src.nodata] = np.nan
        arr[~np.isfinite(arr)] = np.nan
        arr[arr < -9000] = np.nan
        inverse = ~src.transform
        to_raster = Transformer.from_crs(G.graph.get("crs", "EPSG:4326"), src.crs, always_xy=True)
    ids = list(G.nodes)
    xs, ys = to_raster.transform([float(G.nodes[n]["x"]) for n in ids],
                                 [float(G.nodes[n]["y"]) for n in ids])
    col, row = inverse * (np.asarray(xs), np.asarray(ys))
    z = _bilinear(arr, col - 0.5, row - 0.5)
    for n, e in zip(ids, z):
        G.nodes[n]["elevation"] = float(e)
    print(f"  elevation coverage: {np.isfinite(z).mean():.1%} of {len(ids)} nodes")
    G.graph["elevation_stamp"] = _elevation_stamp(tiff_path)
    return G


def finalize_edge_attributes(G):
    for u, v, d in G.edges(data=True):
        length = float(d.get("length") or 1.0)
        try:
            grade = (float(G.nodes[v].get("elevation")) - float(G.nodes[u].get("elevation"))) / length
        except (TypeError, ValueError):
            grade = 0.0
        grade = float(np.clip(grade, -MAX_GRADE, MAX_GRADE)) if np.isfinite(grade) else 0.0
        d["length"], d["grade"] = length, grade
        d["effective_length"] = calculate_effective_edge_length(length, grade)


def download_network(polygon=None, refresh=False):
    import osmnx as ox
    path = Path(DATA_DIR) / "stockholm_network.graphml"
    raster_path = Path(DATA_DIR) / "output_hh.tif"
    if not raster_path.exists():
        raise FileNotFoundError(f"Elevation raster not found: {raster_path}. Copy output_hh.tif into the data directory.")
    if path.exists() and not refresh:
        print(f"Reusing walking network: {path}")
        G = ox.load_graphml(path)
        if G.graph.get("elevation_stamp") != _elevation_stamp(raster_path):
            print("Cached network elevations are missing or outdated; resampling ...")
            G = add_local_raster_elevations(G, raster_path)
            finalize_edge_attributes(G)
            _atomic_write(path, lambda temporary: ox.save_graphml(G, temporary))
        return G
    if polygon is None:
        polygon, _ = get_boundary_polygon(refresh=refresh)

    print("Downloading walking network clipped to boundary polygon ...")
    G = ox.graph_from_polygon(polygon, network_type="walk", simplify=True, retain_all=True)
    G = add_local_raster_elevations(G, raster_path)
    G = ox.project_graph(G)
    finalize_edge_attributes(G)

    print(f"  {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    _atomic_write(path, lambda temporary: ox.save_graphml(G, temporary))
    return G


def download_transit(polygon=None, refresh=False):
    import osmnx as ox
    transit_path = _layer_path("transit")
    if transit_path.exists() and not refresh:
        return gpd.read_file(transit_path)
    if polygon is None:
        polygon, _ = get_boundary_polygon(refresh=refresh)
    print("Downloading public transport stops within boundary...")
    transit_tags = {"highway": "bus_stop", "public_transport": ["platform", "stop_position"], "railway": "station"}
    transit = ox.features_from_polygon(polygon, tags=transit_tags)
    transit = transit[transit.geometry.type == "Point"]
    _save_layer(transit, transit_path)
    return transit


def download_amenities_and_greenspace(polygon=None, refresh=False):
    import osmnx as ox
    definitions = {
        "amenities": ({"shop": True, "amenity": ["cafe", "restaurant", "pharmacy", "school"]}, {"Point"}),
        "greenspace": ({"leisure": ["park", "garden"], "landuse": ["forest", "grass"]}, {"Polygon", "MultiPolygon"}),
    }
    layers = {}
    for layer, (tags, types) in definitions.items():
        path = _layer_path(layer)
        if path.exists() and not refresh:
            layers[layer] = gpd.read_file(path)
            continue
        if polygon is None:
            polygon, _ = get_boundary_polygon(refresh=refresh)
        print(f"Downloading {layer} within boundary ...")
        frame = ox.features_from_polygon(polygon, tags=tags)
        frame = frame[frame.geometry.type.isin(types)]
        _save_layer(frame, path)
        layers[layer] = frame
    return layers["amenities"], layers["greenspace"]


def main(argv=None):
    import argparse
    from database import bootstrap_configuration, dataset_metadata, import_dataset, initialize_database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--reimport", "--replace-database", dest="reimport", action="store_true")
    parser.add_argument("--reference-points", type=int, default=N_REFERENCE_POINTS)
    parser.add_argument("--skip-database", action="store_true")
    args = parser.parse_args(argv)
    
    global DATA_DIR
    if args.data_dir is not None:
        DATA_DIR = str(args.data_dir.resolve())
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    
    print("[1/3] Initializing PostgreSQL database, extensions and tables ...", flush=True)
    url = bootstrap_configuration()
    initialize_database(url)

    print("[2/3] Preparing network and spatial data layers ...", flush=True)
    import osmnx as ox
    ox.settings.cache_folder = str(Path(DATA_DIR).parent / "cache")
    ox.settings.use_cache = not args.refresh
    needs_download = args.refresh or not (Path(DATA_DIR) / "stockholm_network.graphml").exists()
    polygon = get_boundary_polygon(refresh=args.refresh)[0] if needs_download else None
    
    G = download_network(polygon, refresh=args.refresh)
    graph_crs = G.graph["crs"]

    amenities, greenspace = download_amenities_and_greenspace(polygon, refresh=args.refresh)
    amenities = amenities.to_crs(graph_crs)
    greenspace = greenspace.to_crs(graph_crs)

    transit = download_transit(polygon, refresh=args.refresh)
    if transit is not None and not transit.empty:
        transit = transit.to_crs(graph_crs)

    if args.skip_database:
        return

    del G, amenities, greenspace, transit
    print("[3/3] Importing data into PostgreSQL and building reference scores ...", flush=True)
    metadata = import_dataset(url, DATA_DIR, replace=True, reference_points=args.reference_points)
    print(f"Ready: {metadata['nodes_loaded']} nodes, {metadata['edges_loaded']} edges stored.")


if __name__ == "__main__":
    main()