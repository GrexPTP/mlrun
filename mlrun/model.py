# Copyright 2023 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import json
import pathlib
import re
import time
import typing
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
from os import environ
from typing import Any, Dict, List, Optional, Tuple, Union

import pydantic.error_wrappers

import mlrun
import mlrun.common.schemas.notification

from .utils import (
    dict_to_json,
    dict_to_yaml,
    fill_project_path_template,
    get_artifact_target,
    is_legacy_artifact,
    logger,
)

# Changing {run_id} will break and will not be backward compatible.
RUN_ID_PLACE_HOLDER = "{run_id}"  # IMPORTANT: shouldn't be changed.


class ModelObj:
    _dict_fields = []

    @staticmethod
    def _verify_list(param, name):
        if not isinstance(param, list):
            raise ValueError(f"Parameter {name} must be a list")

    @staticmethod
    def _verify_dict(param, name, new_type=None):
        if (
            param is not None
            and not isinstance(param, dict)
            and not hasattr(param, "to_dict")
        ):
            raise ValueError(f"Parameter {name} must be a dict or object")
        if new_type and (isinstance(param, dict) or param is None):
            return new_type.from_dict(param)
        return param

    def to_dict(self, fields=None, exclude=None):
        """convert the object to a python dictionary

        :param fields:  list of fields to include in the dict
        :param exclude: list of fields to exclude from the dict
        """
        struct = {}
        fields = fields or self._dict_fields
        if not fields:
            fields = list(inspect.signature(self.__init__).parameters.keys())
        for t in fields:
            if not exclude or t not in exclude:
                val = getattr(self, t, None)
                if val is not None and not (isinstance(val, dict) and not val):
                    if hasattr(val, "to_dict"):
                        val = val.to_dict()
                        if val:
                            struct[t] = val
                    else:
                        struct[t] = val
        return struct

    @classmethod
    def from_dict(cls, struct=None, fields=None, deprecated_fields: dict = None):
        """create an object from a python dictionary"""
        struct = {} if struct is None else struct
        deprecated_fields = deprecated_fields or {}
        fields = fields or cls._dict_fields
        if not fields:
            fields = list(inspect.signature(cls.__init__).parameters.keys())
        new_obj = cls()
        if struct:
            # we are looping over the fields to save the same order and behavior in which the class
            # initialize the attributes
            for field in fields:
                # we want to set the field only if the field exists in struct
                if field in struct:
                    field_val = struct.get(field, None)
                    if field not in deprecated_fields:
                        setattr(new_obj, field, field_val)

            for deprecated_field, new_field in deprecated_fields.items():
                field_value = struct.get(new_field) or struct.get(deprecated_field)
                if field_value:
                    setattr(new_obj, new_field, field_value)

        return new_obj

    def to_yaml(self, exclude=None) -> str:
        """convert the object to yaml

        :param exclude: list of fields to exclude from the yaml
        """
        return dict_to_yaml(self.to_dict(exclude=exclude))

    def to_json(self, exclude=None):
        """convert the object to json

        :param exclude: list of fields to exclude from the json
        """
        return dict_to_json(self.to_dict(exclude=exclude))

    def to_str(self):
        """convert the object to string (with dict layout)"""
        return self.__str__()

    def __str__(self):
        return str(self.to_dict())

    def copy(self):
        """create a copy of the object"""
        return deepcopy(self)


# model class for building ModelObj dictionaries
class ObjectDict:
    kind = "object_dict"

    def __init__(self, classes_map, default_kind=""):
        self._children = OrderedDict()
        self._default_kind = default_kind
        self._classes_map = classes_map

    def values(self):
        return self._children.values()

    def keys(self):
        return self._children.keys()

    def items(self):
        return self._children.items()

    def __len__(self):
        return len(self._children)

    def __iter__(self):
        yield from self._children.keys()

    def __getitem__(self, name):
        return self._children[name]

    def __setitem__(self, key, item):
        self._children[key] = self._get_child_object(item, key)

    def __delitem__(self, key):
        del self._children[key]

    def update(self, key, item):
        child = self._get_child_object(item, key)
        self._children[key] = child
        return child

    def to_dict(self):
        return {k: v.to_dict() for k, v in self._children.items()}

    @classmethod
    def from_dict(cls, classes_map: dict, children=None, default_kind=""):
        if children is None:
            return cls(classes_map, default_kind)
        if not isinstance(children, dict):
            raise ValueError("children must be a dict")

        new_obj = cls(classes_map, default_kind)
        for name, child in children.items():
            obj_name = name
            if hasattr(child, "name") and child.name is not None:
                obj_name = child.name
            elif isinstance(child, dict) and "name" in child:
                obj_name = child["name"]
            child_obj = new_obj._get_child_object(child, obj_name)
            new_obj._children[name] = child_obj

        return new_obj

    def _get_child_object(self, child, name):
        if hasattr(child, "kind") and child.kind in self._classes_map.keys():
            child.name = name
            return child
        elif isinstance(child, dict):
            kind = child.get("kind", self._default_kind)
            if kind not in self._classes_map.keys():
                raise ValueError(f"illegal object kind {kind}")
            child_obj = self._classes_map[kind].from_dict(child)
            child_obj.name = name
            return child_obj
        else:
            raise ValueError(f"illegal child (should be dict or child kind), {child}")

    def to_yaml(self):
        return dict_to_yaml(self.to_dict())

    def to_json(self):
        return dict_to_json(self.to_dict())

    def to_str(self):
        return self.__str__()

    def __str__(self):
        return str(self.to_dict())

    def copy(self):
        return deepcopy(self)


class ObjectList:
    def __init__(self, child_class):
        self._children = OrderedDict()
        self._child_class = child_class

    def values(self):
        return self._children.values()

    def keys(self):
        return self._children.keys()

    def items(self):
        return self._children.items()

    def __len__(self):
        return len(self._children)

    def __iter__(self):
        yield from self._children.values()

    def __getitem__(self, name):
        if isinstance(name, int):
            return list(self._children.values())[name]
        return self._children[name]

    def __setitem__(self, key, item):
        self.update(item, key)

    def __delitem__(self, key):
        del self._children[key]

    def to_dict(self):
        # method used by ModelObj class to serialize the object to nested dict
        return [t.to_dict() for t in self._children.values()]

    @classmethod
    def from_list(cls, child_class, children=None):
        if children is None:
            return cls(child_class)
        if not isinstance(children, list):
            raise ValueError("states must be a list")

        new_obj = cls(child_class)
        for child in children:
            name, child_obj = new_obj._get_child_object(child)
            new_obj._children[name] = child_obj
        return new_obj

    def _get_child_object(self, child):
        if isinstance(child, self._child_class):
            return child.name, child
        elif isinstance(child, dict):
            if "name" not in child.keys():
                raise ValueError("illegal object no 'name' field")
            child_obj = self._child_class.from_dict(child)
            return child_obj.name, child_obj
        else:
            raise ValueError(f"illegal child (should be dict or child kind), {child}")

    def update(self, child, name=None):
        object_name, child_obj = self._get_child_object(child)
        child_obj.name = name or object_name
        self._children[child_obj.name] = child_obj
        return child_obj


class Credentials(ModelObj):
    generate_access_key = "$generate"
    secret_reference_prefix = "$ref:"

    def __init__(
        self,
        access_key: str = None,
    ):
        self.access_key = access_key


class BaseMetadata(ModelObj):
    def __init__(
        self,
        name=None,
        tag=None,
        hash=None,
        namespace=None,
        project=None,
        labels=None,
        annotations=None,
        categories=None,
        updated=None,
        credentials=None,
    ):
        self.name = name
        self.tag = tag
        self.hash = hash
        self.namespace = namespace
        self.project = project or ""
        self.labels = labels or {}
        self.categories = categories or []
        self.annotations = annotations or {}
        self.updated = updated
        self._credentials = None
        self.credentials = credentials

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    @credentials.setter
    def credentials(self, credentials):
        self._credentials = self._verify_dict(credentials, "credentials", Credentials)


class ImageBuilder(ModelObj):
    """An Image builder"""

    def __init__(
        self,
        functionSourceCode=None,
        source=None,
        image=None,
        base_image=None,
        commands=None,
        extra=None,
        secret=None,
        code_origin=None,
        registry=None,
        load_source_on_run=None,
        origin_filename=None,
        with_mlrun=None,
        auto_build=None,
        requirements: list = None,
        extra_args=None,
        builder_env=None,
    ):
        self.functionSourceCode = functionSourceCode  #: functionSourceCode
        self.codeEntryType = ""  #: codeEntryType
        self.codeEntryAttributes = ""  #: codeEntryAttributes
        self.source = source  #: source
        self.code_origin = code_origin  #: code_origin
        self.origin_filename = origin_filename
        self.image = image  #: image
        self.base_image = base_image  #: base_image
        self.commands = commands or []  #: commands
        self.extra = extra  #: extra
        self.extra_args = extra_args  #: extra args
        self.builder_env = builder_env  #: builder env
        self.secret = secret  #: secret
        self.registry = registry  #: registry
        self.load_source_on_run = load_source_on_run  #: load_source_on_run
        self.with_mlrun = with_mlrun  #: with_mlrun
        self.auto_build = auto_build  #: auto_build
        self.build_pod = None
        self.requirements = requirements or []  #: pip requirements

    @property
    def source(self):
        return self._source

    @source.setter
    def source(self, source):
        if source and not (
            source.startswith("git://")
            # lenient check for file extension because we support many file types locally and remotely
            or pathlib.Path(source).suffix
            or source in [".", "./"]
        ):
            raise mlrun.errors.MLRunInvalidArgumentError(
                f"source ({source}) must be a compressed (tar.gz / zip) file, a git repo, "
                f"a file path or in the project's context (.)"
            )

        self._source = source

    def build_config(
        self,
        image="",
        base_image=None,
        commands: list = None,
        secret=None,
        source=None,
        extra=None,
        load_source_on_run=None,
        with_mlrun=None,
        auto_build=None,
        requirements=None,
        requirements_file=None,
        overwrite=False,
        builder_env=None,
        extra_args=None,
    ):
        if image:
            self.image = image
        if base_image:
            self.base_image = base_image
        if commands:
            self.with_commands(commands, overwrite=overwrite)
        if requirements or requirements_file:
            self.with_requirements(requirements, requirements_file, overwrite=overwrite)
        if extra:
            self.extra = extra
        if secret is not None:
            self.secret = secret
        if source:
            self.source = source
        if load_source_on_run:
            self.load_source_on_run = load_source_on_run
        if with_mlrun is not None:
            self.with_mlrun = with_mlrun
        if auto_build:
            self.auto_build = auto_build
        if builder_env:
            self.builder_env = builder_env
        if extra_args:
            self.extra_args = extra_args

    def with_commands(
        self,
        commands: List[str],
        overwrite: bool = False,
    ):
        """add commands to build spec.

        :param commands:  list of commands to run during build
        :param overwrite: whether to overwrite the existing commands or add to them (the default)

        :return: function object
        """
        if not isinstance(commands, list) or not all(
            isinstance(item, str) for item in commands
        ):
            raise ValueError("commands must be a string list")
        if not self.commands or overwrite:
            self.commands = commands
        else:
            # add commands to existing build commands
            for command in commands:
                if command not in self.commands:
                    self.commands.append(command)
            # using list(set(x)) won't retain order,
            # solution inspired from https://stackoverflow.com/a/17016257/8116661
            self.commands = list(dict.fromkeys(self.commands))

    def with_requirements(
        self,
        requirements: Optional[List[str]] = None,
        requirements_file: str = "",
        overwrite: bool = False,
    ):
        """add package requirements from file or list to build spec.

        :param requirements:        a list of python packages
        :param requirements_file:   path to a python requirements file
        :param overwrite:           overwrite existing requirements,
                                    when False (default) will append to existing requirements
        :return: function object
        """
        requirements = requirements or []
        self._verify_list(requirements, "requirements")
        resolved_requirements = self._resolve_requirements(
            requirements, requirements_file
        )
        requirements = self.requirements or [] if not overwrite else []

        # make sure we don't append the same line twice
        for requirement in resolved_requirements:
            if requirement not in requirements:
                requirements.append(requirement)

        self.requirements = requirements

    @staticmethod
    def _resolve_requirements(requirements: list, requirements_file: str = "") -> list:
        requirements = requirements or []
        requirements_to_resolve = []

        # handle the requirements_file argument
        if requirements_file:
            with open(requirements_file, "r") as fp:
                requirements_to_resolve.extend(fp.read().splitlines())

        # handle the requirements argument
        requirements_to_resolve.extend(requirements)

        requirements = []
        for requirement in requirements_to_resolve:
            # clean redundant leading and trailing whitespaces
            requirement = requirement.strip()

            # ignore empty lines
            # ignore comments
            if not requirement or requirement.startswith("#"):
                continue

            # ignore inline comments as well
            inline_comment = requirement.split(" #")
            if len(inline_comment) > 1:
                requirement = inline_comment[0].strip()

            requirements.append(requirement)

        return requirements


class Notification(ModelObj):
    """Notification specification"""

    def __init__(
        self,
        kind=None,
        name=None,
        message=None,
        severity=None,
        when=None,
        condition=None,
        secret_params=None,
        params=None,
        status=None,
        sent_time=None,
        reason=None,
    ):
        self.kind = kind or mlrun.common.schemas.notification.NotificationKind.slack
        self.name = name or ""
        self.message = message or ""
        self.severity = (
            severity or mlrun.common.schemas.notification.NotificationSeverity.INFO
        )
        self.when = when or ["completed"]
        self.condition = condition or ""
        self.secret_params = secret_params or {}
        self.params = params or {}
        self.status = status
        self.sent_time = sent_time
        self.reason = reason

        self.validate_notification()

    def validate_notification(self):
        try:
            mlrun.common.schemas.notification.Notification(**self.to_dict())
        except pydantic.error_wrappers.ValidationError as exc:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "Invalid notification object"
            ) from exc

        # validate that size of notification secret_params doesn't exceed 1 MB,
        # due to k8s default secret size limitation.
        # a buffer of 100 KB is added to the size to account for the size of the secret metadata
        if (
            len(json.dumps(self.secret_params))
            > mlrun.common.schemas.notification.NotificationLimits.max_params_size.value
        ):
            raise mlrun.errors.MLRunInvalidArgumentError(
                "Notification params size exceeds max size of 1 MB"
            )

    @staticmethod
    def validate_notification_uniqueness(notifications: List["Notification"]):
        """Validate that all notifications in the list are unique by name"""
        names = [notification.name for notification in notifications]
        if len(names) != len(set(names)):
            raise mlrun.errors.MLRunInvalidArgumentError(
                "Notification names must be unique"
            )


class RunMetadata(ModelObj):
    """Run metadata"""

    def __init__(
        self,
        uid=None,
        name=None,
        project=None,
        labels=None,
        annotations=None,
        iteration=None,
    ):
        self.uid = uid
        self._iteration = iteration
        self.name = name
        self.project = project
        self.labels = labels or {}
        self.annotations = annotations or {}

    @property
    def iteration(self):
        return self._iteration or 0

    @iteration.setter
    def iteration(self, iteration):
        self._iteration = iteration


class HyperParamStrategies:
    grid = "grid"
    list = "list"
    random = "random"
    custom = "custom"

    @staticmethod
    def all():
        return [
            HyperParamStrategies.grid,
            HyperParamStrategies.list,
            HyperParamStrategies.random,
            HyperParamStrategies.custom,
        ]


class HyperParamOptions(ModelObj):
    """Hyper Parameter Options

    Parameters:
        param_file (str):                   hyper params input file path/url, instead of inline
        strategy (HyperParamStrategies):    hyper param strategy - grid, list or random
        selector (str):                     selection criteria for best result ([min|max.]<result>), e.g. max.accuracy
        stop_condition (str):               early stop condition e.g. "accuracy > 0.9"
        parallel_runs (int):                number of param combinations to run in parallel (over Dask)
        dask_cluster_uri (str):             db uri for a deployed dask cluster function, e.g. db://myproject/dask
        max_iterations (int):               max number of runs (in random strategy)
        max_errors (int):                   max number of child runs errors for the overall job to fail
        teardown_dask (bool):               kill the dask cluster pods after the runs
    """

    def __init__(
        self,
        param_file=None,
        strategy: typing.Optional[HyperParamStrategies] = None,
        selector=None,
        stop_condition=None,
        parallel_runs=None,
        dask_cluster_uri=None,
        max_iterations=None,
        max_errors=None,
        teardown_dask=None,
    ):
        self.param_file = param_file
        self.strategy = strategy
        self.selector = selector
        self.stop_condition = stop_condition
        self.max_iterations = max_iterations
        self.max_errors = max_errors
        self.parallel_runs = parallel_runs
        self.dask_cluster_uri = dask_cluster_uri
        self.teardown_dask = teardown_dask

    def validate(self):
        if self.strategy and self.strategy not in HyperParamStrategies.all():
            raise mlrun.errors.MLRunInvalidArgumentError(
                f"illegal hyper param strategy, use {','.join(HyperParamStrategies.all())}"
            )
        if self.max_iterations and self.strategy != HyperParamStrategies.random:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "max_iterations is only valid in random strategy"
            )


class RunSpec(ModelObj):
    """Run specification"""

    def __init__(
        self,
        parameters=None,
        hyperparams=None,
        param_file=None,
        selector=None,
        handler=None,
        inputs=None,
        outputs=None,
        input_path=None,
        output_path=None,
        function=None,
        secret_sources=None,
        data_stores=None,
        strategy=None,
        verbose=None,
        scrape_metrics=None,
        hyper_param_options=None,
        allow_empty_resources=None,
        inputs_type_hints=None,
        returns=None,
        notifications=None,
        state_thresholds=None,
    ):
        # A dictionary of parsing configurations that will be read from the inputs the user set. The keys are the inputs
        # keys (parameter names) and the values are the type hint given in the input keys after the colon.
        # Notice: We set it first as empty dictionary as setting the inputs will set it as well in case the type hints
        # were passed in the input keys.
        self._inputs_type_hints = {}

        self._hyper_param_options = None

        # Initialize the inputs and returns properties first and then use their setter methods:
        self._inputs = None
        self.inputs = inputs
        if inputs_type_hints:
            # Override the empty dictionary only if the user passed the parameter:
            self._inputs_type_hints = inputs_type_hints
        self._returns = None
        self.returns = returns

        self._outputs = outputs
        self.hyper_param_options = hyper_param_options
        self.parameters = parameters or {}
        self.hyperparams = hyperparams or {}
        self.param_file = param_file
        self.strategy = strategy
        self.selector = selector
        self.handler = handler
        self.input_path = input_path
        self.output_path = output_path
        self.function = function
        self._secret_sources = secret_sources or []
        self._data_stores = data_stores
        self.verbose = verbose
        self.scrape_metrics = scrape_metrics
        self.allow_empty_resources = allow_empty_resources
        self._notifications = notifications or []
        self.state_thresholds = state_thresholds or {}

    def to_dict(self, fields=None, exclude=None):
        struct = super().to_dict(fields, exclude=["handler"])
        if self.handler and isinstance(self.handler, str):
            struct["handler"] = self.handler
        return struct

    def is_hyper_job(self):
        param_file = self.param_file or self.hyper_param_options.param_file
        return param_file or self.hyperparams

    @property
    def inputs(self) -> Dict[str, str]:
        """
        Get the inputs dictionary. A dictionary of parameter names as keys and paths as values.

        :return: The inputs dictionary.
        """
        return self._inputs

    @inputs.setter
    def inputs(self, inputs: Dict[str, str]):
        """
        Set the given inputs in the spec. Inputs can include a type hint string in their keys following a colon, meaning
        following this structure: "<input key : type hint>".

        :exmaple:

        >>> run_spec.inputs = {
        ...     "my_input": "...",
        ...     "my_hinted_input : pandas.DataFrame": "..."
        ... }

        :param inputs: The inputs to set.
        """
        # Check if None, then set and return:
        if inputs is None:
            self._inputs = None
            return

        # Verify it's a dictionary:
        self._inputs = self._verify_dict(inputs, "inputs")

    @property
    def inputs_type_hints(self) -> Dict[str, str]:
        """
        Get the input type hints. A dictionary of parameter names as keys and their type hints as values.

        :return: The input type hints dictionary.
        """
        return self._inputs_type_hints

    @inputs_type_hints.setter
    def inputs_type_hints(self, inputs_type_hints: Dict[str, str]):
        """
        Set the inputs type hints to parse during a run.

        :param inputs_type_hints: The type hints to set.
        """
        # Verify the given value is a dictionary or None:
        self._inputs_type_hints = self._verify_dict(
            inputs_type_hints, "inputs_type_hints"
        )

    @property
    def returns(self):
        """
        Get the returns list. A list of log hints for returning values.

        :return: The returns list.
        """
        return self._returns

    @returns.setter
    def returns(self, returns: List[Union[str, Dict[str, str]]]):
        """
        Set the returns list to log the returning values at the end of a run.

        :param returns: The return list to set.

        :raise MLRunInvalidArgumentError: In case one of the values in the list is invalid.
        """
        # This import is located in the method due to circular imports error.
        from mlrun.package.utils import LogHintUtils

        if returns is None:
            self._returns = None
            return
        self._verify_list(returns, "returns")

        # Validate:
        for log_hint in returns:
            LogHintUtils.parse_log_hint(log_hint=log_hint)

        # Store the results:
        self._returns = returns

    @property
    def hyper_param_options(self) -> HyperParamOptions:
        return self._hyper_param_options

    @hyper_param_options.setter
    def hyper_param_options(self, hyper_param_options):
        self._hyper_param_options = self._verify_dict(
            hyper_param_options, "hyper_param_options", HyperParamOptions
        )

    @property
    def outputs(self) -> List[str]:
        """
        Get the expected outputs. The list is constructed from keys of both the `outputs` and `returns` properties.

        :return: The expected outputs list.
        """
        return self.join_outputs_and_returns(
            outputs=self._outputs, returns=self.returns
        )

    @outputs.setter
    def outputs(self, outputs):
        """
        Set the expected outputs list.

        :param outputs: A list of expected output keys.
        """
        self._verify_list(outputs, "outputs")
        self._outputs = outputs

    @property
    def secret_sources(self):
        return self._secret_sources

    @secret_sources.setter
    def secret_sources(self, secret_sources):
        self._verify_list(secret_sources, "secret_sources")
        self._secret_sources = secret_sources

    @property
    def data_stores(self):
        return self._data_stores

    @data_stores.setter
    def data_stores(self, data_stores):
        self._verify_list(data_stores, "data_stores")
        self._data_stores = data_stores

    @property
    def handler_name(self):
        if self.handler:
            if inspect.isfunction(self.handler):
                return self.handler.__name__
            else:
                return str(self.handler)
        return ""

    @property
    def notifications(self):
        return self._notifications

    @notifications.setter
    def notifications(self, notifications):
        if isinstance(notifications, list):
            self._notifications = ObjectList.from_list(Notification, notifications)
        elif isinstance(notifications, ObjectList):
            self._notifications = notifications
        else:
            raise ValueError("Notifications must be a list")

    @property
    def state_thresholds(self):
        return self._state_thresholds

    @state_thresholds.setter
    def state_thresholds(self, state_thresholds: Dict[str, str]):
        """
        Set the dictionary of k8s states (pod phase) to thresholds time strings.
        The state will be matched against the pod's status. The threshold should be a time string that conforms
        to timelength python package standards and is at least 1 minute (-1 for infinite). If the phase is active
        for longer than the threshold, the run will be marked as aborted and the pod will be deleted.
        See mlconf.function.spec.state_thresholds for the state options and default values.

        example:
            {"image_pull_backoff": "1h", "running": "1d 2 hours"}

        :param state_thresholds: The state-thresholds dictionary.
        """
        self._verify_dict(state_thresholds, "state_thresholds")
        self._state_thresholds = state_thresholds

    def extract_type_hints_from_inputs(self):
        """
        This method extracts the type hints from the input keys in the input dictionary.

        As a result, after the method ran the inputs dictionary - a dictionary of parameter names as keys and paths as
        values, will be cleared from type hints and the extracted type hints will be saved in the spec's inputs type
        hints dictionary - a dictionary of parameter names as keys and their type hints as values. If a parameter is
        not in the type hints dictionary, its type hint will be `mlrun.DataItem` by default.
        """
        # Validate there are inputs to read:
        if self.inputs is None:
            return

        # Prepare dictionaries to hold the cleared inputs and type hints:
        cleared_inputs = {}
        extracted_inputs_type_hints = {}

        # Clear the inputs from parsing configurations:
        for input_key, input_value in self.inputs.items():
            # Look for type hinted in input key:
            if ":" in input_key:
                # Separate the user input by colon:
                input_key, input_type = RunSpec._separate_type_hint_from_input_key(
                    input_key=input_key
                )
                # Collect the type hint:
                extracted_inputs_type_hints[input_key] = input_type
            # Collect the cleared input key:
            cleared_inputs[input_key] = input_value

        # Set the now configuration free inputs and extracted type hints:
        self.inputs = cleared_inputs
        self.inputs_type_hints = extracted_inputs_type_hints

    @staticmethod
    def join_outputs_and_returns(
        outputs: List[str], returns: List[Union[str, Dict[str, str]]]
    ) -> List[str]:
        """
        Get the outputs set in the spec. The outputs are constructed from both the 'outputs' and 'returns' properties
        that were set by the user.

        :param outputs: A spec outputs property - list of output keys.
        :param returns: A spec returns property - list of key and configuration of how to log returning values.

        :return: The joined 'outputs' and 'returns' list.
        """
        # Collect the 'returns' property keys:
        cleared_returns = []
        if returns:
            for return_value in returns:
                # Check if the return entry is a configuration dictionary or a key-type structure string (otherwise its
                # just a key string):
                if isinstance(return_value, dict):
                    # Set it to the artifact key:
                    return_value = return_value["key"]
                elif ":" in return_value:
                    # Take only the key name (returns values pattern is validated when set in the spec):
                    return_value = return_value.replace(" ", "").split(":")[0]
                # Collect it:
                cleared_returns.append(return_value)

        # Use `set` join to combine the two lists without duplicates:
        outputs = list(set(outputs if outputs else []) | set(cleared_returns))

        return outputs

    @staticmethod
    def _separate_type_hint_from_input_key(input_key: str) -> Tuple[str, str]:
        """
        An input key in the `inputs` dictionary parameter of a task (or `Runtime.run` method) or the docs setting of a
        `Runtime` handler can be provided with a colon to specify its type hint in the following structure:
        "<parameter_key> : <type_hint>".

        This method parses the provided value by the user.

        :param input_key: A string entry in the inputs dictionary keys.

        :return: The value as key and type hint tuple.

        :raise MLRunInvalidArgumentError: If an incorrect pattern was provided.
        """
        # Validate correct pattern:
        if input_key.count(":") > 1:
            raise mlrun.errors.MLRunInvalidArgumentError(
                f"Incorrect input pattern. Input keys can have only a single ':' in them to specify the desired type "
                f"the input will be parsed as. Given: {input_key}."
            )

        # Split into key and type:
        value_key, value_type = input_key.replace(" ", "").split(":")

        return value_key, value_type


class RunStatus(ModelObj):
    """Run status"""

    def __init__(
        self,
        state=None,
        error=None,
        host=None,
        commit=None,
        status_text=None,
        results=None,
        artifacts=None,
        start_time=None,
        last_update=None,
        iterations=None,
        ui_url=None,
        reason: str = None,
        notifications: Dict[str, Notification] = None,
    ):
        self.state = state or "created"
        self.status_text = status_text
        self.error = error
        self.host = host
        self.commit = commit
        self.results = results
        self.artifacts = artifacts
        self.start_time = start_time
        self.last_update = last_update
        self.iterations = iterations
        self.ui_url = ui_url
        self.reason = reason
        self.notifications = notifications or {}


class RunTemplate(ModelObj):
    """Run template"""

    def __init__(self, spec: RunSpec = None, metadata: RunMetadata = None):
        self._spec = None
        self._metadata = None
        self.spec = spec
        self.metadata = metadata

    @property
    def spec(self) -> RunSpec:
        return self._spec

    @spec.setter
    def spec(self, spec):
        self._spec = self._verify_dict(spec, "spec", RunSpec)

    @property
    def metadata(self) -> RunMetadata:
        return self._metadata

    @metadata.setter
    def metadata(self, metadata):
        self._metadata = self._verify_dict(metadata, "metadata", RunMetadata)

    def with_params(self, **kwargs):
        """set task parameters using key=value, key2=value2, .."""
        self.spec.parameters = kwargs
        return self

    def with_input(self, key, path):
        """set task data input, path is an Mlrun global DataItem uri

        examples::

            task.with_input("data", "/file-dir/path/to/file")
            task.with_input("data", "s3://<bucket>/path/to/file")
            task.with_input("data", "v3io://[<remote-host>]/<data-container>/path/to/file")
        """
        if not self.spec.inputs:
            self.spec.inputs = {}
        self.spec.inputs[key] = path
        return self

    def with_hyper_params(
        self,
        hyperparams,
        selector=None,
        strategy: HyperParamStrategies = None,
        **options,
    ):
        """set hyper param values and configurations,
        see parameters in: :py:class:`HyperParamOptions`

        example::

            grid_params = {"p1": [2,4,1], "p2": [10,20]}
            task = mlrun.new_task("grid-search")
            task.with_hyper_params(grid_params, selector="max.accuracy")
        """
        self.spec.hyperparams = hyperparams
        self.spec.hyper_param_options = options
        self.spec.hyper_param_options.selector = selector
        self.spec.hyper_param_options.strategy = strategy
        self.spec.hyper_param_options.validate()
        return self

    def with_param_file(
        self,
        param_file,
        selector=None,
        strategy: HyperParamStrategies = None,
        **options,
    ):
        """set hyper param values (from a file url) and configurations,
        see parameters in: :py:class:`HyperParamOptions`

        example::

            grid_params = "s3://<my-bucket>/path/to/params.json"
            task = mlrun.new_task("grid-search")
            task.with_param_file(grid_params, selector="max.accuracy")
        """
        self.spec.hyper_param_options = options
        self.spec.hyper_param_options.param_file = param_file
        self.spec.hyper_param_options.selector = selector
        self.spec.hyper_param_options.strategy = strategy
        self.spec.hyper_param_options.validate()
        return self

    def with_secrets(self, kind, source):
        """register a secrets source (file, env or dict)

        read secrets from a source provider to be used in workflows, example::

            task.with_secrets('file', 'file.txt')
            task.with_secrets('inline', {'key': 'val'})
            task.with_secrets('env', 'ENV1,ENV2')

            task.with_secrets('vault', ['secret1', 'secret2'...])

            # If using with k8s secrets, the k8s secret is managed by MLRun, through the project-secrets
            # mechanism. The secrets will be attached to the running pod as environment variables.
            task.with_secrets('kubernetes', ['secret1', 'secret2'])

            # If using an empty secrets list [] then all accessible secrets will be available.
            task.with_secrets('vault', [])

            # To use with Azure key vault, a k8s secret must be created with the following keys:
            # kubectl -n <namespace> create secret generic azure-key-vault-secret \\
            #     --from-literal=tenant_id=<service principal tenant ID> \\
            #     --from-literal=client_id=<service principal client ID> \\
            #     --from-literal=secret=<service principal secret key>

            task.with_secrets('azure_vault', {
                'name': 'my-vault-name',
                'k8s_secret': 'azure-key-vault-secret',
                # An empty secrets list may be passed ('secrets': []) to access all vault secrets.
                'secrets': ['secret1', 'secret2'...]
            })

        :param kind:   secret type (file, inline, env)
        :param source: secret data or link (see example)

        :returns: The RunTemplate object
        """

        if kind == "vault" and isinstance(source, list):
            source = {"project": self.metadata.project, "secrets": source}

        self.spec.secret_sources.append({"kind": kind, "source": source})
        return self

    def set_label(self, key, value):
        """set a key/value label for the task"""
        self.metadata.labels[key] = str(value)
        return self

    def to_env(self):
        environ["MLRUN_EXEC_CONFIG"] = self.to_json()


class RunObject(RunTemplate):
    """A run"""

    def __init__(
        self,
        spec: RunSpec = None,
        metadata: RunMetadata = None,
        status: RunStatus = None,
    ):
        super().__init__(spec, metadata)
        self._status = None
        self.status = status
        self.outputs_wait_for_completion = True

    @classmethod
    def from_template(cls, template: RunTemplate):
        return cls(template.spec, template.metadata)

    def to_json(self, exclude=None, **kwargs):
        # Since the `params` attribute within each notification object can be large,
        # it has the potential to cause errors and is unnecessary for the notification functionality.
        # Therefore, in this section, we remove the `params` attribute from each notification object.
        if (
            exclude_notifications_params := kwargs.get("exclude_notifications_params")
        ) and exclude_notifications_params:
            if self.spec.notifications:
                # Extract and remove 'params' from each notification
                extracted_params = []
                for notification in self.spec.notifications:
                    extracted_params.append(notification.params)
                    del notification.params
                # Generate the JSON representation, excluding specified fields
                json_obj = super().to_json(exclude=exclude)
                # Restore 'params' back to the notifications
                for notification, params in zip(
                    self.spec.notifications, extracted_params
                ):
                    notification.params = params
                return json_obj
        return super().to_json(exclude=exclude)

    @property
    def status(self) -> RunStatus:
        return self._status

    @status.setter
    def status(self, status):
        self._status = self._verify_dict(status, "status", RunStatus)

    @property
    def error(self) -> str:
        """error string if failed"""
        if self.status:
            if self.status.state != "error":
                return f"Run state ({self.status.state}) is not in error state"
            return (
                self.status.error
                or self.status.reason
                or self.status.status_text
                or "Unknown error"
            )
        return ""

    def output(self, key):
        """return the value of a specific result or artifact by key"""
        self._outputs_wait_for_completion()
        if self.status.results and key in self.status.results:
            return self.status.results.get(key)
        artifact = self._artifact(key)
        if artifact:
            return get_artifact_target(artifact, self.metadata.project)
        return None

    @property
    def ui_url(self) -> str:
        """UI URL (for relevant runtimes)"""
        self.refresh()
        if not self._status.ui_url:
            print("UI currently not available (status={})".format(self._status.state))
        return self._status.ui_url

    @property
    def outputs(self):
        """return a dict of outputs, result values and artifact uris"""
        outputs = {}
        self._outputs_wait_for_completion()
        if self.status.results:
            outputs = {k: v for k, v in self.status.results.items()}
        if self.status.artifacts:
            for a in self.status.artifacts:
                key = a["key"] if is_legacy_artifact(a) else a["metadata"]["key"]
                outputs[key] = get_artifact_target(a, self.metadata.project)
        return outputs

    def artifact(self, key) -> "mlrun.DataItem":
        """return artifact DataItem by key"""
        self._outputs_wait_for_completion()
        artifact = self._artifact(key)
        if artifact:
            uri = get_artifact_target(artifact, self.metadata.project)
            if uri:
                return mlrun.get_dataitem(uri)
        return None

    def _outputs_wait_for_completion(
        self,
        show_logs=False,
    ):
        """
        Wait for the run to complete fetching the run outputs.
        When running a function with watch=False, and passing the outputs to another function,
        the outputs will not be available until the run is completed.
        :param show_logs: default False, avoid spamming unwanted logs of the run when the user asks for outputs
        """
        if self.outputs_wait_for_completion:
            self.wait_for_completion(
                show_logs=show_logs,
            )

    def _artifact(self, key):
        """return artifact DataItem by key"""
        if self.status.artifacts:
            for a in self.status.artifacts:
                if a["metadata"]["key"] == key:
                    return a
        return None

    def uid(self):
        """run unique id"""
        return self.metadata.uid

    def state(self):
        """current run state"""
        if self.status.state in mlrun.runtimes.constants.RunStates.terminal_states():
            return self.status.state
        self.refresh()
        return self.status.state or "unknown"

    def refresh(self):
        """refresh run state from the db"""
        db = mlrun.get_run_db()
        run = db.read_run(
            uid=self.metadata.uid,
            project=self.metadata.project,
            iter=self.metadata.iteration,
        )
        if run:
            self.status = RunStatus.from_dict(run.get("status", {}))
            self.status.from_dict(run.get("status", {}))
            return self

    def show(self):
        """show the current status widget, in jupyter notebook"""
        db = mlrun.get_run_db()
        db.list_runs(uid=self.metadata.uid, project=self.metadata.project).show()

    def logs(self, watch=True, db=None, offset=0):
        """return or watch on the run logs"""
        if not db:
            db = mlrun.get_run_db()

        if not db:
            logger.warning("DB is not configured, cannot show logs")
            return None

        state, new_offset = db.watch_log(
            self.metadata.uid, self.metadata.project, watch=watch, offset=offset
        )
        if state:
            logger.debug("Run reached terminal state", state=state)

        return state, new_offset

    def wait_for_completion(
        self,
        sleep=3,
        timeout=0,
        raise_on_failure=True,
        show_logs=None,
        logs_interval=None,
    ):
        """
        Wait for remote run to complete.
        Default behavior is to wait until reached terminal state or timeout passed, if timeout is 0 then wait forever
        It pulls the run status from the db every sleep seconds.
        If show_logs is not False and logs_interval is not None, it will print the logs when run reached terminal state
        If show_logs is not False and logs_interval is defined, it will print the logs every logs_interval seconds
        if show_logs is False it will not print the logs, will still pull the run state until it reaches terminal state
        """
        # TODO: rename sleep to pull_state_interval
        total_time = 0
        offset = 0
        last_pull_log_time = None
        logs_enabled = show_logs is not False
        state = self.state()
        if state not in mlrun.runtimes.constants.RunStates.terminal_states():
            logger.info(
                f"run {self.metadata.name} is not completed yet, waiting for it to complete",
                current_state=state,
            )
        while True:
            state = self.state()
            if (
                logs_enabled
                and logs_interval
                and state not in mlrun.runtimes.constants.RunStates.terminal_states()
                and (
                    last_pull_log_time is None
                    or (datetime.now() - last_pull_log_time).seconds > logs_interval
                )
            ):
                last_pull_log_time = datetime.now()
                state, offset = self.logs(watch=False, offset=offset)

            if state in mlrun.runtimes.constants.RunStates.terminal_states():
                if logs_enabled and logs_interval:
                    self.logs(watch=False, offset=offset)
                break
            time.sleep(sleep)
            total_time += sleep
            if timeout and total_time > timeout:
                raise mlrun.errors.MLRunTimeoutError(
                    "Run did not reach terminal state on time"
                )
        if logs_enabled and not logs_interval:
            self.logs(watch=False)
        if raise_on_failure and state != mlrun.runtimes.constants.RunStates.completed:
            raise mlrun.errors.MLRunRuntimeError(
                f"Task {self.metadata.name} did not complete (state={state})"
            )

        return state

    @staticmethod
    def create_uri(project: str, uid: str, iteration: Union[int, str], tag: str = ""):
        if tag:
            tag = f":{tag}"
        iteration = str(iteration)
        return f"{project}@{uid}#{iteration}{tag}"

    @staticmethod
    def parse_uri(uri: str) -> Tuple[str, str, str, str]:
        uri_pattern = (
            r"^(?P<project>.*)@(?P<uid>.*)\#(?P<iteration>.*?)(:(?P<tag>.*))?$"
        )
        match = re.match(uri_pattern, uri)
        if not match:
            raise ValueError(
                "Uri not in supported format <project>@<uid>#<iteration>[:tag]"
            )
        group_dict = match.groupdict()
        return (
            group_dict["project"],
            group_dict["uid"],
            group_dict["iteration"],
            group_dict["tag"],
        )


class EntrypointParam(ModelObj):
    def __init__(
        self,
        name="",
        type=None,
        default=None,
        doc="",
        required=None,
        choices: list = None,
    ):
        self.name = name
        self.type = type
        self.default = default
        self.doc = doc
        self.required = required
        self.choices = choices


class FunctionEntrypoint(ModelObj):
    def __init__(
        self,
        name="",
        doc="",
        parameters=None,
        outputs=None,
        lineno=-1,
        has_varargs=None,
        has_kwargs=None,
    ):
        self.name = name
        self.doc = doc
        self.parameters = [] if parameters is None else parameters
        self.outputs = [] if outputs is None else outputs
        self.lineno = lineno
        self.has_varargs = has_varargs
        self.has_kwargs = has_kwargs


def new_task(
    name=None,
    project=None,
    handler=None,
    params=None,
    hyper_params=None,
    param_file=None,
    selector=None,
    hyper_param_options=None,
    inputs=None,
    outputs=None,
    in_path=None,
    out_path=None,
    artifact_path=None,
    secrets=None,
    base=None,
    returns=None,
) -> RunTemplate:
    """Creates a new task

    :param name:            task name
    :param project:         task project
    :param handler:         code entry-point/handler name
    :param params:          input parameters (dict)
    :param hyper_params:    dictionary of hyper parameters and list values, each
                            hyper param holds a list of values, the run will be
                            executed for every parameter combination (GridSearch)
    :param param_file:      a csv file with parameter combinations, first row hold
                            the parameter names, following rows hold param values
    :param selector:        selection criteria for hyper params e.g. "max.accuracy"
    :param hyper_param_options:   hyper parameter options, see: :py:class:`HyperParamOptions`
    :param inputs:          dictionary of input objects + optional paths (if path is
                            omitted the path will be the in_path/key)
    :param outputs:         dictionary of input objects + optional paths (if path is
                            omitted the path will be the out_path/key)
    :param in_path:         default input path/url (prefix) for inputs
    :param out_path:        default output path/url (prefix) for artifacts
    :param artifact_path:   default artifact output path
    :param secrets:         extra secrets specs, will be injected into the runtime
                            e.g. ['file=<filename>', 'env=ENV_KEY1,ENV_KEY2']
    :param base:            task instance to use as a base instead of a fresh new task instance
    :param returns:         List of log hints - configurations for how to log the returning values from the handler's
                            run (as artifacts or results). The list's length must be equal to the amount of returning
                            objects. A log hint may be given as:

                            * A string of the key to use to log the returning value as result or as an artifact. To
                              specify The artifact type, it is possible to pass a string in the following structure:
                              "<key> : <type>". Available artifact types can be seen in `mlrun.ArtifactType`. If no
                              artifact type is specified, the object's default artifact type will be used.
                            * A dictionary of configurations to use when logging. Further info per object type and
                              artifact type can be given there. The artifact key must appear in the dictionary as
                              "key": "the_key".
    """

    if base:
        run = deepcopy(base)
    else:
        run = RunTemplate()
    run.metadata.name = name or run.metadata.name
    run.metadata.project = project or run.metadata.project
    run.spec.handler = handler or run.spec.handler
    run.spec.parameters = params or run.spec.parameters
    run.spec.inputs = inputs or run.spec.inputs
    run.spec.returns = returns or run.spec.returns
    run.spec.outputs = outputs or run.spec.outputs or []
    run.spec.input_path = in_path or run.spec.input_path
    run.spec.output_path = artifact_path or out_path or run.spec.output_path
    run.spec.secret_sources = secrets or run.spec.secret_sources or []

    run.spec.hyperparams = hyper_params or run.spec.hyperparams
    run.spec.hyper_param_options = hyper_param_options or run.spec.hyper_param_options
    run.spec.hyper_param_options.param_file = (
        param_file or run.spec.hyper_param_options.param_file
    )
    run.spec.hyper_param_options.selector = (
        selector or run.spec.hyper_param_options.selector
    )
    return run


class TargetPathObject:
    """Class configuring the target path
    This class will take consideration of a few parameters to create the correct end result path:

    - :run_id: if run_id is provided target will be considered as run_id mode which require to
        contain a {run_id} place holder in the path.
    - :is_single_file: if true then run_id must be the directory containing the output file
        or generated before the file name (run_id/output.file).
    - :base_path: if contains the place holder for run_id, run_id must not be None.
        if run_id passed and place holder doesn't exist the place holder will
        be generated in the correct place.
    """

    def __init__(
        self,
        base_path=None,
        run_id=None,
        is_single_file=False,
    ):
        self.run_id = run_id
        self.full_path_template = base_path
        if run_id is not None:
            if RUN_ID_PLACE_HOLDER not in self.full_path_template:
                if not is_single_file:
                    if self.full_path_template[-1] != "/":
                        self.full_path_template = self.full_path_template + "/"
                    self.full_path_template = (
                        self.full_path_template + RUN_ID_PLACE_HOLDER + "/"
                    )
                else:
                    dir_name_end = len(self.full_path_template)
                    if self.full_path_template[-1] != "/":
                        dir_name_end = self.full_path_template.rfind("/") + 1
                    updated_path = (
                        self.full_path_template[:dir_name_end]
                        + RUN_ID_PLACE_HOLDER
                        + "/"
                        + self.full_path_template[dir_name_end:]
                    )
                    self.full_path_template = updated_path
            else:
                if self.full_path_template[-1] != "/":
                    if self.full_path_template.endswith(RUN_ID_PLACE_HOLDER):
                        self.full_path_template = self.full_path_template + "/"
        else:
            if RUN_ID_PLACE_HOLDER in self.full_path_template:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    "Error when trying to create TargetPathObject with place holder '{run_id}' but no value."
                )

    def get_templated_path(self):
        return self.full_path_template

    def get_absolute_path(self, project_name=None):
        path = fill_project_path_template(
            artifact_path=self.full_path_template,
            project=project_name,
        )
        return path.format(run_id=self.run_id) if self.run_id else path


class DataSource(ModelObj):
    """online or offline data source spec"""

    _dict_fields = [
        "kind",
        "name",
        "path",
        "attributes",
        "key_field",
        "time_field",
        "schedule",
        "online",
        "workers",
        "max_age",
        "start_time",
        "end_time",
        "credentials_prefix",
    ]
    kind = None

    def __init__(
        self,
        name: str = None,
        path: str = None,
        attributes: Dict[str, object] = None,
        key_field: str = None,
        time_field: str = None,
        schedule: str = None,
        start_time: Optional[Union[datetime, str]] = None,
        end_time: Optional[Union[datetime, str]] = None,
    ):
        self.name = name
        self.path = str(path) if path is not None else None
        self.attributes = attributes or {}
        self.schedule = schedule
        self.key_field = key_field
        self.time_field = time_field
        self.start_time = start_time
        self.end_time = end_time

        self.online = None
        self.max_age = None
        self.workers = None
        self._secrets = {}

    def set_secrets(self, secrets):
        self._secrets = secrets


class DataTargetBase(ModelObj):
    """data target spec, specify a destination for the feature set data"""

    _dict_fields = [
        "name",
        "kind",
        "path",
        "after_step",
        "attributes",
        "partitioned",
        "key_bucketing_number",
        "partition_cols",
        "time_partitioning_granularity",
        "max_events",
        "flush_after_seconds",
        "storage_options",
        "run_id",
        "schema",
        "credentials_prefix",
    ]

    @classmethod
    def from_dict(cls, struct=None, fields=None):
        return super().from_dict(struct, fields=fields)

    def get_path(self):
        if self.path:
            is_single_file = hasattr(self, "is_single_file") and self.is_single_file()
            return TargetPathObject(self.path, self.run_id, is_single_file)
        else:
            return None

    def __init__(
        self,
        kind: str = None,
        name: str = "",
        path=None,
        attributes: Dict[str, str] = None,
        after_step=None,
        partitioned: bool = False,
        key_bucketing_number: Optional[int] = None,
        partition_cols: Optional[List[str]] = None,
        time_partitioning_granularity: Optional[str] = None,
        max_events: Optional[int] = None,
        flush_after_seconds: Optional[int] = None,
        storage_options: Dict[str, str] = None,
        schema: Dict[str, Any] = None,
        credentials_prefix=None,
    ):
        self.name = name
        self.kind: str = kind
        self.path = path
        self.after_step = after_step
        self.attributes = attributes or {}
        self.last_written = None
        self.partitioned = partitioned
        self.key_bucketing_number = key_bucketing_number
        self.partition_cols = partition_cols
        self.time_partitioning_granularity = time_partitioning_granularity
        self.max_events = max_events
        self.flush_after_seconds = flush_after_seconds
        self.storage_options = storage_options
        self.run_id = None
        self.schema = schema
        self.credentials_prefix = credentials_prefix


class FeatureSetProducer(ModelObj):
    """information about the task/job which produced the feature set data"""

    def __init__(self, kind=None, name=None, uri=None, owner=None, sources=None):
        self.kind = kind
        self.name = name
        self.owner = owner
        self.uri = uri
        self.sources = sources or {}


class DataTarget(DataTargetBase):
    """data target with extra status information (used in the feature-set/vector status)"""

    _dict_fields = [
        "name",
        "kind",
        "path",
        "start_time",
        "online",
        "status",
        "updated",
        "size",
        "last_written",
        "run_id",
        "partitioned",
        "key_bucketing_number",
        "partition_cols",
        "time_partitioning_granularity",
        "credentials_prefix",
    ]

    def __init__(
        self,
        kind: str = None,
        name: str = "",
        path=None,
        online=None,
    ):
        super().__init__(kind, name, path)
        self.status = ""
        self.updated = None
        self.size = None
        self.online = online
        self.max_age = None
        self.start_time = None
        self.last_written = None
        self._producer = None
        self.producer = {}

    @property
    def producer(self) -> FeatureSetProducer:
        return self._producer

    @producer.setter
    def producer(self, producer):
        self._producer = self._verify_dict(producer, "producer", FeatureSetProducer)


class VersionedObjMetadata(ModelObj):
    def __init__(
        self,
        name: str = None,
        tag: str = None,
        uid: str = None,
        project: str = None,
        labels: Dict[str, str] = None,
        annotations: Dict[str, str] = None,
        updated=None,
    ):
        self.name = name
        self.tag = tag
        self.uid = uid
        self.project = project
        self.labels = labels or {}
        self.annotations = annotations or {}
        self.updated = updated
