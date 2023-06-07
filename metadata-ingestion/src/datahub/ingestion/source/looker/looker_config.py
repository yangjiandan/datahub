import dataclasses
import os
import re
from typing import Any, Dict, List, Optional, Union

import pydantic
from pydantic import Field, validator
from typing_extensions import ClassVar

from datahub.configuration import ConfigModel
from datahub.configuration.common import AllowDenyPattern, ConfigurationError
from datahub.configuration.source_common import DatasetSourceConfigMixin, EnvConfigMixin
from datahub.configuration.validate_field_removal import pydantic_removed_field
from datahub.ingestion.source.looker.looker_lib_wrapper import LookerAPIConfig
from datahub.ingestion.source.state.stale_entity_removal_handler import (
    StatefulStaleMetadataRemovalConfig,
)
from datahub.ingestion.source.state.stateful_ingestion_base import (
    StatefulIngestionConfigBase,
)


class NamingPattern(ConfigModel):
    ALLOWED_VARS: ClassVar[List[str]] = []
    REQUIRE_AT_LEAST_ONE_VAR: ClassVar[bool] = True

    pattern: str

    @classmethod
    def __get_validators__(cls):
        yield cls.pydantic_accept_raw_pattern
        yield cls.validate
        yield cls.pydantic_validate_pattern

    @classmethod
    def pydantic_accept_raw_pattern(cls, v):
        if isinstance(v, (NamingPattern, dict)):
            return v
        assert isinstance(v, str), "pattern must be a string"
        return {"pattern": v}

    @classmethod
    def pydantic_validate_pattern(cls, v):
        assert isinstance(v, NamingPattern)
        assert v.validate_pattern(cls.REQUIRE_AT_LEAST_ONE_VAR)
        return v

    @classmethod
    def allowed_docstring(cls) -> str:
        return f"Allowed variables are {cls.ALLOWED_VARS}"

    def validate_pattern(self, at_least_one: bool) -> bool:
        variables = re.findall("({[^}{]+})", self.pattern)

        variables = [v[1:-1] for v in variables]  # remove the {}

        for v in variables:
            if v not in self.ALLOWED_VARS:
                raise ConfigurationError(
                    f"Failed to find {v} in allowed_variables {self.ALLOWED_VARS}"
                )
        if at_least_one and len(variables) == 0:
            raise ConfigurationError(
                f"Failed to find any variable assigned to pattern {self.pattern}. Must have at least one. {self.allowed_docstring()}"
            )
        return True

    def replace_variables(self, values: Union[Dict[str, Optional[str]], object]) -> str:
        if not isinstance(values, dict):
            # Check that this is a dataclass instance (not a dataclass type).
            assert dataclasses.is_dataclass(values) and not isinstance(values, type)
            values = dataclasses.asdict(values)
        values = {k: v for k, v in values.items() if v is not None}
        return self.pattern.format(**values)


@dataclasses.dataclass
class NamingPatternMapping:
    platform: str
    env: str
    project: str
    model: str
    name: str


class LookerNamingPattern(NamingPattern):
    ALLOWED_VARS = [field.name for field in dataclasses.fields(NamingPatternMapping)]


class LookerCommonConfig(DatasetSourceConfigMixin):
    explore_naming_pattern: LookerNamingPattern = pydantic.Field(
        description=f"Pattern for providing dataset names to explores. {LookerNamingPattern.allowed_docstring()}",
        default=LookerNamingPattern(pattern="{model}.explore.{name}"),
    )
    explore_browse_pattern: LookerNamingPattern = pydantic.Field(
        description=f"Pattern for providing browse paths to explores. {LookerNamingPattern.allowed_docstring()}",
        default=LookerNamingPattern(pattern="/{env}/{platform}/{project}/explores"),
    )
    view_naming_pattern: LookerNamingPattern = Field(
        LookerNamingPattern(pattern="{project}.view.{name}"),
        description=f"Pattern for providing dataset names to views. {LookerNamingPattern.allowed_docstring()}",
    )
    view_browse_pattern: LookerNamingPattern = Field(
        LookerNamingPattern(pattern="/{env}/{platform}/{project}/views"),
        description=f"Pattern for providing browse paths to views. {LookerNamingPattern.allowed_docstring()}",
    )
    tag_measures_and_dimensions: bool = Field(
        True,
        description="When enabled, attaches tags to measures, dimensions and dimension groups to make them more "
        "discoverable. When disabled, adds this information to the description of the column.",
    )
    platform_name: str = Field(
        "looker", description="Default platform name. Don't change."
    )
    extract_column_level_lineage: bool = Field(
        True,
        description="When enabled, extracts column-level lineage from Views and Explores",
    )


class LookerDashboardSourceConfig(
    LookerAPIConfig,
    LookerCommonConfig,
    StatefulIngestionConfigBase,
    EnvConfigMixin,
):
    _removed_github_info = pydantic_removed_field("github_info")

    dashboard_pattern: AllowDenyPattern = Field(
        AllowDenyPattern.allow_all(),
        description="Patterns for selecting dashboard ids that are to be included",
    )
    chart_pattern: AllowDenyPattern = Field(
        AllowDenyPattern.allow_all(),
        description="Patterns for selecting chart ids that are to be included",
    )
    include_deleted: bool = Field(
        False, description="Whether to include deleted dashboards."
    )
    extract_owners: bool = Field(
        True,
        description="When enabled, extracts ownership from Looker directly. When disabled, ownership is left empty "
        "for dashboards and charts.",
    )
    actor: Optional[str] = Field(
        None,
        description="This config is deprecated in favor of `extract_owners`. Previously, was the actor to use in "
        "ownership properties of ingested metadata.",
    )
    strip_user_ids_from_email: bool = Field(
        False,
        description="When enabled, converts Looker user emails of the form name@domain.com to urn:li:corpuser:name "
        "when assigning ownership",
    )
    skip_personal_folders: bool = Field(
        False,
        description="Whether to skip ingestion of dashboards in personal folders. Setting this to True will only "
        "ingest dashboards in the Shared folder space.",
    )
    max_threads: int = Field(
        os.cpu_count() or 40,
        description="Max parallelism for Looker API calls. Defaults to cpuCount or 40",
    )
    external_base_url: Optional[str] = Field(
        None,
        description="Optional URL to use when constructing external URLs to Looker if the `base_url` is not the "
        "correct one to use. For example, `https://looker-public.company.com`. If not provided, "
        "the external base URL will default to `base_url`.",
    )
    extract_usage_history: bool = Field(
        True,
        description="Whether to ingest usage statistics for dashboards. Setting this to True will query looker system "
        "activity explores to fetch historical dashboard usage.",
    )
    # TODO - stateful ingestion to autodetect usage history interval
    extract_usage_history_for_interval: str = Field(
        "30 days",
        description="Used only if extract_usage_history is set to True. Interval to extract looker dashboard usage "
        "history for. See https://docs.looker.com/reference/filter-expressions#date_and_time.",
    )
    extract_embed_urls: bool = Field(
        True,
        description="Produce URLs used to render Looker Explores as Previews inside of DataHub UI. Embeds must be "
        "enabled inside of Looker to use this feature.",
    )
    stateful_ingestion: Optional[StatefulStaleMetadataRemovalConfig] = Field(
        default=None, description=""
    )

    @validator("external_base_url", pre=True, always=True)
    def external_url_defaults_to_api_config_base_url(
        cls, v: Optional[str], *, values: Dict[str, Any], **kwargs: Dict[str, Any]
    ) -> Optional[str]:
        return v or values.get("base_url")
