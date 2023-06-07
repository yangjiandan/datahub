import logging
from collections import defaultdict
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    TypeVar,
    Union,
)

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.metadata.schema_classes import (
    BrowsePathEntryClass,
    BrowsePathsClass,
    BrowsePathsV2Class,
    ContainerClass,
    MetadataChangeEventClass,
    MetadataChangeProposalClass,
    StatusClass,
    TagKeyClass,
)
from datahub.utilities.urns.tag_urn import TagUrn
from datahub.utilities.urns.urn import guess_entity_type
from datahub.utilities.urns.urn_iter import list_urns

if TYPE_CHECKING:
    from datahub.ingestion.api.source import SourceReport
    from datahub.ingestion.source.state.stale_entity_removal_handler import (
        StaleEntityRemovalHandler,
    )

logger = logging.getLogger(__name__)


def auto_workunit(
    stream: Iterable[Union[MetadataChangeEventClass, MetadataChangeProposalWrapper]]
) -> Iterable[MetadataWorkUnit]:
    """Convert a stream of MCEs and MCPs to a stream of :class:`MetadataWorkUnit`s."""

    for item in stream:
        if isinstance(item, MetadataChangeEventClass):
            yield MetadataWorkUnit(id=f"{item.proposedSnapshot.urn}/mce", mce=item)
        else:
            yield item.as_workunit()


def auto_status_aspect(
    stream: Iterable[MetadataWorkUnit],
) -> Iterable[MetadataWorkUnit]:
    """
    For all entities that don't have a status aspect, add one with removed set to false.
    """

    all_urns: Set[str] = set()
    status_urns: Set[str] = set()
    for wu in stream:
        urn = wu.get_urn()
        all_urns.add(urn)

        if not wu.is_primary_source:
            # If this is a non-primary source, we pretend like we've seen the status
            # aspect so that we don't try to emit a removal for it.
            status_urns.add(urn)
        elif isinstance(wu.metadata, MetadataChangeEventClass):
            if any(
                isinstance(aspect, StatusClass)
                for aspect in wu.metadata.proposedSnapshot.aspects
            ):
                status_urns.add(urn)
        elif isinstance(wu.metadata, MetadataChangeProposalWrapper):
            if isinstance(wu.metadata.aspect, StatusClass):
                status_urns.add(urn)
        elif isinstance(wu.metadata, MetadataChangeProposalClass):
            if wu.metadata.aspectName == StatusClass.ASPECT_NAME:
                status_urns.add(urn)
        else:
            raise ValueError(f"Unexpected type {type(wu.metadata)}")

        yield wu

    for urn in sorted(all_urns - status_urns):
        yield MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=StatusClass(removed=False),
        ).as_workunit()


def _default_entity_type_fn(wu: MetadataWorkUnit) -> Optional[str]:
    urn = wu.get_urn()
    entity_type = guess_entity_type(urn)
    return entity_type


def auto_stale_entity_removal(
    stale_entity_removal_handler: "StaleEntityRemovalHandler",
    stream: Iterable[MetadataWorkUnit],
    entity_type_fn: Callable[
        [MetadataWorkUnit], Optional[str]
    ] = _default_entity_type_fn,
) -> Iterable[MetadataWorkUnit]:
    """
    Record all entities that are found, and emit removals for any that disappeared in this run.
    """

    for wu in stream:
        urn = wu.get_urn()

        if wu.is_primary_source:
            entity_type = entity_type_fn(wu)
            if entity_type is not None:
                stale_entity_removal_handler.add_entity_to_state(entity_type, urn)
        else:
            stale_entity_removal_handler.add_urn_to_skip(urn)

        yield wu

    # Clean up stale entities.
    yield from stale_entity_removal_handler.gen_removed_entity_workunits()


T = TypeVar("T", bound=MetadataWorkUnit)


def auto_workunit_reporter(report: "SourceReport", stream: Iterable[T]) -> Iterable[T]:
    """
    Calls report.report_workunit() on each workunit.
    """

    for wu in stream:
        report.report_workunit(wu)
        yield wu


def auto_materialize_referenced_tags(
    stream: Iterable[MetadataWorkUnit],
) -> Iterable[MetadataWorkUnit]:
    """For all references to tags, emit a tag key aspect to ensure that the tag exists in our backend."""

    referenced_tags = set()
    tags_with_aspects = set()

    for wu in stream:
        for urn in list_urns(wu.metadata):
            if guess_entity_type(urn) == "tag":
                referenced_tags.add(urn)

        urn = wu.get_urn()
        if guess_entity_type(urn) == "tag":
            tags_with_aspects.add(urn)

        yield wu

    for urn in sorted(referenced_tags - tags_with_aspects):
        tag_urn = TagUrn.create_from_string(urn)

        yield MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=TagKeyClass(name=tag_urn.get_entity_id()[0]),
        ).as_workunit()


def auto_browse_path_v2(
    drop_dirs: Sequence[str],
    stream: Iterable[MetadataWorkUnit],
) -> Iterable[MetadataWorkUnit]:
    """Generate BrowsePathsV2 from Container and BrowsePaths aspects."""

    ignore_urns: Set[str] = set()
    legacy_browse_paths: Dict[str, List[str]] = defaultdict(list)
    container_urns: Set[str] = set()
    parent_container_map: Dict[str, str] = {}
    children: Dict[str, List[str]] = defaultdict(list)
    for wu in stream:
        yield wu

        urn = wu.get_urn()
        if guess_entity_type(urn) == "container":
            container_urns.add(urn)

        container_aspects = wu.get_aspects_of_type(ContainerClass)
        for c_aspect in container_aspects:
            parent = c_aspect.container
            parent_container_map[urn] = parent
            children[parent].append(urn)

        browse_path_aspects = wu.get_aspects_of_type(BrowsePathsClass)
        for b_aspect in browse_path_aspects:
            if b_aspect.paths:
                path = b_aspect.paths[0]  # Only take first path
                legacy_browse_paths[urn] = [
                    p for p in path.strip("/").split("/") if p.strip() not in drop_dirs
                ]

        if wu.get_aspects_of_type(BrowsePathsV2Class):
            ignore_urns.add(urn)

    paths: Dict[str, List[str]] = {}  # Maps urn -> list of urns in path
    # Yield browse paths v2 in topological order, starting with root containers
    processed_urns = set()
    nodes = container_urns - parent_container_map.keys()
    while nodes:
        node = nodes.pop()
        nodes.update(children[node])

        if node not in parent_container_map:  # root
            paths[node] = []
        else:
            parent = parent_container_map[node]
            paths[node] = [*paths[parent], parent]
        if node not in ignore_urns:
            yield MetadataChangeProposalWrapper(
                entityUrn=node,
                aspect=BrowsePathsV2Class(
                    path=[BrowsePathEntryClass(id=urn, urn=urn) for urn in paths[node]]
                ),
            ).as_workunit()
            processed_urns.add(node)

    # Yield browse paths v2 based on browse paths v1 (legacy)
    # Only done if the entity is not part of a container hierarchy
    for urn in legacy_browse_paths.keys() - processed_urns - ignore_urns:
        yield MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=BrowsePathsV2Class(
                path=[BrowsePathEntryClass(id=p) for p in legacy_browse_paths[urn]]
            ),
        ).as_workunit()
