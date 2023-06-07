import logging
from enum import Enum
from typing import Dict, List, Optional, cast

from pydantic import Field, SecretStr, root_validator, validator

from datahub.configuration.common import AllowDenyPattern
from datahub.configuration.pattern_utils import UUID_REGEX
from datahub.configuration.validate_field_removal import pydantic_removed_field
from datahub.configuration.validate_field_rename import pydantic_renamed_field
from datahub.ingestion.glossary.classification_mixin import (
    ClassificationSourceConfigMixin,
)
from datahub.ingestion.source.state.stateful_ingestion_base import (
    StatefulProfilingConfigMixin,
    StatefulUsageConfigMixin,
)
from datahub.ingestion.source_config.sql.snowflake import (
    BaseSnowflakeConfig,
    SnowflakeConfig,
)
from datahub.ingestion.source_config.usage.snowflake_usage import SnowflakeUsageConfig

logger = logging.Logger(__name__)

# FIVETRAN creates temporary tables in schema named FIVETRAN_xxx_STAGING.
# Ref - https://support.fivetran.com/hc/en-us/articles/1500003507122-Why-Is-There-an-Empty-Schema-Named-Fivetran-staging-in-the-Destination-
#
# DBT incremental models create temporary tables ending with __dbt_tmp
# Ref - https://discourse.getdbt.com/t/handling-bigquery-incremental-dbt-tmp-tables/7540
DEFAULT_UPSTREAMS_DENY_LIST = [
    r".*\.FIVETRAN_.*_STAGING\..*",  # fivetran
    r".*__DBT_TMP$",  # dbt
    rf".*\.SEGMENT_{UUID_REGEX}",  # segment
    rf".*\.STAGING_.*_{UUID_REGEX}",  # stitch
]


class TagOption(str, Enum):
    with_lineage = "with_lineage"
    without_lineage = "without_lineage"
    skip = "skip"


class SnowflakeV2Config(
    SnowflakeConfig,
    SnowflakeUsageConfig,
    StatefulUsageConfigMixin,
    StatefulProfilingConfigMixin,
    ClassificationSourceConfigMixin,
):
    convert_urns_to_lowercase: bool = Field(
        default=True,
    )

    include_usage_stats: bool = Field(
        default=True,
        description="If enabled, populates the snowflake usage statistics. Requires appropriate grants given to the role.",
    )

    include_technical_schema: bool = Field(
        default=True,
        description="If enabled, populates the snowflake technical schema and descriptions.",
    )

    include_column_lineage: bool = Field(
        default=True,
        description="If enabled, populates the column lineage. Supported only for snowflake table-to-table and view-to-table lineage edge (not supported in table-to-view or view-to-view lineage edge yet). Requires appropriate grants given to the role.",
    )

    _check_role_grants_removed = pydantic_removed_field("check_role_grants")
    _provision_role_removed = pydantic_removed_field("provision_role")

    # FIXME: This validator already exists in one of the parent classes, but for some reason it
    # does not have any effect there. As such, we have to re-add it here.
    rename_host_port_to_account_id = pydantic_renamed_field("host_port", "account_id")

    extract_tags: TagOption = Field(
        default=TagOption.skip,
        description="""Optional. Allowed values are `without_lineage`, `with_lineage`, and `skip` (default). `without_lineage` only extracts tags that have been applied directly to the given entity. `with_lineage` extracts both directly applied and propagated tags, but will be significantly slower. See the [Snowflake documentation](https://docs.snowflake.com/en/user-guide/object-tagging.html#tag-lineage) for information about tag lineage/propagation. """,
    )

    include_external_url: bool = Field(
        default=True,
        description="Whether to populate Snowsight url for Snowflake Objects",
    )

    match_fully_qualified_names: bool = Field(
        default=False,
        description="Whether `schema_pattern` is matched against fully qualified schema name `<catalog>.<schema>`.",
    )

    use_legacy_lineage_method: bool = Field(
        default=False,
        description="Whether to use the legacy lineage computation method. If set to False, ingestion uses new optimised lineage extraction method that requires less ingestion process memory.",
    )

    validate_upstreams_against_patterns: bool = Field(
        default=True,
        description="Whether to validate upstream snowflake tables against allow-deny patterns",
    )

    tag_pattern: AllowDenyPattern = Field(
        default=AllowDenyPattern.allow_all(),
        description="List of regex patterns for tags to include in ingestion. Only used if `extract_tags` is enabled.",
    )

    upstreams_deny_pattern: List[str] = Field(
        default=DEFAULT_UPSTREAMS_DENY_LIST,
        description="[Advanced] Regex patterns for upstream tables to filter in ingestion. Specify regex to match the entire table name in database.schema.table format. Defaults are to set in such a way to ignore the temporary staging tables created by known ETL tools. Not used if `use_legacy_lineage_method=True`",
    )

    @validator("include_column_lineage")
    def validate_include_column_lineage(cls, v, values):
        if not values.get("include_table_lineage") and v:
            raise ValueError(
                "include_table_lineage must be True for include_column_lineage to be set."
            )
        return v

    @root_validator(pre=False)
    def validate_unsupported_configs(cls, values: Dict) -> Dict:
        value = values.get("include_read_operational_stats")
        if value is not None and value:
            raise ValueError(
                "include_read_operational_stats is not supported. Set `include_read_operational_stats` to False.",
            )

        match_fully_qualified_names = values.get("match_fully_qualified_names")

        schema_pattern: Optional[AllowDenyPattern] = values.get("schema_pattern")

        if (
            schema_pattern is not None
            and schema_pattern != AllowDenyPattern.allow_all()
            and match_fully_qualified_names is not None
            and not match_fully_qualified_names
        ):
            logger.warning(
                "Please update `schema_pattern` to match against fully qualified schema name `<catalog_name>.<schema_name>` and set config `match_fully_qualified_names : True`."
                "Current default `match_fully_qualified_names: False` is only to maintain backward compatibility. "
                "The config option `match_fully_qualified_names` will be deprecated in future and the default behavior will assume `match_fully_qualified_names: True`."
            )

        # Always exclude reporting metadata for INFORMATION_SCHEMA schema
        if schema_pattern is not None and schema_pattern:
            logger.debug("Adding deny for INFORMATION_SCHEMA to schema_pattern.")
            cast(AllowDenyPattern, schema_pattern).deny.append(r".*INFORMATION_SCHEMA$")

        include_technical_schema = values.get("include_technical_schema")
        include_profiles = (
            values.get("profiling") is not None and values["profiling"].enabled
        )
        delete_detection_enabled = (
            values.get("stateful_ingestion") is not None
            and values["stateful_ingestion"].enabled
            and values["stateful_ingestion"].remove_stale_metadata
        )

        # TODO: Allow lineage extraction and profiling irrespective of basic schema extraction,
        # as it seems possible with some refactor
        if not include_technical_schema and any(
            [include_profiles, delete_detection_enabled]
        ):
            raise ValueError(
                "Cannot perform Deletion Detection or Profiling without extracting snowflake technical schema. Set `include_technical_schema` to True or disable Deletion Detection and Profiling."
            )

        return values

    def get_sql_alchemy_url(
        self,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[SecretStr] = None,
        role: Optional[str] = None,
    ) -> str:
        return BaseSnowflakeConfig.get_sql_alchemy_url(
            self, database=database, username=username, password=password, role=role
        )
