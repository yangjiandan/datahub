import random
import string
from datetime import datetime, timezone
from typing import cast
from unittest import mock

import pandas as pd
import pytest
from freezegun import freeze_time

from datahub.configuration.common import AllowDenyPattern, DynamicTypedConfig
from datahub.ingestion.glossary.classifier import (
    ClassificationConfig,
    DynamicTypedClassifierConfig,
)
from datahub.ingestion.glossary.datahub_classifier import (
    DataHubClassifierConfig,
    InfoTypeConfig,
    PredictionFactorsAndWeights,
    ValuesFactorConfig,
)
from datahub.ingestion.run.pipeline import Pipeline
from datahub.ingestion.run.pipeline_config import PipelineConfig, SourceConfig
from datahub.ingestion.source.ge_profiling_config import GEProfilingConfig
from datahub.ingestion.source.snowflake.snowflake_config import (
    SnowflakeV2Config,
    TagOption,
)
from datahub.ingestion.source.snowflake.snowflake_report import SnowflakeV2Report
from tests.integration.snowflake.common import FROZEN_TIME, default_query_results
from tests.test_helpers import mce_helpers


def random_email():
    return (
        "".join(
            [
                random.choice(string.ascii_lowercase)
                for i in range(random.randint(10, 15))
            ]
        )
        + "@xyz.com"
    )


def random_cloud_region():
    return "".join(
        [
            random.choice(["af", "ap", "ca", "eu", "me", "sa", "us"]),
            "-",
            random.choice(["central", "north", "south", "east", "west"]),
            "-",
            str(random.randint(1, 2)),
        ]
    )


@freeze_time(FROZEN_TIME)
@pytest.mark.integration
def test_snowflake_basic(pytestconfig, tmp_path, mock_time, mock_datahub_graph):
    test_resources_dir = pytestconfig.rootpath / "tests/integration/snowflake"

    # Run the metadata ingestion pipeline.
    output_file = tmp_path / "snowflake_test_events.json"
    golden_file = test_resources_dir / "snowflake_golden.json"

    with mock.patch("snowflake.connector.connect") as mock_connect, mock.patch(
        "datahub.ingestion.source.snowflake.snowflake_v2.SnowflakeV2Source.get_sample_values_for_table"
    ) as mock_sample_values:
        sf_connection = mock.MagicMock()
        sf_cursor = mock.MagicMock()
        mock_connect.return_value = sf_connection
        sf_connection.cursor.return_value = sf_cursor

        sf_cursor.execute.side_effect = default_query_results

        mock_sample_values.return_value = pd.DataFrame(
            data={
                "col_1": [random.randint(0, 100) for i in range(1, 100)],
                "col_2": [random_email() for i in range(1, 100)],
                "col_3": [random_cloud_region() for i in range(1, 100)],
            }
        )

        datahub_classifier_config = DataHubClassifierConfig()
        datahub_classifier_config.confidence_level_threshold = 0.58
        datahub_classifier_config.info_types_config = {
            "Age": InfoTypeConfig(
                Prediction_Factors_and_Weights=PredictionFactorsAndWeights(
                    Name=0, Values=1, Description=0, Datatype=0
                )
            ),
            "CloudRegion": InfoTypeConfig(
                Prediction_Factors_and_Weights=PredictionFactorsAndWeights(
                    Name=0,
                    Description=0,
                    Datatype=0,
                    Values=1,
                ),
                Values=ValuesFactorConfig(
                    prediction_type="regex",
                    regex=[
                        r"(af|ap|ca|eu|me|sa|us)-(central|north|(north(?:east|west))|south|south(?:east|west)|east|west)-\d+"
                    ],
                ),
            ),
        }
        pipeline = Pipeline(
            config=PipelineConfig(
                source=SourceConfig(
                    type="snowflake",
                    config=SnowflakeV2Config(
                        account_id="ABC12345.ap-south-1.aws",
                        username="TST_USR",
                        password="TST_PWD",
                        match_fully_qualified_names=True,
                        schema_pattern=AllowDenyPattern(allow=["test_db.test_schema"]),
                        include_technical_schema=True,
                        include_table_lineage=True,
                        include_view_lineage=True,
                        include_usage_stats=False,
                        use_legacy_lineage_method=False,
                        validate_upstreams_against_patterns=False,
                        include_operational_stats=True,
                        start_time=datetime(2022, 6, 6, 7, 17, 0, 0).replace(
                            tzinfo=timezone.utc
                        ),
                        end_time=datetime(2022, 6, 7, 7, 17, 0, 0).replace(
                            tzinfo=timezone.utc
                        ),
                        classification=ClassificationConfig(
                            enabled=True,
                            column_pattern=AllowDenyPattern(
                                allow=[".*col_1$", ".*col_2$", ".*col_3$"]
                            ),
                            classifiers=[
                                DynamicTypedClassifierConfig(
                                    type="datahub", config=datahub_classifier_config
                                )
                            ],
                        ),
                        profiling=GEProfilingConfig(
                            enabled=True,
                            profile_if_updated_since_days=None,
                            profile_table_row_limit=None,
                            profile_table_size_limit=None,
                            profile_table_level_only=True,
                        ),
                        extract_tags=TagOption.without_lineage,
                    ),
                ),
                sink=DynamicTypedConfig(
                    type="file", config={"filename": str(output_file)}
                ),
            )
        )
        pipeline.run()
        pipeline.pretty_print_summary()
        pipeline.raise_from_status()

        # Verify the output.

        mce_helpers.check_golden_file(
            pytestconfig,
            output_path=output_file,
            golden_path=golden_file,
            ignore_paths=[],
        )
        report = cast(SnowflakeV2Report, pipeline.source.get_report())
        assert report.lru_cache_info["get_tables_for_database"]["misses"] == 1
        assert report.lru_cache_info["get_views_for_database"]["misses"] == 1
        assert report.lru_cache_info["get_columns_for_schema"]["misses"] == 1
        assert report.lru_cache_info["get_pk_constraints_for_schema"]["misses"] == 1
        assert report.lru_cache_info["get_fk_constraints_for_schema"]["misses"] == 1


@freeze_time(FROZEN_TIME)
@pytest.mark.integration
def test_snowflake_private_link(pytestconfig, tmp_path, mock_time, mock_datahub_graph):
    test_resources_dir = pytestconfig.rootpath / "tests/integration/snowflake"

    # Run the metadata ingestion pipeline.
    output_file = tmp_path / "snowflake_privatelink_test_events.json"
    golden_file = test_resources_dir / "snowflake_privatelink_golden.json"

    with mock.patch("snowflake.connector.connect") as mock_connect:
        sf_connection = mock.MagicMock()
        sf_cursor = mock.MagicMock()
        mock_connect.return_value = sf_connection
        sf_connection.cursor.return_value = sf_cursor
        sf_cursor.execute.side_effect = default_query_results

        pipeline = Pipeline(
            config=PipelineConfig(
                source=SourceConfig(
                    type="snowflake",
                    config=SnowflakeV2Config(
                        account_id="ABC12345.ap-south-1.privatelink",
                        username="TST_USR",
                        password="TST_PWD",
                        schema_pattern=AllowDenyPattern(allow=["test_schema"]),
                        include_technical_schema=True,
                        include_table_lineage=True,
                        include_column_lineage=False,
                        include_views=False,
                        include_view_lineage=False,
                        use_legacy_lineage_method=False,
                        include_usage_stats=False,
                        include_operational_stats=False,
                        start_time=datetime(2022, 6, 6, 7, 17, 0, 0).replace(
                            tzinfo=timezone.utc
                        ),
                        end_time=datetime(2022, 6, 7, 7, 17, 0, 0).replace(
                            tzinfo=timezone.utc
                        ),
                    ),
                ),
                sink=DynamicTypedConfig(
                    type="file", config={"filename": str(output_file)}
                ),
            )
        )
        pipeline.run()
        pipeline.pretty_print_summary()
        pipeline.raise_from_status()

        # Verify the output.

        mce_helpers.check_golden_file(
            pytestconfig,
            output_path=output_file,
            golden_path=golden_file,
            ignore_paths=[],
        )
