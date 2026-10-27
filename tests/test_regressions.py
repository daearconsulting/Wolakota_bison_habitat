import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from rasterio.transform import array_bounds
from sitekit.config import has_demo
from sitekit.fetch import make_grid
from sitekit.validation import validate, validate_drought

class RegressionTests(unittest.TestCase):
    def test_grid_preserves_requested_extent(self):
        bounds = (-105.6, 39.4, -104.5, 41.0)
        transform, shape, spacing = make_grid(bounds, 90)
        np.testing.assert_allclose(array_bounds(*shape, transform), bounds, atol=1e-10)
        self.assertTrue(all(x > 0 for x in spacing))

    def test_mixed_provenance_is_demo(self):
        m = {"mode": "real", "datasets": {"rap": {"status": "demo"}}}
        self.assertTrue(has_demo(m))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "manifest.json").write_text(json.dumps(m))
            with self.assertRaisesRegex(ValueError, "Synthetic"):
                validate(p)

    def test_empty_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "manifest.json").write_text("{}")
            with self.assertRaises(ValueError): validate(p)

    def test_success_without_files_rejected(self):
        from datetime import datetime, timezone
        m = {"mode":"real", "datasets":{"area":{"status":"ok", "files":[], "retrieved_utc":datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}}}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p / "manifest.json").write_text(json.dumps(m))
            with self.assertRaisesRegex(ValueError, "output files"): validate(p)

    def test_cumulative_or_missing_drought_rejected(self):
        frame=pd.DataFrame({"date":["2025-01-01"],"county":["A"],"none":[0],"d0":[100],"d1":[50],"d2":[20],"d3":[0],"d4":[0]})
        with self.assertRaises(ValueError): validate_drought(frame)
        frame["d0"]=30
        validate_drought(frame)
        with self.assertRaises(ValueError): validate_drought(frame.drop(columns="d4"))

    def test_printable_raster_map(self):
        from sitekit import show
        import folium
        import geopandas as gpd
        from shapely.geometry import box
        with patch.object(show, "area", return_value=gpd.GeoDataFrame(geometry=[box(-101,43,-100,44)], crs="EPSG:4326")):
            m=folium.Map()
            folium.raster_layers.ImageOverlay(show._png(np.ones((3,3)),"YlGn",0,2), bounds=[[43,-101],[44,-100]],opacity=0.7).add_to(m)
            self.assertIn("data:image/png;base64", show.static_map(m))

    def test_failed_refresh_keeps_previous_data(self):
        from sitekit import transaction
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            processed=root/"data"/"processed"
            processed.mkdir(parents=True)
            (processed/"sentinel").write_text("known working")
            with patch.object(transaction,"ROOT",root), patch.object(transaction,"PROCESSED",processed), patch.dict("os.environ", {}, clear=True), patch("sys.argv", ["build_data.py"]), patch.object(transaction.subprocess,"run") as run:
                run.return_value.returncode=1
                self.assertEqual(transaction.run(lambda: 0),1)
            self.assertEqual((processed/"sentinel").read_text(),"known working")

if __name__ == "__main__": unittest.main()


class WaterAndFireTests(unittest.TestCase):
    def test_wrong_waterbody_geometry_is_rejected(self):
        import geopandas as gpd
        from shapely.geometry import box, LineString
        from sitekit import fetch
        area=gpd.GeoDataFrame(geometry=[box(-101,43,-100,44)],crs="EPSG:4326")
        lines=gpd.GeoDataFrame({"FCode":[46006]},geometry=[LineString([(-100.9,43.1),(-100.8,43.2)])],crs="EPSG:4326")
        with patch.object(fetch,"_nhd_query",return_value=lines):
            with self.assertRaisesRegex(ValueError,"polygons"):
                fetch.build_water({"rap":{"map_resolution_m":90}},area,{})

    def test_null_fire_years_can_be_grouped(self):
        frame=pd.DataFrame({"year":[2020,None],"acres_in_area":[10,20]})
        from sitekit.show import fire_year_totals
        self.assertEqual(fire_year_totals(frame).loc[2020],10)


class OptionalWaterTests(unittest.TestCase):
    def run_builder(self, error):
        import runpy, requests
        from sitekit import fetch, config
        script = Path(__file__).resolve().parents[1] / "scripts/build_data.py"
        captured = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "water_distance_km.tif").write_text("old partial data")
            with patch.object(config, "PROCESSED", path), patch.object(config, "write_manifest", side_effect=lambda m: captured.append(json.loads(json.dumps(m)))), patch.object(fetch, "build_area"), patch.object(fetch, "build_rap"), patch.object(fetch, "build_drought"), patch.object(fetch, "build_fire"), patch.object(fetch, "build_water", side_effect=error), patch("sys.argv", [str(script)]):
                main = runpy.run_path(str(script))["main"]
                result = main()
            return result, captured[-1], (path / "water_distance_km.tif").exists()

    def test_usgs_504_is_explicitly_unavailable_and_removes_partial_files(self):
        import requests
        response = requests.Response()
        response.status_code = 504
        result, manifest, leftover = self.run_builder(requests.HTTPError("504 Gateway Time-out", response=response))
        self.assertEqual(result, 0)
        self.assertEqual(manifest["datasets"]["water"]["status"], "unavailable")
        self.assertIn("504", manifest["datasets"]["water"]["note"])
        self.assertFalse(leftover)

    def test_water_processing_errors_still_fail(self):
        result, manifest, _ = self.run_builder(ValueError("Waterbody service must return polygons"))
        self.assertEqual(result, 1)
        self.assertEqual(manifest["datasets"]["water"]["status"], "failed")

    def test_optional_water_publication_contract(self):
        from sitekit import demo, config
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = {"datasets": {}}
            with patch.object(demo, "PROCESSED", directory):
                demo.build_demo(config.load_site(), manifest)
            for name in manifest["datasets"]["water"]["files"]:
                (directory / name).unlink()
            config.record(manifest, "water", status="unavailable", source="USGS", url="https://hydro.nationalmap.gov/", note="HTTP 504; omitted")
            (directory / "manifest.json").write_text(json.dumps(manifest))
            validate(directory, allow_demo=True)
            with self.assertRaisesRegex(ValueError, "Synthetic"):
                validate(directory)
            (directory / "water_distance_km.tif").write_text("leftover")
            with self.assertRaisesRegex(ValueError, "old or partial"):
                validate(directory, allow_demo=True)


    def test_http_404_is_not_treated_as_a_temporary_outage(self):
        import requests
        response = requests.Response()
        response.status_code = 404
        result, manifest, _ = self.run_builder(requests.HTTPError("404 Not Found", response=response))
        self.assertEqual(result, 1)
        self.assertEqual(manifest["datasets"]["water"]["status"], "failed")


class MapRecoveryTests(unittest.TestCase):
    def test_tiled_hydrography_deduplicates_ids(self):
        from sitekit import fetch
        from unittest.mock import Mock
        calls = []
        def request(url, params, timeout):
            calls.append(params)
            response = Mock()
            if params.get("returnIdsOnly"):
                response.json.return_value = {"objectIds": [7, 8]}
            else:
                self.assertNotIn("geometry", params)
                self.assertEqual(params["objectIds"], "7,8")
                response.json.return_value = {"features": [
                    {"type": "Feature", "properties": {"OBJECTID": i, "FCode": 46006},
                     "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}
                    for i in [7, 8]]}
            return response
        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch, "CACHE", Path(tmp)), patch.object(fetch.requests, "get", side_effect=request):
            result = fetch._nhd_query("https://example.test/query", (0, 0, 1, 1))
        self.assertEqual(len(result), 2)
        self.assertEqual(sum(bool(c.get("returnIdsOnly")) for c in calls), 16)

    def test_incomplete_hydrography_response_is_rejected(self):
        from sitekit import fetch
        from unittest.mock import Mock
        response = Mock()
        response.json.return_value = {}
        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch, "CACHE", Path(tmp)), patch.object(fetch.requests, "get", return_value=response):
            with self.assertRaisesRegex(ValueError, "Incomplete NHD"):
                fetch._nhd_query("https://example.test/query", (0, 0, 1, 1))

    def test_mtbs_years_deduplication_and_clipped_acreage(self):
        from sitekit import fetch
        import geopandas as gpd
        from shapely.geometry import box
        frame = gpd.GeoDataFrame({"FIRE_ID": ["a", "a"], "YEAR": [2012, 2012],
                                  "FIRE_NAME": ["Test", "Test"]},
                                 geometry=[box(-101,43,-100,44)]*2, crs="EPSG:4326")
        area = gpd.GeoDataFrame(geometry=[box(-101,43,-100.5,43.5)], crs="EPSG:4326")
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {}
            with patch.object(fetch, "PROCESSED", Path(tmp)), patch.object(fetch, "_arcgis_query", return_value=frame):
                fetch.build_fire(area, manifest)
            result = gpd.read_file(Path(tmp)/"fire_perimeters.geojson")
            self.assertEqual(len(result), 1)
            self.assertEqual(result.year.iloc[0], 2012)
            self.assertEqual(result["name"].iloc[0], "Test")
            self.assertAlmostEqual(result.acres_in_area.iloc[0], area.to_crs(fetch.CRS_AREA).area.iloc[0]/4046.86)
            self.assertIn("MTBS", manifest["datasets"]["fire"]["source"])


class HydrographyGeometryTests(unittest.TestCase):
    def test_invalid_source_polygon_is_repaired_before_clipping(self):
        from sitekit import fetch
        import geopandas as gpd
        import rasterio
        from shapely.geometry import box, Polygon, LineString
        area = gpd.GeoDataFrame(geometry=[box(-101,43,-100.9,43.1)], crs="EPSG:4326")
        streams = gpd.GeoDataFrame({"FCode": [46006]}, geometry=[LineString([(-100.99,43.01),(-100.91,43.09)])], crs=area.crs)
        ponds = gpd.GeoDataFrame(geometry=[Polygon([(-100.98,43.02),(-100.92,43.08),(-100.98,43.08),(-100.92,43.02),(-100.98,43.02)])], crs=area.crs)
        self.assertFalse(ponds.geometry.is_valid.all())
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(fetch, "PROCESSED", Path(tmp)), patch.object(fetch, "_nhd_query", side_effect=[streams, ponds]):
                fetch.build_water({"rap": {"map_resolution_m": 240}}, area, {})
            result = gpd.read_file(Path(tmp)/"water_bodies.geojson")
            self.assertTrue(len(result))
            self.assertTrue(result.geometry.is_valid.all())
            with rasterio.open(Path(tmp)/"water_distance_km.tif") as src:
                self.assertTrue(np.isfinite(src.read(1, masked=True).compressed()).all())
