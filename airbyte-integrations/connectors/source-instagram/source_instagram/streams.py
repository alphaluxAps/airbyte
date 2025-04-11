#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import copy
import json
import logging
import os
from abc import ABC
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional

import pendulum
from cached_property import cached_property

from airbyte_cdk.models import SyncMode
from airbyte_cdk.sources.streams import IncrementalMixin, Stream
from airbyte_cdk.sources.utils.transform import TransformConfig, TypeTransformer
from source_instagram.api import InstagramAPI

from .common import remove_params_from_url


class DatetimeTransformerMixin:
    transformer: TypeTransformer = TypeTransformer(TransformConfig.CustomSchemaNormalization)

    @staticmethod
    @transformer.registerCustomTransform
    def custom_transform_datetime_rfc3339(original_value, field_schema):
        """
        Transform datetime string to RFC 3339 format
        """
        if original_value and field_schema.get("format") == "date-time" and field_schema.get("airbyte_type") == "timestamp_with_timezone":
            # Parse the ISO format timestamp
            dt = pendulum.parse(original_value)

            # Convert to RFC 3339 format
            return dt.to_rfc3339_string()
        return original_value


class InstagramStream(Stream, ABC):
    """Base stream class"""

    page_size = 100
    primary_key = "id"

    def __init__(self, api: InstagramAPI, **kwargs):
        super().__init__(**kwargs)
        self._api = api

    @cached_property
    def fields(self) -> List[str]:
        """List of fields that we want to query, for now just all properties from stream's schema"""
        non_object_fields = ["page_id", "business_account_id"]
        fields = list(self.get_json_schema().get("properties", {}).keys())
        return list(set(fields) - set(non_object_fields))

    def request_params(
        self,
        stream_slice: Mapping[str, Any] = None,
        stream_state: Mapping[str, Any] = None,
    ) -> MutableMapping[str, Any]:
        """Parameters that should be passed to query_records method"""
        return {"limit": self.page_size}

    def stream_slices(
        self, sync_mode: SyncMode, cursor_field: List[str] = None, stream_state: Mapping[str, Any] = None
    ) -> Iterable[Optional[Mapping[str, Any]]]:
        """
        Override to define the slices for this stream. See the stream slicing section of the docs for more information.

        :param sync_mode:
        :param cursor_field:
        :param stream_state:
        :return:
        """
        for account in self._api.accounts:
            yield {"account": account}

    def transform(self, record: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        return self._clear_url(record)

    @staticmethod
    def _clear_url(record: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        """
        This function removes the _nc_rid parameter from the video url and ccb from profile_picture_url for users.
        _nc_rid is generated every time a new one and ccb can change its value, and tests fail when checking for identity.
        This does not spoil the link, it remains correct and by clicking on it you can view the video or see picture.
        """
        if record.get("media_url"):
            record["media_url"] = remove_params_from_url(record["media_url"], params=["_nc_rid"])
        if record.get("profile_picture_url"):
            record["profile_picture_url"] = remove_params_from_url(record["profile_picture_url"], params=["ccb"])

        return record


class InstagramIncrementalStream(InstagramStream, IncrementalMixin):
    """Base class for incremental streams"""

    def __init__(self, start_date: datetime, lookback_window: int = 7, **kwargs):
        super().__init__(**kwargs)
        self._start_date = pendulum.parse(start_date)
        self._lookback_window = lookback_window  # in days
        self._state = {}

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value: Mapping[str, Any]):
        self._state.update(**value)

    def _update_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]):
        """Update stream state from latest record"""
        # if there is no `end_date` value take the `start_date`
        record_value = latest_record.get(self.cursor_field) or self._start_date.to_iso8601_string()
        account_id = latest_record.get("business_account_id")
        state_value = current_stream_state.get(account_id, {}).get(self.cursor_field) or record_value
        max_cursor = max(pendulum.parse(state_value), pendulum.parse(record_value))
        new_stream_state = copy.deepcopy(current_stream_state)
        new_stream_state[account_id] = {self.cursor_field: str(max_cursor)}
        return new_stream_state


class UserInsights(DatetimeTransformerMixin, InstagramIncrementalStream):
    """Docs: https://developers.facebook.com/docs/instagram-api/reference/ig-user/insights"""

    METRICS_BY_PERIOD = {
        "day": [
            "follower_count",
            "reach",
        ],
        "week": ["reach"],
        "days_28": ["reach"],
        "lifetime": ["online_followers"],
    }

    primary_key = ["business_account_id", "date"]
    cursor_field = "date"

    # For some metrics we can only get insights not older than 30 days, it is Facebook policy
    buffer_days = 30
    days_increment = 1

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._end_date = pendulum.now()
        self.should_exit_gracefully = False

    def read_records(
            self,
            sync_mode: SyncMode,
            cursor_field: List[str] = None,
            stream_slice: Mapping[str, Any] = None,
            stream_state: Mapping[str, Any] = None,
    ) -> Iterable[Mapping[str, Any]]:
        """
        For each daily slice, we:
          1) Fetch the standard METRICS_BY_PERIOD metrics
          2) Fetch daily follows_and_unfollows (a custom structure)
          3) Fetch daily 'accounts_engaged', 'replies', etc. metrics (another total_value structure)
          4) Merge them all into a single record
          5) If everything is empty => decide if we skip or gracefully stop
        """

        account = stream_slice["account"]
        ig_account = account["instagram_business_account"]
        account_id = ig_account.get("id")

        # Build base params (limit, plus daily since/until)
        base_params = self.request_params(stream_state=stream_state, stream_slice=stream_slice)

        # (1) Fetch the standard daily metrics
        insight_list = self._fetch_periodic_metrics(ig_account, base_params)

        # (2) Fetch daily follows_and_unfollows
        follows_data = self._fetch_follows_and_unfollows(ig_account, base_params)

        # (3) Fetch daily engaged metrics
        engaged_data = self._fetch_engagement_metrics(ig_account, base_params)

        # (4) Fetch daily interactions metrics
        interactions_data = self._fetch_interactions_metrics(ig_account, base_params)

        # Build the final record
        #  - If we want to keep "page_id" and "business_account_id" each time:
        insight_record = {
            "page_id": account["page_id"],
            "business_account_id": account_id,
        }

        # Merge the normal metrics from `insight_list`
        for insight in insight_list:
            key = insight["name"]
            if insight["period"] in ["week", "days_28"]:
                key += f"_{insight['period']}"

            # Usually these standard metrics have "values": [...]
            # This code is still the same as the original approach
            val = insight.get("values", [{}])[0].get("value")
            insight_record[key] = val
            if not insight_record.get(self.cursor_field):
                insight_record[self.cursor_field] = insight.get("values", [{}])[0].get("end_time")

        # Merge the daily follows/unfollows data (if any)
        if follows_data:
            insight_record.update(follows_data)

        # Merge the daily engaged metrics
        if engaged_data:
            insight_record.update(engaged_data)

        # Merge the daily interactions metrics
        if interactions_data:
            insight_record.update(interactions_data)

        # Decide if we have anything
        has_standard_metrics = len(insight_list) > 0
        has_follows_data     = bool(follows_data)
        has_engaged_data     = bool(engaged_data)
        has_interactions_data     = bool(interactions_data)

        # If there's NOTHING, do we gracefully stop or yield an empty record?
        if not has_standard_metrics and not has_follows_data and not has_engaged_data and not has_interactions_data:
            self.logger.warning(
                f"No data received for base params {json.dumps(base_params)} across all daily calls. "
                "Stopping stream to avoid missing temporarily unavailable data."
            )
            self.should_exit_gracefully = True
            return

        # If we don't have a "date" yet, you might want to fill it in with the slice's "since" or something
        if not insight_record.get(self.cursor_field):
            # fallback to the slice's 'since', if present
            since_date = stream_slice.get("since")
            if since_date:
                insight_record[self.cursor_field] = since_date

        yield insight_record

        # Finally update state if incremental
        if sync_mode == SyncMode.incremental:
            self.state = self._update_state(self.state, insight_record)

    def stream_slices(
        self, sync_mode: SyncMode, cursor_field: List[str] = None, stream_state: Mapping[str, Any] = None
    ) -> Iterable[Optional[Mapping[str, Any]]]:
        """Extend default slicing based on accounts with slices based on date intervals"""
        stream_state = stream_state or {}
        stream_slices = super().stream_slices(sync_mode=sync_mode, cursor_field=cursor_field, stream_state=stream_state)
        for stream_slice in stream_slices:
            account = stream_slice["account"]
            account_id = account["instagram_business_account"]["id"]

            state_value = stream_state.get(account_id, {}).get(self.cursor_field)
            start_date = pendulum.parse(state_value) if state_value else self._start_date

            # Apply look-back window if we have state (not the initial sync)
            if state_value and hasattr(self, '_lookback_window') and self._lookback_window > 0:
                start_date = start_date.subtract(days=self._lookback_window)

            start_date = max(start_date, self._start_date, pendulum.now().subtract(days=self.buffer_days))
            if start_date > pendulum.now():
                continue
            for since in pendulum.period(start_date, self._end_date).range("days", self.days_increment):
                until = since.add(days=self.days_increment)
                if self.should_exit_gracefully:
                    self.logger.info(f"Stopping syncing stream '{self.name}'")
                    return
                self.logger.info(f"Reading insights between {since.date()} and {until.date()}")
                yield {
                    **stream_slice,
                    "since": since.to_datetime_string(),
                    "until": until.to_datetime_string(),  # excluding
                }

    def request_params(
        self,
        stream_slice: Mapping[str, Any] = None,
        stream_state: Mapping[str, Any] = None,
    ) -> MutableMapping[str, Any]:
        """Append datetime range params"""
        params = super().request_params(stream_state=stream_state, stream_slice=stream_slice)
        return {
            **params,
            "since": stream_slice["since"],
            "until": stream_slice["until"],
        }

    def _state_has_legacy_format(self, state: Mapping[str, Any]) -> bool:
        """Tell if the format of state is outdated"""
        for value in state.values():
            if not isinstance(value, Mapping):
                return True
        return False

    #
    # ---------------------------------------------------------------------
    # Helper sub-functions below
    # ---------------------------------------------------------------------
    #

    def _fetch_periodic_metrics(
            self, ig_account: Any, base_params: Mapping[str, Any]
    ) -> List[Mapping[str, Any]]:
        """
        The original loop over self.METRICS_BY_PERIOD
        Returns a list of "raw_data" dicts
        """
        insight_list = []
        for period, metrics in self.METRICS_BY_PERIOD.items():
            params = {
                **base_params,
                "metric": metrics,
                "period": [period],
            }
            cursor = ig_account.get_insights(params=params)
            # gather results
            if cursor:
                insight_list += [insights.export_all_data() for insights in cursor]
        return insight_list

    def _fetch_follows_and_unfollows(
            self, ig_account: Any, base_params: Mapping[str, Any]
    ) -> Optional[Mapping[str, Any]]:
        """
        The extra call for 'follows_and_unfollows' which returns nested total_value.breakdowns, etc.
        """
        extra_params = {
            **base_params,
            "metric": ["follows_and_unfollows"],
            "period": ["day"],
            "metric_type": "total_value",
            "breakdown": "follow_type",
        }
        cursor = ig_account.get_insights(params=extra_params)
        if not cursor:
            return None

        raw_data = cursor[0].export_all_data()

        # Parse the structure. If it truly has "total_value.breakdowns" -> parse them
        # or if your data might differ, adjust accordingly.
        total_value = raw_data.get("total_value", {})
        breakdowns = total_value.get("breakdowns", [])
        unfollows = 0
        if breakdowns:
            results = breakdowns[0].get("results", [])
            #new_followers = 0
            #unfollows = 0
            for item in results:
                dims = item.get("dimension_values", [])
                count = item.get("value", 0)

                # Example logic: treat "FOLLOWER" as "new_followers"
                # and "NON_FOLLOWER" as "new_followers_non_followers"
                # (It's not strictly "unfollows," so be sure you understand the meaning.)
                #if "FOLLOWER" in dims:
                #    new_followers += count
                #el
                if "NON_FOLLOWER" in dims:
                    unfollows += count

        return {
            # rename as you like
            "unfollows": unfollows
        }

    def _fetch_engagement_metrics(
            self, ig_account: Any, base_params: Mapping[str, Any]
    ) -> Optional[Mapping[str, Any]]:
        """
        Another daily call for 'accounts_engaged,replies,website_clicks', etc.
        This returns a data array with e.g. "total_value": { "value": <number> }
        We'll parse them into a dict.
        """
        # define the metrics you want
        daily_metrics = [
            "accounts_engaged",
            "replies",
            "website_clicks",
            "profile_views",
            "profile_links_taps",
        ]
        extra_params = {
            **base_params,
            "metric": daily_metrics,
            "period": ["day"],
            "metric_type": "total_value",
        }

        cursor = ig_account.get_insights(params=extra_params)
        if not cursor:
            return None

        # cursor is a list of "Insights" objects; each has "data" for one or more metrics.
        # Typically each object in that list is one metric or so. If we do .export_all_data() on the first item,
        # we may only get the first metric. We want them all, so let's parse them all.
        # The standard pattern in this code is to do something like:
        raw_data_list = [c.export_all_data() for c in cursor]
        # 'raw_data_list' might be multiple items, each something like:
        # {
        #   "name": "accounts_engaged",
        #   "period": "day",
        #   "title": "...",
        #   "total_value": {
        #     "value": 14
        #   },
        #   "id": ".../insights/accounts_engaged/day"
        # }

        data_dict = {}
        for raw_data in raw_data_list:
            metric_name = raw_data.get("name")
            tv = raw_data.get("total_value", {})
            val = tv.get("value")
            # just store the metric name => value
            data_dict[metric_name] = val

        return data_dict if data_dict else None

    def _fetch_interactions_metrics(
            self, ig_account: Any, base_params: Mapping[str, Any]
    ) -> Optional[Mapping[str, Any]]:
        """
        Fetch daily interactions metrics (likes, comments, saves, shares, total_interactions)
        with media_product_type breakdown. Returns a dictionary like:

            {
              "likes": 8,
              "comments": 0,
              "saves": 1,
              "shares": 0,
              "total_interactions": 9,
              "engagement_breakdown": {
                "likes": {"ad":5, "post":2, "reel":1},
                "comments": {},
                "saves": {"post":1},
                "shares": {},
                "total_interactions": {"ad":5, "post":3, "reel":1}
              }
            }

        or None if no data is returned.
        """

        metrics = ["likes", "comments", "saves", "shares", "total_interactions"]
        extra_params = {
            **base_params,
            "metric": metrics,
            "period": ["day"],
            "metric_type": "total_value",
            "breakdown": "media_product_type",
        }

        # Make the API call
        cursor = ig_account.get_insights(params=extra_params)
        if not cursor:
            return None

        raw_data_list = [c.export_all_data() for c in cursor]

        # Prepare the final result
        interactions_data: dict = {}
        engagement_breakdown: dict = {}

        # Parse each metric object from the response
        for raw_data in raw_data_list:
            metric_name = raw_data.get("name")  # e.g. "likes", "comments", etc.
            total_value = raw_data.get("total_value", {})
            top_level_val = total_value.get("value", 0)

            # Store the overall metric count (e.g. interactions_data["likes"] = 8)
            interactions_data[metric_name] = top_level_val

            # Build the breakdown (e.g. {"ad":5, "post":2, "reel":1})
            breakdown_dict = {}
            for breakdown_item in total_value.get("breakdowns", []):
                for r in breakdown_item.get("results", []):
                    dims = r.get("dimension_values", [])
                    val = r.get("value", 0)
                    if dims:
                        # Typically dims = ["AD"], ["POST"], or ["REEL"]
                        key = dims[0].lower()  # unify to lowercase
                        breakdown_dict[key] = breakdown_dict.get(key, 0) + val

            # Store this metric's breakdown
            engagement_breakdown[metric_name] = breakdown_dict

        # Attach the combined breakdown under "engagement_breakdown"
        interactions_data["engagement_breakdown"] = engagement_breakdown

        return interactions_data


class MediaInsightsStream(DatetimeTransformerMixin, InstagramIncrementalStream):
    """
    Custom stream implementation for Media Insights that implements direct timestamp filtering.
    """
    cursor_field = "media_timestamp"
    name = "media_insights"
    
    def __init__(self, media_stream=None, **kwargs):
        super().__init__(**kwargs)
        self._media_stream = media_stream
        self.primary_key = "id"
        
    def get_json_schema(self) -> Dict[str, Any]:
        """
        Load schema from schemas/media_insights.json file.
        """
        schema_path = os.path.join(os.path.dirname(__file__), "schemas", "media_insights.json")
        try:
            with open(schema_path, "r") as f:
                return json.loads(f.read())
        except Exception as e:
            logging.error(f"Error loading schema from {schema_path}: {e}")
            # Provide a fallback schema if the file can't be loaded
            return {
                "type": "object",
                "properties": {
                    "id": {"type": ["null", "string"]},
                    "business_account_id": {"type": ["null", "string"]},
                    "media_timestamp": {"type": ["null", "string"], "format": "date-time"},
                    "reach": {"type": ["null", "integer"]}
                }
            }
        
    def set_media_stream(self, media_stream):
        """Set the media stream to use for fetching parent records"""
        self._media_stream = media_stream
        
    def stream_slices(
        self, sync_mode: SyncMode, cursor_field: List[str] = None, stream_state: Mapping[str, Any] = None
    ) -> Iterable[Mapping[str, Any]]:
        """
        Return empty slices since we're handling everything in read_records
        """
        # We'll just return an empty slice to trigger one read_records call
        yield {}
        
    def read_records(
        self,
        sync_mode: SyncMode,
        cursor_field: List[str] = None,
        stream_slice: Mapping[str, Any] = None,
        stream_state: Mapping[str, Any] = None,
    ) -> Iterable[Mapping[str, Any]]:
        """
        Override read_records to filter media records by timestamp before fetching insights.
        This ensures we only process media within the lookback window.
        """
        if not self._media_stream:
            logging.error("MediaInsights: Media stream not set. No records will be returned.")
            # Return empty list rather than raising an exception
            return []
        
        # Determine the cutoff date based on sync mode and state
        if sync_mode == SyncMode.full_refresh or not stream_state:
            # For full refresh or first sync (no state), use start_date
            cutoff_date = self._start_date
            logging.info(f"MediaInsights: Full refresh or initial sync - using start_date as cutoff: {cutoff_date.to_iso8601_string()}")
        else:
            # For incremental sync with state, use lookback window
            lookback_days = self._lookback_window
            cutoff_date = pendulum.now() - pendulum.duration(days=lookback_days)
            logging.info(f"MediaInsights: Incremental sync - using lookback of {lookback_days} days (cutoff: {cutoff_date.to_iso8601_string()})")
        
        # Initialize counters for tracking
        media_processed = 0
        media_filtered = 0
        insights_returned = 0
        
        try:
            # Get media records from the media stream
            for account_slice in self._media_stream.stream_slices(sync_mode=sync_mode, cursor_field=cursor_field, stream_state=stream_state):
                try:
                    # Skip empty slices
                    if not account_slice:
                        logging.debug("MediaInsights: Skipping empty account slice")
                        continue
                        
                    media_records = list(self._media_stream.read_records(
                        sync_mode=sync_mode,
                        cursor_field=cursor_field,
                        stream_slice=account_slice,
                        stream_state=stream_state
                    ))
                except Exception as e:
                    logging.error(f"MediaInsights: Error reading media records for slice {account_slice}: {e}")
                    continue
                    
                # Log the number of media records
                logging.info(f"MediaInsights: Processing {len(media_records)} media records from account {account_slice.get('account', {}).get('business_account_id', 'unknown')}")
                
                # Filter media records by timestamp
                filtered_records = []
                for record in media_records:
                    media_processed += 1
                    
                    # Extract timestamp
                    timestamp_str = record.get("timestamp")
                    if not timestamp_str:
                        continue
                        
                    # Parse timestamp
                    try:
                        timestamp = pendulum.parse(timestamp_str)
                        
                        # Filter by cutoff date
                        if timestamp >= cutoff_date:
                            filtered_records.append(record)
                        else:
                            logging.debug(f"MediaInsights: Filtered out media record with timestamp {timestamp_str} (older than cutoff {cutoff_date})")
                    except Exception as e:
                        logging.warning(f"MediaInsights: Error parsing timestamp: {e}")
                
                media_filtered += len(filtered_records)
                logging.info(f"MediaInsights: Filtered to {len(filtered_records)} media records from cutoff date {cutoff_date.to_iso8601_string()}")
                
                # Process filtered media records
                for media_record in filtered_records:
                    # Extract necessary information from the media record
                    media_id = media_record.get("id")
                    business_account_id = media_record.get("business_account_id")
                    page_id = media_record.get("page_id")
                    media_type = media_record.get("media_type")
                    media_product_type = media_record.get("media_product_type", "")
                    timestamp = media_record.get("timestamp")
                    
                    if not media_id or not business_account_id:
                        continue
                        
                    # Determine metrics based on media type
                    metrics = []
                    if media_product_type == "REELS":
                        metrics = ["comments", "ig_reels_avg_watch_time", "ig_reels_video_view_total_time", "likes", "reach", "saved", "shares"]
                    elif media_type == "VIDEO" and media_product_type == "FEED":
                        metrics = ["reach", "saved"]
                    elif media_type == "VIDEO":
                        metrics = ["reach", "saved", "likes", "comments", "shares", "follows", "profile_visits"]
                    elif media_type == "CAROUSEL_ALBUM":
                        metrics = ["reach", "saved", "shares", "follows", "profile_visits"]
                    else:
                        metrics = ["reach", "saved", "likes", "comments", "shares", "follows", "profile_visits"]
                        
                    # Fetch insights for this media
                    params = {"metric": ",".join(metrics)}
                    
                    try:
                        insights_data = {}
                        
                        # Add identity fields
                        insights_data["id"] = media_id
                        insights_data["page_id"] = page_id
                        insights_data["business_account_id"] = business_account_id
                        insights_data["media_timestamp"] = timestamp
                        
                        # Fetch insights
                        cursor = self._api.get_insights(account_id=media_id, params=params)
                        if cursor:
                            for insight in cursor:
                                # Extract data
                                insight_data = insight.export_all_data()
                                name = insight_data.get("name")
                                if not name:
                                    continue
                                    
                                # Extract the value
                                values = insight_data.get("values", [])
                                if values and len(values) > 0:
                                    value = values[0].get("value")
                                    insights_data[name] = value
                            
                            # Yield the insights data
                            yield insights_data
                            insights_returned += 1
                            
                    except Exception as e:
                        logging.warning(f"MediaInsights: Error fetching insights for media {media_id}: {e}")
                        # Continue with next media record
        
        except Exception as e:
            logging.error(f"MediaInsights: Unexpected error occurred: {e}")
            # Don't re-raise - we need to continue to process as many records as possible
        
        finally:
            # Always log summary stats regardless of errors
            if sync_mode == SyncMode.full_refresh or not stream_state:
                logging.info(f"MediaInsights summary (FULL REFRESH): Processed {media_processed} media records, filtered to {media_filtered} (since {cutoff_date.to_iso8601_string()}), returned {insights_returned} insights")
            else:
                logging.info(f"MediaInsights summary (INCREMENTAL): Processed {media_processed} media records, filtered to {media_filtered} (since {cutoff_date.to_iso8601_string()}), returned {insights_returned} insights")
        
    def get_updated_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]) -> Mapping[str, Any]:
        """
        Override to properly save state for the cursor field media_timestamp.
        Handles nested state by business_account_id.
        """
        if not latest_record.get(self.cursor_field) or not latest_record.get("business_account_id"):
            return current_stream_state
            
        account_id = latest_record["business_account_id"]
        latest_cursor_value = latest_record[self.cursor_field]
        
        # Initialize account in state if needed
        current_stream_state = current_stream_state or {}
        current_stream_state[account_id] = current_stream_state.get(account_id, {})
        
        # Get current cursor value for the account
        current_cursor_value = current_stream_state[account_id].get(self.cursor_field)
        
        # Update state only if new value is more recent
        if not current_cursor_value or pendulum.parse(latest_cursor_value) > pendulum.parse(current_cursor_value):
            current_stream_state[account_id][self.cursor_field] = latest_cursor_value
            
        return current_stream_state
