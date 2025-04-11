# Copyright (c) 2024 Airbyte, Inc., all rights reserved.
import logging
from dataclasses import dataclass
from datetime import datetime, timezone,timedelta
from typing import Any, Dict, MutableMapping, Optional, Iterable

import requests
from airbyte_cdk.sources.declarative.requesters.error_handlers.backoff_strategies.exponential_backoff_strategy import (
    ExponentialBackoffStrategy,
)
from airbyte_cdk.sources.declarative.requesters.error_handlers.default_error_handler import DefaultErrorHandler
from airbyte_cdk.sources.declarative.requesters.http_requester import HttpClient, HttpMethod
from airbyte_cdk.sources.declarative.transformations import RecordTransformation
from airbyte_cdk.sources.declarative.types import Config, Record, StreamSlice, StreamState
from airbyte_cdk.sources.declarative.models.declarative_component_schema import SubstreamPartitionRouter, ParentStreamConfig
from airbyte_cdk.sources.declarative.partition_routers.substream_partition_router import SubstreamPartitionRouter
from airbyte_cdk.sources.streams.concurrent.adapters import StreamPartition
from .common import remove_params_from_url
from typing import Any, Dict, Iterable, List, Optional, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from airbyte_cdk.sources.types import StreamSlice, StreamState, Record
from airbyte_cdk.sources.declarative.partition_routers import CartesianProductStreamSlicer
from typing import Any, Dict, List

from airbyte_cdk.sources.declarative.types import Record

# Import state-related models from the new airbyte_protocol module.
from airbyte_cdk.models.airbyte_protocol import (
    AirbyteStateMessage,
    AirbyteStreamState,
    AirbyteStateType,
    StreamDescriptor,
)


GRAPH_URL = "https://graph.facebook.com/v21.0"


def get_http_response(name: str, path: str, request_params: Dict, config: Config) -> Optional[MutableMapping[str, Any]]:
    http_logger = logging.getLogger(f"airbyte.HttpClient.{name}")
    try:
        url = f"{GRAPH_URL}/{path}"
        token = config["access_token"]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        params = {
            **request_params,
        }
        factor = 5
        backoff_parameters = backoff_config = {"backoff": factor}
        backoff_strategy = ExponentialBackoffStrategy(factor=factor, parameters=backoff_parameters, config=backoff_config)
        error_handler = DefaultErrorHandler(config={}, parameters={}, backoff_strategies=[backoff_strategy])
        http_client = HttpClient(
            name=name,
            logger=http_logger,
            use_cache=False,
            error_handler=error_handler,
        )
        _, response = http_client.send_request(
            http_method=HttpMethod.GET.name,
            url=url,
            request_kwargs={},
            headers=headers,
            params=params,
        )
        response.raise_for_status()
    except requests.HTTPError as http_err:
        error = f"HTTP error occurred: {http_err.response.status_code} - {http_err.response.text}"
        http_logger.error(f"Error getting children data: {error}")
        raise Exception(error)
    except Exception as err:
        error = f"An error occurred: {err}"
        http_logger.error(f"Error getting children data: {error}")
        raise Exception(error)
    return response.json()


@dataclass
class InstagramClearUrlTransformation(RecordTransformation):
    def transform(self, record: MutableMapping[str, Any], config: Optional[Config] = None, **kwargs) -> MutableMapping[str, Any]:
        """
        Transforms the given record by removing specific query parameters from certain URLs to ensure consistency
        and prevent test failures due to dynamic parameters.

        Specifically, this function removes the `_nc_rid` parameter from the `media_url` and the `ccb` parameter
        from the `profile_picture_url`. The `_nc_rid` parameter is generated anew each time and the `ccb` parameter
        can change its value, which can cause tests to fail when checking for identity.

        Removing these parameters does not invalidate the URLs. The links remain correct and functional, allowing
        users to view the video or see the picture.
        """
        if record.get("media_url"):
            record["media_url"] = remove_params_from_url(record["media_url"], params=["_nc_rid"])
        if record.get("profile_picture_url"):
            record["profile_picture_url"] = remove_params_from_url(record["profile_picture_url"], params=["ccb"])

        return record

@dataclass
class InstagramMediaCommentsTransformation(RecordTransformation):
    def transform(self, record: MutableMapping[str, Any], config: Optional[Config] = None, **kwargs) -> MutableMapping[str, Any]:
        # Safely extract the comments data
        comments = record.get("comments", {})
        data = comments.get("data", [])
        record["comments"] = data
        logging.debug("Transformed record: %s", record)
        return record


@dataclass
class InstagramMediaChildrenTransformation(RecordTransformation):
    def transform(self, record: MutableMapping[str, Any], config: Optional[Config] = None, **kwargs) -> MutableMapping[str, Any]:
        """
        Transforms the 'children' field in the record, which is an array of Media IDs with a common Media parent.
        This transformation fetches detailed information for each Media ID from the /media endpoint and updates the 'children' array
        with this information.

        Example input:
            "children": {
                  "data": [
                    {
                      "id": "7608776690540"
                    },
                    {
                      "id": "2896800415362"
                    }
                  ]
                }

        After fetching information for each Media ID:
            children:
                [
                  {
                    "id": "7608776690540",
                    "ig_id": "2521545917836833225",
                    "media_type": "IMAGE",
                    "media_url": "https://fake_url?_nc_cat=...",
                    // more fields
                  },
                  {
                    "id": "2896800415362",
                    "ig_id": "2521545917736276706",
                    "media_type": "IMAGE",
                    "media_url": "https://fake_url?_nc_cat=...",
                    // more fields
                  }
                }
        """
        children = record.get("children")
        children_fetched = []
        fields = "id,ig_id,media_type,media_url,owner,permalink,shortcode,thumbnail_url,timestamp,username"
        if children:
            children_ids = [child.get("id") for child in children.get("data")]
            for children_id in children_ids:
                media_data = get_http_response(f"MediaInsights.{children_id}", children_id, {"fields": fields}, config=config)
                media_data = InstagramClearUrlTransformation().transform(media_data)
                if media_data.get("timestamp"):
                    normalized_timestamp = normalize_timestamp(media_data["timestamp"])
                    if normalized_timestamp:
                        media_data["timestamp"] = normalized_timestamp
                children_fetched.append(media_data)
            record["children"] = children_fetched
        return record


@dataclass
class InstagramInsightsTransformation(RecordTransformation):
    def transform(self, record: MutableMapping[str, Any], **kwargs) -> MutableMapping[str, Any]:
        """
        The transformation flattens the array of insights into a single object (dictionary). In such object, each key-value pair
        in the resulting object represents a metric and and its corresponding value.

        Example Input:
        {
            "data": [
              {
                "name": "comments",
                "period": "lifetime",
                "values": [
                  {
                    "value": 7
                  }
                ],
                "title": "title1",
                "description": "Description1.",
                "id": "insta_id/insights/comments/lifetime"
              },
              {
                "name": "ig_reels_avg_watch_time",
                "period": "lifetime",
                "values": [
                  {
                    "value": 11900
                  }
                ],
                "title": "2",
                "description": "Description2.",
                "id": "insta_id/insights/ig_reels_avg_watch_time/lifetime"
              }
        }

        Example Output:
        {
            "comments": 7,
            "ig_reels_avg_watch_time": 11900
        }
        """
        if record.get("data"):
            insights_data = record.pop("data")
            for insight in insights_data:
                record[insight["name"]] = insight.get("values")[0]["value"]
        return record



@dataclass
class InstagramBreakDownResultsTransformation(RecordTransformation):
    """
    The transformation flattens a nested array of breakdown results located at total_value.breakdowns[0].results into a single object
    (dictionary). In this transformation, each key-value pair in the resulting object represents a dimension and its corresponding value.

    Example input:
        {
        "total_value": {
          "breakdowns": [
            {
              "dimension_keys": [
                "city"
              ],
              "results": [
                {
                  "dimension_values": [
                    "London, England"
                  ],
                  "value": 263
                },
                {
                  "dimension_values": [
                    "Sydney, New South Wales"
                  ],
                  "value": 467
                }
              ]
            }
          ]
        },
        "id": "id/insights/follower_demographics/lifetime"
      }

    Example output:
        {
        "value": {
          "London, England": 263,
          "Sydney, New South Wales": 467,
        }
    The nested 'results' array is transformed into a 'value' dictionary where each key is a dimension and each value is the corresponding value.
    """

    def transform(self, record: MutableMapping[str, Any], **kwargs) -> MutableMapping[str, Any]:
        record_total_value = record.pop("total_value")
        record["value"] = {res.get("dimension_values", [""])[0]: res.get("value") for res in record_total_value["breakdowns"][0]["results"]}
        return record





def normalize_timestamp(timestamp_str):
    """
    Parse a timestamp string in various formats and normalize it to ISO format with timezone.

    Args:
        timestamp_str (str): The timestamp string to normalize

    Returns:
        str: Normalized timestamp in format "%Y-%m-%dT%H:%M:%S%z" with colon in timezone
        None: If parsing fails
    """
    if not timestamp_str:
        return None

    # Try different possible formats
    formats = [
        "%Y-%m-%dT%H:%M:%S%z",    # ISO format with T
        "%Y-%m-%d %H:%M:%S%z",    # Space instead of T
        "%Y-%m-%dT%H:%M:%S+0000", # No colon in timezone
        "%Y-%m-%d %H:%M:%S+0000"  # Space and no colon
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(timestamp_str, fmt)
            # Format to consistent format
            formatted_str = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
            # Add colon to timezone if needed
            if ":" not in formatted_str[-5:]:
                return formatted_str[:-2] + ":" + formatted_str[-2:]
            return formatted_str
        except ValueError:
            continue

    # If we couldn't parse the timestamp, log it and return None
    print(f"Warning: Could not parse timestamp '{timestamp_str}'")
    return None
