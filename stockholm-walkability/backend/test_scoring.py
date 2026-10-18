"""test_scoring.py
Unit tests for the pure scoring logic, elevation sampling and slope costs.
No database, network access or downloads are needed.

Run from the backend directory: python -m unittest test_scoring -v
(The database and API tests live in test_database.py.)
"""

import math
import unittest

import networkx as nx
import numpy as np

import data_prep
from scoring import (
    AccessibilityFactors,
    accessibility_composite,
    calculate_effective_edge_length,
    compute_morphology_factors,
    elevation_summary,
    final_walkability_score,
    isochrone_subgraph,
    z_scores,
)

SPACING = 100.0


def tobler_speed(grade):
    """Tobler's hiking function (1993), km/h."""
    return 6.0 * math.exp(-3.5 * abs(grade + 0.05))


def add_two_way(g, u, v, grade=0.0):
    for a, b, gr in ((u, v, grade), (v, u, -grade)):
        g.add_edge(a, b, length=SPACING, grade=gr,
                   effective_length=calculate_effective_edge_length(SPACING, gr))


def chain(n, up=0.0):
    """Straight two-way street. Moving towards +x has grade `up`."""
    g = nx.MultiDiGraph()
    for i in range(n):
        g.add_node(i, x=i * SPACING, y=0.0)
    for i in range(n - 1):
        add_two_way(g, i, i + 1, up)
    return g


def t_junction():
    g = nx.MultiDiGraph()
    for node, xy in {0: (0, 0), 1: (100, 0), 2: (200, 0), 3: (100, 100)}.items():
        g.add_node(node, x=float(xy[0]), y=float(xy[1]))
    for u, v in ((0, 1), (1, 2), (1, 3)):
        add_two_way(g, u, v)
    return g


def grid(n):
    g = nx.MultiDiGraph()
    for i in range(n):
        for j in range(n):
            g.add_node(i * n + j, x=i * SPACING, y=j * SPACING)
    for i in range(n):
        for j in range(n):
            if i + 1 < n:
                add_two_way(g, i * n + j, (i + 1) * n + j)
            if j + 1 < n:
                add_two_way(g, i * n + j, i * n + j + 1)
    return g


def factors_for(g, origin=0, minutes=15):
    sub = isochrone_subgraph(g, origin, trip_minutes=minutes)
    return compute_morphology_factors(g, origin, iso_subgraph=sub)


class NormalisationTests(unittest.TestCase):
    def test_z_scores_have_zero_mean_and_unit_deviation(self):
        z = z_scores([1, 2, 3, 4, 5])
        self.assertAlmostEqual(float(z.mean()), 0.0)
        self.assertAlmostEqual(float(z.std()), 1.0)

    def test_constant_input_does_not_divide_by_zero(self):
        self.assertTrue(np.all(z_scores([4, 4, 4]) == 0))


class MorphologyTests(unittest.TestCase):
    def test_straight_street_has_no_intersections(self):
        # Two-way arcs must not turn mid-street nodes into intersections.
        self.assertEqual(factors_for(chain(8)).intersection_density, 0.0)

    def test_t_junction_counts_exactly_one_intersection(self):
        # One node with three distinct neighbours; hull is a 10,000 m2 triangle.
        self.assertAlmostEqual(factors_for(t_junction(), origin=0).intersection_density, 100.0)

    def test_grid_is_denser_than_a_street(self):
        self.assertGreater(factors_for(grid(5), origin=12).intersection_density,
                           factors_for(chain(8)).intersection_density)

    def test_average_block_length_matches_spacing(self):
        self.assertAlmostEqual(factors_for(grid(4), origin=5).avg_block_length, SPACING)


class SlopeTests(unittest.TestCase):
    def cost(self, grade, length=100.0):
        return calculate_effective_edge_length(length, grade)

    def test_cost_scales_linearly_with_length(self):
        for g in (-0.2, -0.05, 0.0, 0.1):
            self.assertAlmostEqual(self.cost(g, 200.0), 2 * self.cost(g, 100.0))

    def test_uphill_costs_more_as_it_steepens(self):
        self.assertLess(self.cost(0.0), self.cost(0.05))
        self.assertLess(self.cost(0.05), self.cost(0.10))
        self.assertLess(self.cost(0.10), self.cost(0.20))

    def test_gentle_downhill_is_faster_and_steep_downhill_slower(self):
        self.assertLess(self.cost(-0.05), self.cost(0.0))
        self.assertGreater(self.cost(-0.25), self.cost(-0.05))

    def test_cost_ratios_follow_tobler(self):
        # Ratios are independent of how the flat speed is normalised.
        flat = self.cost(0.0)
        for g in (-0.20, -0.05, 0.05, 0.10, 0.20):
            with self.subTest(grade=g):
                self.assertAlmostEqual(self.cost(g) / flat, tobler_speed(0.0) / tobler_speed(g), places=6)

    def test_missing_grade_leaves_length_unchanged(self):
        self.assertEqual(calculate_effective_edge_length(100.0, float("nan")), 100.0)

    def test_reach_is_smaller_uphill_than_downhill(self):
        g = chain(15, up=0.05)
        uphill = isochrone_subgraph(g, 0, trip_minutes=5)     # walking towards +x, uphill
        downhill = isochrone_subgraph(g, 14, trip_minutes=5)  # walking towards -x, downhill
        self.assertLess(uphill.number_of_nodes(), downhill.number_of_nodes())


class AccessibilityTests(unittest.TestCase):
    reference = [AccessibilityFactors(5, 2, 0.05), AccessibilityFactors(20, 4, 0.20),
                 AccessibilityFactors(40, 6, 0.40), AccessibilityFactors(10, 3, 0.10)]
    shops = AccessibilityFactors(40, 5, 0.05)
    park = AccessibilityFactors(5, 5, 0.40)

    def test_weights_can_flip_the_ranking(self):
        amenity_first = {"amenity": 1.0, "transit": 0.0, "green": 0.0}
        green_first = {"amenity": 0.0, "transit": 0.0, "green": 1.0}
        self.assertGreater(accessibility_composite(amenity_first, self.shops, self.reference),
                           accessibility_composite(amenity_first, self.park, self.reference))
        self.assertGreater(accessibility_composite(green_first, self.park, self.reference),
                           accessibility_composite(green_first, self.shops, self.reference))

    def test_final_score_blend(self):
        self.assertAlmostEqual(final_walkability_score(1.0, 0.0), 0.7)
        self.assertAlmostEqual(final_walkability_score(0.0, 1.0), 0.3)
        self.assertAlmostEqual(final_walkability_score(1.0, 3.0, accessibility_weight=0.5), 2.0)


class ElevationSummaryTests(unittest.TestCase):
    def test_summary_ignores_missing_values(self):
        s = elevation_summary([10, 20, None, float("nan"), 30], origin=20)
        self.assertTrue(s["available"])
        self.assertEqual((s["min"], s["max"], s["mean"], s["relief"], s["origin"]), (10.0, 30.0, 20.0, 20.0, 20.0))
        self.assertEqual(len(s["histogram"]["share"]), 12)
        self.assertAlmostEqual(sum(s["histogram"]["share"]), 1.0)

    def test_no_data_is_reported_as_unavailable(self):
        self.assertEqual(elevation_summary([None, float("nan")]), {"available": False})

    def test_flat_area_has_zero_relief(self):
        s = elevation_summary([12.0, 12.0, 12.0])
        self.assertEqual(s["relief"], 0.0)
        self.assertAlmostEqual(sum(s["histogram"]["share"]), 1.0)

    def test_invalid_origin_becomes_none(self):
        self.assertIsNone(elevation_summary([1, 2], origin=float("nan"))["origin"])


class RasterSamplingTests(unittest.TestCase):
    arr = np.array([[0.0, 10.0], [20.0, 30.0]])

    def sample(self, col, row):
        return data_prep._bilinear(self.arr, np.array([col], dtype=float), np.array([row], dtype=float))[0]

    def test_centre_is_the_mean_of_the_four_pixels(self):
        self.assertAlmostEqual(self.sample(0.5, 0.5), 15.0)

    def test_pixel_centres_return_their_own_value(self):
        self.assertAlmostEqual(self.sample(0.0, 0.0), 0.0)
        self.assertAlmostEqual(self.sample(1.0, 1.0), 30.0)

    def test_outside_the_raster_is_nan_not_zero(self):
        self.assertTrue(math.isnan(self.sample(3.0, 0.0)))

    def test_half_pixel_border_is_tolerated(self):
        self.assertAlmostEqual(self.sample(-0.4, 0.0), 0.0)

    def test_nodata_neighbour_gives_nan(self):
        arr = self.arr.copy()
        arr[1, 1] = np.nan
        out = data_prep._bilinear(arr, np.array([0.5]), np.array([0.5]))
        self.assertTrue(math.isnan(out[0]))


class EdgeAttributeTests(unittest.TestCase):
    def test_grades_are_clipped_and_missing_elevation_is_flat(self):
        g = nx.MultiDiGraph()
        for node, elevation in {1: 0.0, 2: 5.0, 3: float("nan"), 4: 100.0}.items():
            g.add_node(node, elevation=elevation)
        g.add_edge(1, 2, length=100.0)
        g.add_edge(2, 1, length=100.0)
        g.add_edge(1, 3, length=100.0)
        g.add_edge(1, 4, length=10.0)
        data_prep.finalize_edge_attributes(g)
        grade = {(u, v): d["grade"] for u, v, d in g.edges(data=True)}
        self.assertAlmostEqual(grade[(1, 2)], 0.05)
        self.assertAlmostEqual(grade[(2, 1)], -0.05)
        self.assertEqual(grade[(1, 3)], 0.0)
        self.assertEqual(grade[(1, 4)], data_prep.MAX_GRADE)
        for u, v, d in g.edges(data=True):
            self.assertAlmostEqual(d["effective_length"], calculate_effective_edge_length(d["length"], d["grade"]))


if __name__ == "__main__":
    unittest.main()