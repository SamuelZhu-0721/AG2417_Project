"""
api.py
FastAPI application for the Stockholm Walkability Explorer API.
"""

from __future__ import annotations

import os
import logging
import math
from typing import Dict, List, Any

import networkx as nx
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from database import Database, configured_url
import psycopg
from psycopg_pool import PoolTimeout
from pyproj import Transformer
from shapely.geometry import Point, LineString, MultiPoint, mapping, shape
import geopandas as gpd
import json

from scoring import (
    AccessibilityFactors,
    MorphologyFactors,
    accessibility_composite,
    compute_morphology_factors,
    final_walkability_score,
    isochrone_subgraph,
    morphology_composite,
    calculate_effective_edge_length,
    elevation_summary,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

app = FastAPI(title="Stockholm Walkability Explorer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_state: Dict[str, object] = {}

def clean_gdf_for_json(gdf):
    for col in gdf.columns:
        if col != "geometry":
            gdf[col] = gdf[col].apply(lambda x: str(x) if isinstance(x, pd.Timestamp) else x)
    return gdf

@app.on_event("startup")
def load_data():
    previous = _state.get("database")
    if previous is not None:
        previous.close()
    _state.clear()
    boundary_path = os.path.join(DATA_DIR, "stockholm_boundary.geojson")
    if os.path.exists(boundary_path):
        with open(boundary_path, encoding="utf-8") as f:
            _state["boundary"] = shape(json.load(f)["features"][0]["geometry"])
    url = configured_url()
    if url:
        _state["database"] = Database(url).open()
        print("API ready with PostgreSQL, PostGIS and pgRouting.")
        return
    import osmnx as ox
    graph_path = os.path.join(DATA_DIR, "stockholm_network.graphml")
    ref_path = os.path.join(DATA_DIR, "reference_scores.csv")
    amenities_path = os.path.join(DATA_DIR, "stockholm_amenities.geojson")
    greenspace_path = os.path.join(DATA_DIR, "stockholm_greenspace.geojson")
    transit_path = os.path.join(DATA_DIR, "stockholm_transit.geojson")

    if not os.path.exists(graph_path) or not os.path.exists(ref_path):
        raise RuntimeError("Missing precomputed data. Run `python3 data_prep.py` first.")

    print("Loading street network & datasets ...")
    G = ox.load_graphml(graph_path)
    
    for _, _, d in G.edges(data=True):
        length = float(d.get("length", 1.0))
        grade = float(d.get("grade", 0.0)) if d.get("grade") is not None and not pd.isna(d.get("grade")) else 0.0
        d["length"] = length
        d["grade"] = grade
        if "effective_length" not in d:
            d["effective_length"] = calculate_effective_edge_length(length, grade)
        else:
            d["effective_length"] = float(d["effective_length"])
            
    _state["graph"] = G

    graph_crs = G.graph["crs"]
    _state["to_graph_crs"] = Transformer.from_crs("EPSG:4326", graph_crs, always_xy=True)
    _state["to_wgs84"] = Transformer.from_crs(graph_crs, "EPSG:4326", always_xy=True)

    if os.path.exists(amenities_path):
        amenities_wgs = gpd.read_file(amenities_path).to_crs("EPSG:4326")
        _state["amenities_json"] = json.loads(clean_gdf_for_json(amenities_wgs).to_json())
        _state["amenities_proj"] = amenities_wgs.to_crs(graph_crs)

    if os.path.exists(greenspace_path):
        greenspace_wgs = gpd.read_file(greenspace_path).to_crs("EPSG:4326")
        _state["greenspace_json"] = json.loads(clean_gdf_for_json(greenspace_wgs).to_json())
        _state["greenspace_proj"] = greenspace_wgs.to_crs(graph_crs)

    if os.path.exists(transit_path):
        transit_wgs = gpd.read_file(transit_path).to_crs("EPSG:4326")
        _state["transit_json"] = json.loads(clean_gdf_for_json(transit_wgs).to_json())
        _state["transit_proj"] = transit_wgs.to_crs(graph_crs)

    print("Loading reference dataset ...")
    ref_df = pd.read_csv(ref_path)
    _state["reference"] = ref_df
    
    if "x" in ref_df.columns and "y" in ref_df.columns:
        ref_gdf = gpd.GeoDataFrame(
            ref_df, 
            geometry=gpd.points_from_xy(ref_df.x, ref_df.y), 
            crs=graph_crs
        )
        ref_gdf_wgs84 = ref_gdf.to_crs("EPSG:4326")
        _state["reference_json"] = json.loads(clean_gdf_for_json(ref_gdf_wgs84).to_json())

    _state["morph_ref"] = [
        MorphologyFactors(row.intersection_density, row.integration, row.avg_block_length)
        for row in ref_df.itertuples()
    ]
    _state["access_ref"] = [
        AccessibilityFactors(row.amenity_count, row.transit_stop_score, row.green_space_ratio)
        for row in ref_df.itertuples()
    ]
    print("API ready with consistent boundary spatial layers loaded.")

@app.on_event("shutdown")
def close_data():
    database = _state.pop("database", None)
    if database is not None:
        database.close()


class ScoreRequest(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    weights: Dict[str, float] = Field(default_factory=lambda: {"amenity": 1 / 3, "transit": 1 / 3, "green": 1 / 3})
    trip_minutes: float = Field(default=15.0, gt=0, le=120, allow_inf_nan=False)
    walking_speed_kmh: float = Field(default=4.8, gt=0, le=10.0, allow_inf_nan=False)
    exclusion_polygons: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("weights")
    @classmethod
    def validate_weights(cls, weights):
        if set(weights) - {"amenity", "transit", "green"}:
            raise ValueError("Unknown accessibility weight")
        if any(not math.isfinite(v) or v < 0 for v in weights.values()):
            raise ValueError("Weights must be finite and nonnegative")
        return weights

class ScoreResponse(BaseModel):
    morphology_score: float
    accessibility_score: float
    final_score: float
    isochrone: dict
    edges: dict
    factors: dict
    elevation_profile: dict

def get_subgraph_edges_geojson(G_sub):
    edges = []
    for u, v, data in G_sub.edges(data=True):
        if "geometry" in data:
            geom = data["geometry"]
        else:
            point_u = (G_sub.nodes[u]["x"], G_sub.nodes[u]["y"])
            point_v = (G_sub.nodes[v]["x"], G_sub.nodes[v]["y"])
            geom = LineString([point_u, point_v])

        edges.append({
            "geometry": geom,
            "length": data.get("length", 0),
            "grade": data.get("grade", 0.0),
            "highway": str(data.get("highway", "residential")),
        })

    if not edges:
        return {"type": "FeatureCollection", "features": []}

    gdf_edges = gpd.GeoDataFrame(edges, crs=G_sub.graph.get("crs", "EPSG:4326"))
    if gdf_edges.crs != "EPSG:4326":
        gdf_edges = gdf_edges.to_crs("EPSG:4326")

    return json.loads(gdf_edges.to_json())

@app.post("/score", response_model=ScoreResponse)
def score_point(req: ScoreRequest):
    boundary = _state.get("boundary")
    if boundary is not None and not boundary.covers(Point(req.lon, req.lat)):
        raise HTTPException(status_code=400, detail="Point is outside Stockholm Municipality.")
    if "database" in _state:
        try:
            return ScoreResponse(**_state["database"].score(
                req.lat, req.lon, req.trip_minutes, req.weights, req.walking_speed_kmh, req.exclusion_polygons
            ))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (psycopg.Error, PoolTimeout, RuntimeError) as exc:
            logging.getLogger(__name__).exception("PostgreSQL scoring failed")
            raise HTTPException(status_code=503, detail="Database scoring is temporarily unavailable.") from exc
    import osmnx as ox
    G: nx.Graph = _state["graph"]
    graph_crs = G.graph["crs"]

    try:
        proj_x, proj_y = _state["to_graph_crs"].transform(req.lon, req.lat)
        node = ox.distance.nearest_nodes(G, X=proj_x, Y=proj_y)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not locate point: {exc}")

    iso = isochrone_subgraph(
        G, node, trip_minutes=req.trip_minutes, walking_speed_kmh=req.walking_speed_kmh, exclusion_polygons=req.exclusion_polygons
    )
    iso_nodes = list(iso.nodes())

    morph_factors = compute_morphology_factors(G, node, iso_subgraph=iso)
    morph_score = morphology_composite(morph_factors, _state["morph_ref"])

    point_geoms = [
        Point(_state["to_wgs84"].transform(G.nodes[n]["x"], G.nodes[n]["y"]))
        for n in iso_nodes
    ]
    reach_polygon_wgs84 = MultiPoint(point_geoms).convex_hull if point_geoms else Point(req.lon, req.lat).buffer(0.0001)
    edges_geojson = get_subgraph_edges_geojson(iso)

    elevation_profile = elevation_summary(
        [iso.nodes[n].get("elevation") for n in iso_nodes], G.nodes[node].get("elevation"))

    poly_series = gpd.GeoSeries([reach_polygon_wgs84], crs="EPSG:4326").to_crs(graph_crs)
    reach_poly_proj = poly_series.iloc[0]

    amenity_count = 0
    if "amenities_proj" in _state and reach_poly_proj.area > 0:
        am_proj = _state["amenities_proj"]
        amenity_count = int(am_proj.geometry.within(reach_poly_proj).sum())

    green_ratio = 0.0
    if "greenspace_proj" in _state and reach_poly_proj.area > 0:
        gs_proj = _state["greenspace_proj"]
        intersection_area = gs_proj.geometry.intersection(reach_poly_proj).area.sum()
        green_ratio = float(min(intersection_area / reach_poly_proj.area, 1.0))

    transit_score = 0.0
    if "transit_proj" in _state and reach_poly_proj.area > 0:
        tr_proj = _state["transit_proj"]
        reachable_stops = tr_proj[tr_proj.geometry.within(reach_poly_proj)]
        transit_score = float(reachable_stops["departures_per_hour"].sum() if "departures_per_hour" in reachable_stops.columns else len(reachable_stops) * 4)

    access_factors = AccessibilityFactors(
        amenity_count=float(amenity_count),
        transit_stop_score=transit_score,
        green_space_ratio=green_ratio,
    )
    
    access_score = accessibility_composite(req.weights, access_factors, _state["access_ref"])
    final_score = final_walkability_score(access_score, morph_score)

    isochrone_feature = {
        "type": "Feature",
        "geometry": mapping(reach_polygon_wgs84),
        "properties": {
            "final_score": final_score,
            "morphology_score": morph_score,
            "accessibility_score": access_score,
        }
    }

    return ScoreResponse(
        morphology_score=morph_score,
        accessibility_score=access_score,
        final_score=final_score,
        isochrone=isochrone_feature,
        edges=edges_geojson,
        factors={
            "morphology": {
                "intersection_density": morph_factors.intersection_density,
                "integration": morph_factors.integration,
                "avg_block_length": morph_factors.avg_block_length,
            },
            "accessibility": {
                "amenity_count": access_factors.amenity_count,
                "transit_stop_score": access_factors.transit_stop_score,
                "green_space_ratio": access_factors.green_space_ratio,
            },
        },
        elevation_profile=elevation_profile,
    )

def get_layer(name, file_key):
    if "database" in _state:
        try:
            return _state["database"].layer(name)
        except (psycopg.Error, PoolTimeout, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="Database layers are temporarily unavailable.") from exc
    return _state.get(file_key, {"type": "FeatureCollection", "features": []})


@app.get("/layers/boundary")
def get_boundary():
    path = os.path.join(DATA_DIR, "stockholm_boundary.geojson")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Boundary missing. Run data_prep.py")
    with open(path, encoding="utf-8") as f:
        return json.load(f)

@app.get("/layers/amenities")
def get_amenities():
    return get_layer("amenities", "amenities_json")

@app.get("/layers/greenspace")
def get_greenspace():
    return get_layer("greenspace", "greenspace_json")

@app.get("/layers/transit")
def get_transit():
    return get_layer("transit", "transit_json")

@app.get("/layers/reference-points")
def get_reference_points():
    return get_layer("reference-points", "reference_json")

@app.get("/health")
def health():
    if "database" in _state:
        try:
            return _state["database"].health()
        except (psycopg.Error, PoolTimeout, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="Database is unavailable.") from exc
    return {"status": "ok", "storage": "files", "routing": "networkx",
            "nodes_loaded": _state["graph"].number_of_nodes() if "graph" in _state else 0}