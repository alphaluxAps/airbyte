#
# Copyright (c) 2024 Airbyte, Inc., all rights reserved.
#
from typing import Any, List, Mapping, Optional, Tuple

import pendulum
import logging

logger = logging.getLogger("airbyte")

from airbyte_cdk.models import ConfiguredAirbyteCatalog
from airbyte_cdk.sources.declarative.yaml_declarative_source import YamlDeclarativeSource
from airbyte_cdk.sources.source import TState
from airbyte_cdk.sources.streams.core import Stream
from source_instagram.api import InstagramAPI
from source_instagram.streams import UserInsights, MediaInsightsStream


"""
This file provides the necessary constructs to interpret a provided declarative YAML configuration file into
source connector.

WARNING: Do not modify this file.
"""


# Declarative Source
class SourceInstagram(YamlDeclarativeSource):
    def __init__(self, catalog: Optional[ConfiguredAirbyteCatalog], config: Optional[Mapping[str, Any]], state: TState, **kwargs):
        super().__init__(catalog=catalog, config=config, state=state, **{"path_to_yaml": "manifest.yaml"})

    def check_connection(self, logger, config: Mapping[str, Any]) -> Tuple[bool, Any]:
        """Connection check to validate that the user-provided config can be used to connect to the underlying API

        :param config:  the user-input config object conforming to the connector's spec.json
        :param logger:  logger object
        :return Tuple[bool, Any]: (True, None) if the input config can be used to connect to the API successfully, (False, error) otherwise.
        """
        try:
            self._validate_start_date(config)
            api = InstagramAPI(access_token=config["access_token"])
            logger.info(f"Available accounts: {api.accounts}")
        except Exception as exc:
            error_msg = repr(exc)
            return False, error_msg
        return super().check_connection(logger, config)

    def _validate_start_date(self, config):
        # If start_date is not found in config, set it to 2 years ago
        if not config.get("start_date"):
            config["start_date"] = pendulum.now().subtract(years=2).in_timezone("UTC").format("YYYY-MM-DDTHH:mm:ss[Z]")
        else:
            if pendulum.parse(config["start_date"]) > pendulum.now():
                raise ValueError("Please fix the start_date parameter in config, it cannot be in the future")

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        declarative_streams = super().streams(config)
        
        # Filter out the declarative MediaInsights stream
        filtered_streams = [s for s in declarative_streams if s.name != "media_insights"]
        
        return filtered_streams + self.get_non_low_code_streams(config=config)

    def get_non_low_code_streams(self, config: Mapping[str, Any]) -> List[Stream]:
        api = InstagramAPI(access_token=config["access_token"])
        self._validate_start_date(config)
        
        # Get lookback windows from config
        user_insights_lookback_days = config.get('user_insights_lookback_days', 7)
        media_lookback_days = config.get('media_lookback_days', 14)
        
        # Find the Media stream from the declarative streams
        media_stream = None
        for stream in super().streams(config):
            if stream.name == "media":
                media_stream = stream
                break
                
        if not media_stream:
            logger.error("Could not find Media stream in declarative streams")
            # Create a fallback empty list of streams if we can't find the Media stream
            return [UserInsights(api=api, start_date=config["start_date"], lookback_window=user_insights_lookback_days)]
                
        # Create the UserInsights stream
        user_insights = UserInsights(
            api=api, 
            start_date=config["start_date"],
            lookback_window=user_insights_lookback_days
        )
        
        # Create the MediaInsights stream if we found a Media stream
        streams = [user_insights]
        
        if media_stream:
            try:
                media_insights = MediaInsightsStream(
                    api=api,
                    start_date=config["start_date"],
                    lookback_window=media_lookback_days
                )
                # Set the media stream explicitly to ensure proper connection
                media_insights.set_media_stream(media_stream)
                streams.append(media_insights)
                logger.info("Successfully created MediaInsights stream with Media stream")
            except Exception as e:
                logger.error(f"Error creating MediaInsights stream: {e}")
                # Only return UserInsights if there was an error creating MediaInsights
        
        return streams
