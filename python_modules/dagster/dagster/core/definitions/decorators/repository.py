from functools import update_wrapper
from typing import Any, Callable, Optional, Union

from dagster import check
from dagster.core.errors import DagsterInvalidDefinitionError

from ..graph import GraphDefinition
from ..partition import PartitionSetDefinition
from ..pipeline import PipelineDefinition
from ..repository import (
    VALID_REPOSITORY_DATA_DICT_KEYS,
    CachingRepositoryData,
    RepositoryData,
    RepositoryDefinition,
)
from ..schedule import ScheduleDefinition
from ..sensor import SensorDefinition


class _Repository:
    def __init__(self, name: Optional[str] = None, description: Optional[str] = None):
        self.name = check.opt_str_param(name, "name")
        self.description = check.opt_str_param(description, "description")

    def __call__(self, fn: Callable[[], Any]) -> RepositoryDefinition:
        check.callable_param(fn, "fn")

        if not self.name:
            self.name = fn.__name__

        repository_definitions = fn()

        if not (
            isinstance(repository_definitions, list)
            or isinstance(repository_definitions, dict)
            or isinstance(repository_definitions, RepositoryData)
        ):
            raise DagsterInvalidDefinitionError(
                "Bad return value of type {type_} from repository construction function: must "
                "return list, dict, or RepositoryData. See the @repository decorator docstring for "
                "details and examples".format(type_=type(repository_definitions)),
            )

        if isinstance(repository_definitions, list):
            bad_definitions = []
            for i, definition in enumerate(repository_definitions):
                if not (
                    isinstance(definition, PipelineDefinition)
                    or isinstance(definition, PartitionSetDefinition)
                    or isinstance(definition, ScheduleDefinition)
                    or isinstance(definition, SensorDefinition)
                    or isinstance(definition, GraphDefinition)
                ):
                    bad_definitions.append((i, type(definition)))
            if bad_definitions:
                bad_definitions_str = ", ".join(
                    [
                        "value of type {type_} at index {i}".format(type_=type_, i=i)
                        for i, type_ in bad_definitions
                    ]
                )
                raise DagsterInvalidDefinitionError(
                    "Bad return value from repository construction function: all elements of list "
                    "must be of type PipelineDefinition, PartitionSetDefinition, "
                    f"ScheduleDefinition, or SensorDefinition. Got {bad_definitions_str}."
                )
            repository_data = CachingRepositoryData.from_list(repository_definitions)

        elif isinstance(repository_definitions, dict):
            if not set(repository_definitions.keys()).issubset(VALID_REPOSITORY_DATA_DICT_KEYS):
                raise DagsterInvalidDefinitionError(
                    "Bad return value from repository construction function: dict must not contain "
                    "keys other than {{'pipelines', 'partition_sets', 'schedules', 'jobs'}}: found "
                    "{bad_keys}".format(
                        bad_keys=", ".join(
                            [
                                "'{key}'".format(key=key)
                                for key in repository_definitions.keys()
                                if key not in VALID_REPOSITORY_DATA_DICT_KEYS
                            ]
                        )
                    )
                )
            repository_data = CachingRepositoryData.from_dict(repository_definitions)
        elif isinstance(repository_definitions, RepositoryData):
            repository_data = repository_definitions

        repository_def = RepositoryDefinition(
            name=self.name, description=self.description, repository_data=repository_data
        )

        update_wrapper(repository_def, fn)
        return repository_def


def repository(
    name: Union[Optional[str], Callable[..., Any]] = None, description: Optional[str] = None
) -> Union[_Repository, RepositoryDefinition]:
    """Create a repository from the decorated function.

    The decorated function should take no arguments and its return value should one of:

    1. ``List[Union[PipelineDefinition, PartitionSetDefinition, ScheduleDefinition, SensorDefinition]]``.
        Use this form when you have no need to lazy load pipelines or other definitions. This is the
        typical use case.

    2. A dict of the form:

    .. code-block:: python

        {
            'pipelines': Dict[str, Callable[[], PipelineDefinition]],
            'partition_sets': Dict[str, Callable[[], PartitionSetDefinition]],
            'schedules': Dict[str, Callable[[], ScheduleDefinition]]
            'sensors': Dict[str, Callable[[], SensorDefinition]]
        }

    This form is intended to allow definitions to be created lazily when accessed by name,
    which can be helpful for performance when there are many definitions in a repository, or
    when constructing the definitions is costly.

    3. An object of type :py:class:`RepositoryData`. Return this object if you need fine-grained
        control over the construction and indexing of definitions within the repository, e.g., to
        create definitions dynamically from .yaml files in a directory.

    Args:
        name (Optional[str]): The name of the repository. Defaults to the name of the decorated
            function.
        description (Optional[str]): A string description of the repository.

    Example:

    .. code-block:: python

        ######################################################################
        # A simple repository using the first form of the decorated function
        ######################################################################

        @solid(config_schema={n: Field(Int)})
        def return_n(context):
            return context.solid_config['n']

        @pipeline(name='simple_pipeline')
        def simple_pipeline():
            return_n()

        simple_partition_set = PartitionSetDefinition(
            name='simple_partition_set',
            pipeline_name='simple_pipeline',
            partition_fn=lambda: range(10),
            run_config_fn_for_partition=(
                lambda partition: {
                    'solids': {'return_n': {'config': {'n': partition}}}
                }
            ),
        )

        simple_schedule = simple_partition_set.create_schedule_definition(
            schedule_name='simple_daily_10_pm_schedule',
            cron_schedule='0 22 * * *',
        )

        @repository
        def simple_repository():
            return [simple_pipeline, simple_partition_set, simple_schedule]


        ######################################################################
        # A lazy-loaded repository
        ######################################################################

        def make_expensive_pipeline():
            @pipeline(name='expensive_pipeline')
            def expensive_pipeline():
                for i in range(10000):
                    return_n.alias('return_n_{i}'.format(i=i))()

            return expensive_pipeline

        expensive_partition_set = PartitionSetDefinition(
            name='expensive_partition_set',
            pipeline_name='expensive_pipeline',
            partition_fn=lambda: range(10),
            run_config_fn_for_partition=(
                lambda partition: {
                    'solids': {
                        'return_n_{i}'.format(i=i): {'config': {'n': partition}}
                        for i in range(10000)
                    }
                }
            ),
        )

        def make_expensive_schedule():
            expensive_partition_set.create_schedule_definition(
                schedule_name='expensive_schedule',
                cron_schedule='0 22 * * *',
        )

        @repository
        def lazy_loaded_repository():
            return {
                'pipelines': {'expensive_pipeline': make_expensive_pipeline},
                'partition_sets': {
                    'expensive_partition_set': expensive_partition_set
                },
                'schedules': {'expensive_schedule: make_expensive_schedule}
            }


        ######################################################################
        # A complex repository that lazily construct pipelines from a directory
        # of files in a bespoke YAML format
        ######################################################################

        class ComplexRepositoryData(RepositoryData):
            def __init__(self, yaml_directory):
                self._yaml_directory = yaml_directory

            def get_all_pipelines(self):
                return [
                    self._construct_pipeline_def_from_yaml_file(
                      self._yaml_file_for_pipeline_name(file_name)
                    )
                    for file_name in os.listdir(self._yaml_directory)
                ]

            ...

        @repository
        def complex_repository():
            return ComplexRepositoryData('some_directory')

    """
    if callable(name):
        check.invariant(description is None)

        return _Repository()(name)

    return _Repository(name=name, description=description)
