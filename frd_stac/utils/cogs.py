from datetime import datetime, timezone
from matplotlib import pyplot as plt
import numpy as np
import pyproj
import pystac
import rasterio
from rasterio.warp import reproject, Resampling
from shapely.geometry import Polygon, mapping

from .s3_utils import get_object_datetime, split_s3_path


class COG:
    """
    Cloud-Optimized Geotiff Class for conversion into a stac item or asset
    """

    def __init__(
        self,
        uri: str,
        bbox: list,
        temporal: str,
        resolution: str,
        projection: str,
        cmap: str,
    ):
        """
        Initialize COG object.

        Args:
            uri (str): The URI of the COG.
            bbox (list): Bounding box coordinates [minx, miny, maxx, maxy].
            temporal (list): Temporal information.
            resolution (str): Resolution details.
            projection (str): Projection details.
        """
        self.uri = uri
        self._bbox = bbox
        self.temporal = datetime.now(tz=timezone.utc)
        self.resolution = resolution
        self.projection = projection
        self._footprint_4326 = mapping(self._create_footprint())
        self.cmap = cmap

    @property
    def bbox(self):
        return self._bbox

    @property
    def bbox_4326(self):
        transformer = pyproj.Transformer.from_crs(
            self.projection, "epsg:4326", always_xy=True
        )

        return list(transformer.transform(self._bbox[0], self._bbox[1])) + list(
            transformer.transform(self._bbox[2], self._bbox[3])
        )

    def _create_footprint(self):
        """
        Only provide footprint option in 4326
        """
        return Polygon(
            [
                [self.bbox_4326[0], self.bbox_4326[1]],
                [self.bbox_4326[0], self.bbox_4326[3]],
                [self.bbox_4326[2], self.bbox_4326[3]],
                [self.bbox_4326[2], self.bbox_4326[1]],
            ]
        )

    @classmethod
    def from_s3(cls, bucket_name, file_key, cmap):
        try:
            with rasterio.Env(AWS_S3_ENDPOINT_URL="https://s3.amazonaws.com"):
                s3_path = f"/vsis3/{bucket_name}/{file_key}"

                with rasterio.open(s3_path) as dataset:
                    bbox = dataset.bounds
                    projection = dataset.crs.to_string()
                    if dataset.tags(ns="TIFFTAG_DATETIME") == {}:
                        temporal = get_object_datetime(bucket_name, file_key)
                    else:
                        temporal = dataset.tags(ns="TIFFTAG_DATETIME")

                return cls(
                    uri=f"s3://{bucket_name}/{file_key}",
                    bbox=[bbox.left, bbox.bottom, bbox.right, bbox.top],
                    temporal=datetime.now(tz=timezone.utc),
                    resolution=dataset.res,
                    projection=projection,
                    cmap=cmap,
                )
        except Exception as e:
            print(f"An error occurred: {e}")
            return None

    @property
    def all_cells_dry(self):
        # TODO: reading data should happen only once, update implementation
        bucket, key = split_s3_path(self.uri)

        try:
            with rasterio.Env(AWS_S3_ENDPOINT_URL="https://s3.amazonaws.com"):
                s3_path = f"/vsis3/{bucket}/{key}"
                with rasterio.open(s3_path) as dataset:
                    raster_data = dataset.read(1)
                    nodata = dataset.nodatavals[0]
                    return (raster_data == nodata).all()

        except Exception as e:
            print(f"An error occurred: {e}")
            return None

    def create_thumbnail(self, thumbnail_path, factor=4):
        try:
            with rasterio.open(self.uri.replace("s3://", "/vsis3/")) as dataset:
                # Full extent of the image
                window = rasterio.windows.Window(0, 0, dataset.width, dataset.height)
                data = dataset.read(window=window)

                # Calculate the lower resolution dimensions
                new_width = dataset.width // factor
                new_height = dataset.height // factor

                # Resample the data to create a lower-resolution image
                transform = dataset.transform * dataset.transform.scale(
                    (dataset.width / new_width), (dataset.height / new_height)
                )
                resampled = np.empty(
                    (dataset.count, new_height, new_width), dataset.profile["dtype"]
                )

                for i in range(1, dataset.count + 1):
                    reproject(
                        source=data[i - 1],
                        destination=resampled[i - 1],
                        src_transform=dataset.transform,
                        src_crs=dataset.crs,
                        dst_transform=transform,
                        dst_crs=dataset.crs,
                        resampling=Resampling.average,
                    )

                # Save the downsampled data as a PNG file
                plt.imsave(
                    thumbnail_path, np.squeeze(resampled, axis=0), cmap=self.cmap
                )

        except Exception as e:
            print(f"An error occurred: {e}")

    def __repr__(self):
        return (
            f"COG("
            f"\n    uri='{self.uri}',"
            f"\n    bbox={self._bbox},"
            f"\n    footprint_4326={self._footprint_4326},"
            f"\n    temporal={self.temporal},"
            f"\n    resolution='{self.resolution}',"
            f"\n    projection='{self.projection}'"
            f"\n)"
        )


class FRDCog(COG):
    def __init__(self, uri, bbox, temporal, resolution, projection, cmap):
        super().__init__(uri, bbox, temporal, resolution, projection, cmap)

    def to_pystac_item(self, item_id, thumbnail_path, new_thumbnail: bool = True):
        item = pystac.Item(
            id=item_id,
            geometry=self._footprint_4326,
            bbox=self.bbox_4326,
            datetime=self.temporal,
            stac_extensions=[
                "https://stac-extensions.github.io/projection/v1.0.0/schema.json",
                "https://stac-extensions.github.io/storage/v1.0.0/schema.json",
                "https://stac-extensions.github.io/processing/v1.1.0/schema.json",
            ],
            properties={
                "frd:proj": "kanawha",
                "frd:project_status": "FFRD pilot",
                "frd:project_status": "FFRD pilot",
                "proj:bbox": self.bbox,
                "proj:wkt2": self.projection,
                # "storage:platform": "AWS",
                # "storage:region": "us-east-1",
                "processing:software": {"frd-to-stac": "2023.11.04"},
                "created": pystac.utils.datetime_to_str(datetime.now(tz=timezone.utc)),
                "updated": pystac.utils.datetime_to_str(datetime.now(tz=timezone.utc)),
            },
        )

        item.add_asset(
            key="cog",
            asset=pystac.Asset(
                href="grid.tif",
                # href=self.uri,
                media_type=pystac.MediaType.GEOTIFF,
                roles=["data"],
            ),
        )

        if new_thumbnail:
            self.create_thumbnail(thumbnail_path)
            item.add_asset(
                key="thumbnail",
                asset=pystac.Asset(
                    href="thumbnail.png",
                    media_type=pystac.MediaType.PNG,
                    roles=["thumbnail"],
                ),
            )

        return item