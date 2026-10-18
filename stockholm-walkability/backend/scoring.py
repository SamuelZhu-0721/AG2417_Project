"""Core scoring functions for the Stockholm Walkability Explorer."""

from __future__ import annotations
import pandas as pd
import math
from dataclasses import dataclass
import networkx as nx
import numpy as np
from shapely.geometry import shape

DEFAULT_WALK_SPEED_KMH = 4.8


def z_scores(values) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    mean = arr.mean()
    std = arr.std(ddof=0)
    if std == 0:
        return np.zeros_like(arr)
    return (arr - mean) / std


def min_max_scale(values, out_min: float = 0.0, out_max: float = 100.0) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    lo, hi = arr.min(), arr.max()
    if hi == lo:
        return np.full_like(arr, (out_min + out_max) / 2)
    return out_min + (arr - lo) * (out_max - out_min) / (hi - lo)


def local_subgraph(G: nx.Graph, center_node, radius_m: float) -> nx.Graph:
    return nx.ego_graph(G, center_node, radius=radius_m, distance="length")


def calculate_effective_edge_length(length: float, grade: float) -> float:
    if grade is None or pd.isna(grade):
        return length
    
    # Grade is rise/run (e.g., 0.05 for a 5% uphill slope)
    # Tobler's hiking function gives speed in km/h: W = 6 * exp(-3.5 * abs(grade + 0.05))
    slope_speed = 6.0 * math.exp(-3.5 * abs(grade + 0.05))
    
    # Prevent division by zero or near-zero speeds on extreme slopes
    if slope_speed <= 0.05:
        slope_speed = 0.05
        
    # Base flat speed used across your application model (4.8 km/h)
    base_speed = 4.8
    
    # Effective length scales the physical distance based on speed reduction/increase
    # Time = Distance / Speed -> Effective Distance = Physical Distance * (Base Speed / Slope Speed)
    speed_ratio = base_speed / slope_speed
    
    return length * speed_ratio


def get_slope_weighted_length(u, v, data):
    length = data.get("length", 1.0)
    grade = data.get("grade", 0.0)
    return calculate_effective_edge_length(length, grade)


def isochrone_subgraph(G: nx.Graph, source_node: int, trip_minutes: float = 15.0, walking_speed_kmh: float = DEFAULT_WALK_SPEED_KMH, exclusion_polygons: list = None):
    metres_per_minute = (walking_speed_kmh * 1000.0) / 60.0
    max_metres = trip_minutes * metres_per_minute

    exclusion_geoms = []
    if exclusion_polygons:
        for poly in exclusion_polygons:
            try:
                geom = shape(poly.get("geometry", poly))
                if not geom.is_empty:
                    exclusion_geoms.append(geom)
            except Exception:
                pass

    def weight_func(u, v, d):
        # MultiDiGraph passes {edge_key: attributes}; DiGraph passes attributes directly.
        parallel = d.values() if G.is_multigraph() else [d]
        costs = []
        for attrs in parallel:
            geom = attrs.get("geometry")
            if geom is not None and any(geom.intersects(p) for p in exclusion_geoms):
                continue                      # this arc is blocked
            costs.append(get_slope_weighted_length(u, v, attrs))
        return min(costs) if costs else None  # None hides the edge from Dijkstra

    path_lengths = nx.single_source_dijkstra_path_length(
        G, 
        source_node, 
        cutoff=max_metres, 
        weight=weight_func
    )
    
    return G.subgraph(path_lengths.keys()).copy()


@dataclass
class MorphologyFactors:
    intersection_density: float
    integration: float
    avg_block_length: float


def compute_morphology_factors(G: nx.Graph, center_node, iso_subgraph: nx.Graph | None = None) -> MorphologyFactors:
    sub = iso_subgraph if iso_subgraph is not None else local_subgraph(G, center_node, radius_m=400.0)

    if sub.number_of_nodes() == 0:
        return MorphologyFactors(0.0, 0.0, 0.0)

    undirected_sub = nx.Graph(sub)
    intersections = sum(1 for _, d in undirected_sub.degree() if d >= 3)
    
    pts = [(sub.nodes[n]["x"], sub.nodes[n]["y"]) for n in sub.nodes() if "x" in sub.nodes[n] and "y" in sub.nodes[n]]
    if len(pts) >= 3:
        from shapely.geometry import MultiPoint
        area_m2 = MultiPoint(pts).convex_hull.area
        area_km2 = area_m2 / 1_000_000.0 if area_m2 > 0 else math.pi * (0.4 ** 2)
    else:
        area_km2 = math.pi * (0.4 ** 2)
        
    intersection_density = intersections / area_km2 if area_km2 > 0 else 0.0

    lengths = [d.get("length", 0.0) for _, _, d in sub.edges(data=True)]
    avg_block_length = float(np.mean(lengths)) if lengths else 0.0

    if sub.number_of_nodes() > 1:
        depths = nx.single_source_dijkstra_path_length(sub, center_node, weight="length")
        other_depths = [d for node, d in depths.items() if node != center_node]
        mean_depth = float(np.mean(other_depths)) if other_depths else 0.0
        integration = 1.0 / mean_depth if mean_depth > 0 else 0.0
    else:
        integration = 0.0

    return MorphologyFactors(intersection_density, integration, avg_block_length)


def morphology_composite(factors: MorphologyFactors,
                          ref_factors: list[MorphologyFactors],
                          weights: tuple[float, float, float] = (1.0, 1.0, 1.0)) -> float:
    w_id, w_int, w_bl = weights

    ids = [f.intersection_density for f in ref_factors] + [factors.intersection_density]
    integ = [f.integration for f in ref_factors] + [factors.integration]
    inv_bl = [1.0 / f.avg_block_length if f.avg_block_length > 0 else 0.0 for f in ref_factors]
    inv_bl.append(1.0 / factors.avg_block_length if factors.avg_block_length > 0 else 0.0)

    z_id = z_scores(ids)[-1]
    z_int = z_scores(integ)[-1]
    z_bl = z_scores(inv_bl)[-1]

    return float(w_id * z_id + w_int * z_int + w_bl * z_bl)


@dataclass
class AccessibilityFactors:
    amenity_count: float
    transit_stop_score: float
    green_space_ratio: float


def accessibility_composite(user_weights: dict, factors: AccessibilityFactors,
                             ref_factors: list[AccessibilityFactors]) -> float:
    amenity_vals = [f.amenity_count for f in ref_factors] + [factors.amenity_count]
    transit_vals = [f.transit_stop_score for f in ref_factors] + [factors.transit_stop_score]
    green_vals = [f.green_space_ratio for f in ref_factors] + [factors.green_space_ratio]

    z_am = z_scores(amenity_vals)[-1]
    z_tr = z_scores(transit_vals)[-1]
    z_gr = z_scores(green_vals)[-1]

    w = user_weights
    return float(
        w.get("amenity", 1 / 3) * z_am
        + w.get("transit", 1 / 3) * z_tr
        + w.get("green", 1 / 3) * z_gr
    )


def final_walkability_score(accessibility_z: float, morphology_z: float,
                             accessibility_weight: float = 0.7) -> float:
    return accessibility_weight * accessibility_z + (1 - accessibility_weight) * morphology_z


def elevation_summary(elevations, origin=None, bins: int = 12) -> dict:
    arr = np.array([e for e in elevations if e is not None], dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"available": False}
    lo, hi = float(arr.min()), float(arr.max())
    counts, edges = np.histogram(arr, bins=bins, range=(lo, hi if hi > lo else lo + 1.0))
    origin = float(origin) if origin is not None and np.isfinite(origin) else None
    return {
        "available": True, "origin": origin, "min": lo, "max": hi,
        "mean": float(arr.mean()), "relief": hi - lo,
        "histogram": {"edges": [float(e) for e in edges],
                      "share": [float(c) / arr.size for c in counts]},
    }