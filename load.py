import pdb
import os
import json
import uuid
import geopandas as gpd
import rasterio
import hashlib
import pandas as pd
from pathlib import Path
import sqlite3
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import boto3
from urllib.parse import urlparse
import tempfile

# Initialize S3 client
s3_client = boto3.client("s3")


class DatabaseLoader:
    def __init__(self, db_connection_string, hand_version_id, nwm_version_id):
        """Initialize database connection with version IDs."""
        self.engine = create_engine(db_connection_string)
        self.hand_version_id = hand_version_id
        self.nwm_version_id = nwm_version_id
        self._initialize_hand_version()

    def _initialize_hand_version(self):
        """Register hand version before processing data"""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO Hand_Versions (hand_version_id)
                    VALUES (:version)
                    ON CONFLICT (hand_version_id) DO NOTHING
                """
                ),
                {"version": self.hand_version_id},
            )

    def is_s3_path(self, path):
        """Check if a path is an S3 URI."""
        return path.startswith("s3://")

    def parse_s3_path(self, s3_uri):
        """Parse S3 URI into bucket and key."""
        parsed = urlparse(s3_uri)
        return parsed.netloc, parsed.path.lstrip("/")

    def list_s3_objects(self, s3_prefix):
        """List all objects in an S3 prefix/path."""
        bucket, prefix = self.parse_s3_path(s3_prefix)
        paginator = s3_client.get_paginator("list_objects_v2")
        page_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix)
        objects = []
        for page in page_iterator:
            if "Contents" in page:
                objects.extend([obj["Key"] for obj in page["Contents"]])
        return objects

    def download_s3_file(self, s3_uri, local_path):
        """Download a file from S3 to local path."""
        bucket, key = self.parse_s3_path(s3_uri)
        s3_client.download_file(bucket, key, local_path)

    def generate_deterministic_uuid(self, input_string):
        """Generate a deterministic UUID from an input string."""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, input_string))

    def _get_catchment_dirs(self, base_dir):
        """Get all catchment directories that are under HUC/branches/."""
        if self.is_s3_path(base_dir):
            # For S3, list all prefixes that could be catchment directories
            objects = self.list_s3_objects(base_dir)
            # Get unique catchment directories under HUC/branches/
            dirs = set()

            for obj in objects:
                parts = obj.split("/")
                # Look for the pattern: 8-digit-HUC/branches/catchment-dir/
                for i, part in enumerate(parts[:-2]):
                    if (
                        part.isdigit()
                        and len(part) == 8
                        and i + 1 < len(parts)
                        and parts[i + 1] == "branches"
                    ):
                        # Get the directory path up to and including the catchment directory
                        catchment_path = "/".join(
                            parts[: i + 3]
                        )  # Include HUC, 'branches', and catchment dir
                        if catchment_path:  # Only add if we have a catchment directory
                            dirs.add(catchment_path)

            # Convert to full S3 paths
            bucket, _ = self.parse_s3_path(base_dir)
            return [f"s3://{bucket}/{d}" for d in dirs]
        else:
            # For local filesystem
            catchment_dirs = []
            for d in os.listdir(base_dir):
                huc_path = os.path.join(base_dir, d)
                # Check if directory is 8 digits
                if os.path.isdir(huc_path) and d.isdigit() and len(d) == 8:
                    # Check for branches directory
                    branches_path = os.path.join(huc_path, "branches")
                    if os.path.isdir(branches_path):
                        # Add all subdirectories under branches
                        catchment_dirs.extend(
                            [
                                os.path.join(branches_path, cd)
                                for cd in os.listdir(branches_path)
                                if os.path.isdir(os.path.join(branches_path, cd))
                            ]
                        )
            return catchment_dirs

    def _get_files(self, directory, extension, pattern):
        """Get all files matching extension and pattern in directory."""
        if self.is_s3_path(directory):
            objects = self.list_s3_objects(directory)
            return [
                f"s3://{self.parse_s3_path(directory)[0]}/{key}"
                for key in objects
                if key.endswith(extension) and pattern in key
            ]
        else:
            return [
                os.path.join(directory, f)
                for f in os.listdir(directory)
                if f.endswith(extension) and pattern in f
            ]

    def _download_if_s3(self, file_path, tmp_dir):
        """Download file to temporary directory if it's an S3 path."""
        if self.is_s3_path(file_path):
            local_path = os.path.join(tmp_dir, os.path.basename(file_path))
            self.download_s3_file(file_path, local_path)
            return local_path
        return file_path

    def _merge_geometries(self, existing_geom, new_gdf):
        """Merge geometries from a GeoDataFrame."""
        if existing_geom is None:
            return new_gdf.union_all()
        return existing_geom.union(new_gdf.union_all())

    def _read_csv(self, csv_path):
        """Read CSV file from local or S3 path."""
        if self.is_s3_path(csv_path):
            with tempfile.NamedTemporaryFile() as tmp:
                self.download_s3_file(csv_path, tmp.name)
                return pd.read_csv(tmp.name)
        return pd.read_csv(csv_path)

    def load_general_data(self, general_dir):
        """Load data from the general directory (local or S3)."""
        print("Loading data from general directory...")

        # List all objects/files in the general directory
        if self.is_s3_path(general_dir):
            files = [
                f"s3://{self.parse_s3_path(general_dir)[0]}/{key}"
                for key in self.list_s3_objects(general_dir)
            ]
        else:
            files = [os.path.join(general_dir, f) for f in os.listdir(general_dir)]

        # Process NWM lakes
        self.load_nwm_lakes([f for f in files if "nwm_lakes" in f][0])

        # Process HUCs
        self.load_hucs([f for f in files if "WBD_National" in f][0])

        # Process Levees
        self.load_levees([f for f in files if "Levee_protected_areas" in f][0])

        # Process NWM features
        self.load_nwm_features([f for f in files if "nwm_flows" in f][0])

    def load_hand_data(self, hand_dir):
        """Main entry point for HAND data loading with error handling"""
        catchment_dirs = self._get_catchment_dirs(hand_dir)
        total = len(catchment_dirs)

        print(f"Found {total} catchments to process")
        for idx, catchment_dir in enumerate(catchment_dirs, 1):
            print(f"\nProcessing catchment {idx}/{total}: {catchment_dir}")
            try:
                with self.engine.begin() as transaction:
                    # All operations for this catchment will be in a single transaction
                    catchment_id = self.load_catchment_geometry(
                        catchment_dir, transaction
                    )
                    if catchment_id is None:
                        print(
                            f"  Skipping catchment {catchment_dir} - No valid geometry found"
                        )
                        continue

                    self.load_hydrotables(catchment_dir, catchment_id, transaction)
                    self.load_rasters(catchment_dir, catchment_id, transaction)

            except Exception as e:
                print(f"  Error processing catchment {catchment_dir}: {str(e)}")
                # Transaction will automatically rollback due to the context manager
                self._cleanup_catchment(catchment_id)
                continue

    def _cleanup_catchment(self, catchment_id):
        """Clean up any data associated with a failed catchment load"""
        if catchment_id is None:
            return

        print(f"  Cleaning up data for catchment {catchment_id}")
        with self.engine.begin() as conn:
            # Delete from all related tables in reverse order of dependencies
            conn.execute(
                text(
                    """
                    DELETE FROM Hydrotables 
                    WHERE catchment_id = :catch_id AND hand_version_id = :hand_id
                """
                ),
                {"catch_id": catchment_id, "hand_id": self.hand_version_id},
            )

            conn.execute(
                text(
                    """
                    DELETE FROM HAND_REM_Rasters 
                    WHERE catchment_id = :catch_id AND hand_version_id = :hand_id
                """
                ),
                {"catch_id": catchment_id, "hand_id": self.hand_version_id},
            )

            conn.execute(
                text(
                    """
                    DELETE FROM Catchments 
                    WHERE catchment_id = :catch_id AND hand_version_id = :hand_id
                """
                ),
                {"catch_id": catchment_id, "hand_id": self.hand_version_id},
            )

    def load_catchment_geometry(self, catchment_dir, transaction):
        """Load and merge catchment geometries with error handling"""
        print("  Loading catchment geometry...")
        gpkg_files = self._get_files(catchment_dir, ".gpkg", "gw_catchments")

        if not gpkg_files:
            print("  No geometry files found")
            return None

        merged_geom = None
        with tempfile.TemporaryDirectory() as tmpdir:
            for gpkg in gpkg_files:
                try:
                    local_path = self._download_if_s3(gpkg, tmpdir)
                    gdf = gpd.read_file(local_path)
                    if not gdf.empty:
                        merged_geom = self._merge_geometries(merged_geom, gdf)
                except Exception as e:
                    print(f"  Error processing geometry file {gpkg}: {str(e)}")
                    continue

        if merged_geom is None:
            return None

        catchment_id = self._generate_uuid(str(merged_geom.wkt))
        transaction.execute(
            text(
                """
                INSERT INTO Catchments (catchment_id, hand_version_id, geometry)
                VALUES (:catch_id, :hand_id, ST_GeomFromText(:geom, 5070))
            """
            ),
            {
                "catch_id": catchment_id,
                "hand_id": self.hand_version_id,
                "geom": merged_geom.wkt,
            },
        )
        return catchment_id

    def load_hydrotables(self, catchment_dir, catchment_id, transaction):
        """Load hydrotable CSVs with proper version handling and error checking"""
        print("  Loading hydrotables...")
        csv_files = self._get_files(catchment_dir, ".csv", "hydroTable_")

        if not csv_files:
            print("  No hydrotable files found")
            return

        for csv_path in csv_files:
            try:
                df = self._read_csv(csv_path)
                self._insert_hydro_records(df, catchment_id, transaction)
            except Exception as e:
                print(f"  Error processing hydrotable {csv_path}: {str(e)}")
                raise

    def _insert_hydro_records(self, df, catchment_id, transaction):
        """Batch insert hydrotable records using transaction"""
        for _, row in df.iterrows():
            transaction.execute(
                text(
                    """
                    INSERT INTO Hydrotables (
                        catchment_id, hand_version_id, HydroID, nwm_version_id,
                        nwm_feature_id, order_id, number_of_cells, surface_area_m2,
                        bed_area_m2, top_width_m, length_km, area_sq_km,
                        wetted_perimeter_m, hydraulic_radius_m, wet_area_m2,
                        volume_m3, slope, manning_n, stage, discharge_cms,
                        default_discharge_cms, default_volume_m3, default_wet_area_m2,
                        default_hydraulic_radius_m, default_manning_n,
                        bathymetry_source, subdiv_applied, overbank_n, channel_n,
                        subdiv_discharge_cms, calb_applied, lake_id
                    ) VALUES (
                        :catch_id, :hand_id, :hydro_id, :nwm_ver, :feat_id,
                        :order_id, :num_cells, :surf_area, :bed_area, :top_width,
                        :length, :area, :wet_perim, :hydraulic_r, :wet_area,
                        :volume, :slope, :manning_n, :stage, :discharge,
                        :def_discharge, :def_volume, :def_wet_area,
                        :def_hydraulic_r, :def_manning_n, :bath_source,
                        :subdiv_applied, :overbank_n, :channel_n,
                        :subdiv_discharge, :calb_applied, :lake_id
                    )
                """
                ),
                self._prepare_hydro_params(row, catchment_id),
            )

    def _prepare_hydro_params(self, row, catchment_id):
        """Prepare parameters for hydrotable insert with proper null handling"""

        def safe_get(key, default=None):
            return row.get(key, default) if key in row else default

        return {
            "catch_id": catchment_id,
            "hand_id": self.hand_version_id,
            "hydro_id": safe_get("HydroID"),
            "nwm_ver": self.nwm_version_id,
            "feat_id": safe_get("feature_id"),
            "order_id": safe_get("order", 0),
            "num_cells": safe_get("Number of Cells", 0),
            "surf_area": safe_get("SurfaceArea (m2)"),
            "bed_area": safe_get("BedArea (m2)"),
            "top_width": safe_get("TopWidth (m)"),
            "length": safe_get("LENGTHKM"),
            "area": safe_get("AREASQKM"),
            "wet_perim": safe_get("WettedPerimeter (m)"),
            "hydraulic_r": safe_get("HydraulicRadius (m)"),
            "wet_area": safe_get("WetArea (m2)"),
            "volume": safe_get("Volume (m3)"),
            "slope": safe_get("SLOPE"),
            "manning_n": safe_get("ManningN"),
            "stage": safe_get("stage"),
            "discharge": safe_get("discharge_cms"),
            "def_discharge": safe_get("default_discharge_cms"),
            "def_volume": safe_get("default_Volume (m3)"),
            "def_wet_area": safe_get("default_WetArea (m2)"),
            "def_hydraulic_r": safe_get("default_HydraulicRadius (m)"),
            "def_manning_n": safe_get("default_ManningN"),
            "bath_source": safe_get("Bathymetry_source"),
            "subdiv_applied": safe_get("subdiv_applied", False),
            "overbank_n": safe_get("overbank_n"),
            "channel_n": safe_get("channel_n"),
            "subdiv_discharge": safe_get("subdiv_discharge_cms"),
            "calb_applied": safe_get("calb_applied", False),
            "lake_id": safe_get("LakeID"),
        }

    def load_rasters(self, catchment_dir, catchment_id, transaction):
        """Load all raster data types with error handling"""
        print("  Loading REM rasters...")
        rem_files = self._get_files(catchment_dir, ".tif", "rem_zeroed_")

        if not rem_files:
            print("  No REM raster files found")
            return

        print("  Loading catchment rasters...")
        catchment_files = self._get_files(catchment_dir, ".tif", "gw_catchments")

        try:
            # First insert REM rasters and get their IDs
            rem_raster_ids = self._insert_rem_rasters(
                rem_files, catchment_id, transaction
            )

            # Then insert catchment rasters with their corresponding REM raster IDs
            if catchment_files:
                self._insert_catchment_rasters(
                    catchment_files, rem_raster_ids, transaction
                )
            else:
                print("  No catchment raster files found")

        except Exception as e:
            print(f"  Error processing rasters: {str(e)}")
            raise

    def _insert_rem_rasters(self, files, catchment_id, transaction):
        """Insert REM rasters and return their IDs"""
        rem_raster_ids = []
        for file_path in files:
            file_id = self._generate_uuid(file_path)
            rem_raster_ids.append(file_id)

            transaction.execute(
                text(
                    """
                    INSERT INTO HAND_REM_Rasters
                    (rem_raster_id, catchment_id, hand_version_id, raster_path)
                    VALUES (:id, :catch_id, :version, :path)
                """
                ),
                {
                    "id": file_id,
                    "catch_id": catchment_id,
                    "version": self.hand_version_id,
                    "path": file_path,
                },
            )
        return rem_raster_ids

    def _insert_catchment_rasters(self, files, rem_raster_ids, transaction):
        """Insert catchment rasters linking them to REM rasters"""
        for file_path in files:
            # Generate a unique ID for the catchment raster
            catchment_raster_id = self._generate_uuid(file_path)

            # For each catchment raster, we need to link it to a REM raster
            # Here we're assuming a 1:1 relationship and using the first REM raster ID
            # Modify this logic if there's a specific mapping between REM and catchment rasters
            if rem_raster_ids:
                transaction.execute(
                    text(
                        """
                        INSERT INTO HAND_Catchment_Rasters
                        (catchment_raster_id, rem_raster_id, raster_path)
                        VALUES (:id, :rem_id, :path)
                    """
                    ),
                    {
                        "id": catchment_raster_id,
                        "rem_id": rem_raster_ids[0],  # Using first REM raster ID
                        "path": file_path,
                    },
                )

    def load_nwm_lakes(self, gpkg_path):
        """Load NWM lakes from GeoPackage (local or S3)."""
        print("Loading NWM lakes...")
        with self.temp_load_gpkg(gpkg_path) as gdf:
            with self.engine.begin() as conn:
                for _, row in gdf.iterrows():
                    conn.execute(
                        text(
                            """
                            INSERT INTO NWM_Lakes (nwm_lake_id, geometry, shape_area)
                            VALUES (:id, ST_GeomFromText(:geom, 5070), :area)
                        """
                        ),
                        {
                            "id": row["newID"],
                            "geom": row["geometry"].wkt,
                            "area": row["Shape_Area"],
                        },
                    )

    def load_hucs(self, gpkg_path):
        """Load HUCs from GeoPackage."""
        print("Loading HUCs...")
        with self.temp_load_gpkg(gpkg_path) as gdf:
            for layer_info in [
                ("WBDHU2", 2),
                ("WBDHU4", 4),
                ("WBDHU6", 6),
                ("WBDHU8", 8),
            ]:
                if layer_info[0] in gdf:
                    layer = layer_info[0]
                    level = layer_info[1]
                    print(f"  Processing HUC{level}...")
                    huc_gdf = gdf[layer_info[0]]
                    with self.engine.begin() as conn:
                        for _, row in huc_gdf.iterrows():
                            conn.execute(
                                text(
                                    """
                                    INSERT INTO HUCS (huc_id, level, geometry, area_sq_km, states)
                                    VALUES (:huc_id, :level, ST_GeomFromText(:geom, 5070), :area, :states)
                                """
                                ),
                                {
                                    "huc_id": row[f"HUC{level}"],
                                    "level": level,
                                    "geom": row["geometry"].wkt,
                                    "area": row["areasqkm"],
                                    "states": row["states"],
                                },
                            )

    def load_levees(self, gpkg_path):
        """Load levees from GeoPackage."""
        print("Loading levees...")
        with self.temp_load_gpkg(gpkg_path) as gdf:
            with self.engine.begin() as conn:
                for _, row in gdf.iterrows():
                    conn.execute(
                        text(
                            """
                            INSERT INTO Levees (levee_id, geometry, name, systemID, 
                                              systemName, areaSquareMiles, leveedAreaSource)
                            VALUES (:id, ST_GeomFromText(:geom, 5070), :name, 
                                   :sys_id, :sys_name, :area, :source)
                        """
                        ),
                        {
                            "id": row["id"],
                            "geom": row["geometry"].wkt,
                            "name": row["name"],
                            "sys_id": row["systemId"],
                            "sys_name": row["systemName"],
                            "area": row["areaSquareMiles"],
                            "source": row["leveedAreaSource"],
                        },
                    )

    def load_nwm_features(self, gpkg_path):
        """Load NWM features from GeoPackage file."""
        print("Loading NWM features...")
        with self.temp_load_gpkg(gpkg_path) as gdf:
            gdf = gdf["nwm_streams"]
            dropped_count = 0
            with self.engine.begin() as conn:
                for _, row in gdf.iterrows():
                    result = conn.execute(
                        text(
                            """
                            INSERT INTO NWM_Features (nwm_feature_id, nwm_version_id, geometry,
                                                    to_feature, stream_order, lake, gages, slope, mainstem)
                            VALUES (:feat_id, :ver_id, ST_GeomFromText(:geom, 5070),
                                    :to_feature, :order, :lake, :gages, :slope, :mainstem)
                            ON CONFLICT (nwm_feature_id, nwm_version_id) DO NOTHING
                        """
                        ),
                        {
                            "feat_id": row["ID"],
                            "ver_id": self.nwm_version_id,
                            "geom": row["geometry"].wkt,
                            "to_feature": row["to"],
                            "order": row["order_"],
                            "lake": row["Lake"],
                            "gages": row["gages"],
                            "slope": row["Slope"],
                            "mainstem": row["mainstem"],
                        },
                    )
                    if result.rowcount == 0:
                        dropped_count += 1
            print(f"Dropped {dropped_count} duplicate NWM features")

    def _generate_uuid(self, input_str):
        """Generate a UUID from an input string."""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, input_str))

    def temp_load_gpkg(self, gpkg_uri, layers=None):
        """Context manager for loading GeoPackages from S3 or local."""
        if self.is_s3_path(gpkg_uri):
            with tempfile.NamedTemporaryFile(suffix=".gpkg") as tmpfile:
                self.download_s3_file(gpkg_uri, tmpfile.name)
                return gpd.read_file(tmpfile.name, layer=layers)
        else:
            return gpd.read_file(gpkg_uri, layer=layers)


def main():
    """Main function with updated CLI arguments."""
    import argparse

    parser = argparse.ArgumentParser(description="Load HAND data into database")
    parser.add_argument(
        "--db-connection", required=True, help="Database connection string"
    )
    parser.add_argument(
        "--general-dir", help="General data directory/path (S3 or local)"
    )
    parser.add_argument("--hand-dir", help="HAND data directory/path (S3 or local)")
    parser.add_argument(
        "--hand-version", required=True, help="HAND version ID (e.g., v3.0)"
    )
    parser.add_argument(
        "--nwm-version", type=float, required=True, help="NWM version (e.g., 3.0)"
    )

    args = parser.parse_args()
    loader = DatabaseLoader(args.db_connection, args.hand_version, args.nwm_version)

    if args.general_dir:
        loader.load_general_data(args.general_dir)

    if args.hand_dir:
        loader.load_hand_data(args.hand_dir)


if __name__ == "__main__":
    main()
