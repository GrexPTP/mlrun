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
#
import collections
import copy
import json
import re
import traceback
import typing
from hashlib import sha1, sha224
from http import HTTPStatus
from os import environ
from pathlib import Path

import kubernetes.client
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

import mlrun.common.schemas
import mlrun.errors
import mlrun.runtimes.pod
import mlrun.utils.helpers
import mlrun.utils.notifications.notification_pusher
import server.api.constants
import server.api.crud
import server.api.db.base
import server.api.utils.auth.verifier
import server.api.utils.clients.iguazio
import server.api.utils.singletons.k8s
from mlrun.common.helpers import parse_versioned_object_uri
from mlrun.config import config
from mlrun.errors import err_to_str
from mlrun.run import import_function, new_function
from mlrun.runtimes.utils import enrich_function_from_dict
from mlrun.utils import get_in, logger
from server.api.db.sqldb.db import SQLDB
from server.api.rundb.sqldb import SQLRunDB
from server.api.utils.singletons.db import get_db
from server.api.utils.singletons.logs_dir import get_logs_dir
from server.api.utils.singletons.scheduler import get_scheduler


def log_and_raise(status=HTTPStatus.BAD_REQUEST.value, **kw):
    logger.error(str(kw))
    raise HTTPException(status_code=status, detail=kw)


def log_path(project, uid) -> Path:
    return project_logs_path(project) / uid


def project_logs_path(project) -> Path:
    return get_logs_dir() / project


def get_obj_path(schema, path, user=""):
    """
    Perform standardization and validation on paths, which may be provided with an inline schema or not, and may point
    at FUSE mounted paths, which should be adjusted - these paths are replaced with schema-based paths that can be
    accessed by MLRun's data-store mechanism and are not dependent on a specific mount configuration. Also, it validates
    that the path is allowed access through APIs.

    This method does the following:
    - Merges `schema` provided as parameter with the schema given as part of path (if given)
    - Changes User FUSE paths (beginning with `/User`) to v3io paths pointing at the user in the `users` container
    - Changes v3io FUSE paths (beginning with `/v3io`) to v3io schema paths
    - Replace paths in the `data_volume` configured in the MLRun config (if specified) to begin with `real_path`
    - Validate that the path is allowed - allowed paths are those beginning with `v3io://`, `real_path` if specified,
      and any path specified in the `httpdb.allowed_file_paths` config param
    On success, the path returned will always be in the format `<schema>://<path>`
    """
    real_path = config.httpdb.real_path
    if path.startswith("/User/"):
        user = user or environ.get("V3IO_USERNAME", "admin")
        path = "v3io:///users/" + user + path[5:]
        schema = schema or "v3io"
    elif path.startswith("/v3io"):
        path = "v3io://" + path[len("/v3io") :]
        schema = schema or "v3io"
    elif config.httpdb.data_volume and path.startswith(config.httpdb.data_volume):
        data_volume_prefix = config.httpdb.data_volume
        if data_volume_prefix.endswith("/"):
            data_volume_prefix = data_volume_prefix[:-1]
        if real_path:
            path_from_volume = path[len(data_volume_prefix) :]
            if path_from_volume.startswith("/"):
                path_from_volume = path_from_volume[1:]
            path = str(Path(real_path) / Path(path_from_volume))
    if schema:
        schema_prefix = schema + "://"
        if not path.startswith(schema_prefix):
            path = f"{schema_prefix}{path}"

    allowed_paths_list = get_allowed_path_prefixes_list()
    if not any(path.startswith(allowed_path) for allowed_path in allowed_paths_list):
        raise mlrun.errors.MLRunAccessDeniedError("Unauthorized path")
    return path


def get_allowed_path_prefixes_list() -> typing.List[str]:
    """
    Get list of allowed paths - v3io:// is always allowed, and also the real_path parameter if specified.
    We never allow local files in the allowed paths list. Allowed paths must contain a schema (://).
    """
    real_path = config.httpdb.real_path
    allowed_file_paths = config.httpdb.allowed_file_paths or ""
    allowed_paths_list = [
        path.strip() for path in allowed_file_paths.split(",") if "://" in path
    ]
    if real_path:
        allowed_paths_list.append(real_path)
    allowed_paths_list.append("v3io://")
    return allowed_paths_list


def get_secrets(
    auth_info: mlrun.common.schemas.AuthInfo,
):
    return {
        "V3IO_ACCESS_KEY": auth_info.data_session,
    }


def get_run_db_instance(
    db_session: Session,
):
    # TODO: getting the run db should be done seamlessly by the run db factory and not require this logic to
    #  inject the session
    db = get_db()
    if isinstance(db, SQLDB):
        run_db = SQLRunDB(db.dsn, db_session)
    else:
        run_db = db.db
    run_db.connect()
    return run_db


def parse_submit_run_body(data):
    task = data.get("task")
    function_dict = data.get("function")
    function_url = data.get("functionUrl")
    if not function_url and task:
        function_url = get_in(task, "spec.function")
    if not (function_dict or function_url) or not task:
        log_and_raise(
            HTTPStatus.BAD_REQUEST.value,
            reason="bad JSON, need to include function/url and task objects",
        )
    return function_dict, function_url, task


def _generate_function_and_task_from_submit_run_body(db_session: Session, data):
    function_dict, function_url, task = parse_submit_run_body(data)

    if function_dict and not function_url:
        function = new_function(runtime=function_dict)
    else:
        if "://" in function_url:
            function = import_function(
                url=function_url, project=task.get("metadata", {}).get("project")
            )
        else:
            project, name, tag, hash_key = parse_versioned_object_uri(function_url)
            function_record = get_db().get_function(
                db_session, name, project, tag, hash_key
            )
            if not function_record:
                log_and_raise(
                    HTTPStatus.NOT_FOUND.value,
                    reason=f"runtime error: function {function_url} not found",
                )
            function = new_function(runtime=function_record)

        if function_dict:
            # The purpose of the function dict is to enable the user to override configurations of the existing function
            # without modifying it - to do that we're creating a function object from the request function dict and
            # assign values from it to the main function object
            function = enrich_function_from_dict(function, function_dict)

    apply_enrichment_and_validation_on_task(task)

    return function, task


async def submit_run(
    db_session: Session, auth_info: mlrun.common.schemas.AuthInfo, data
):
    _, _, _, response = await run_in_threadpool(
        submit_run_sync, db_session, auth_info, data
    )
    return response


def apply_enrichment_and_validation_on_task(task):
    # Conceal notification config params from the task object with secrets
    mask_notification_params_on_task(task, server.api.constants.MaskOperations.CONCEAL)


def mask_notification_params_on_task(
    task: dict,
    action: server.api.constants.MaskOperations,
):
    """
    Mask notification config params from the task object
    :param task:    The task object to mask
    :param action:  The masking operation to perform on the notification config params (conceal/redact)
    """
    mask_op = _notification_params_mask_op(action)
    run_uid = get_in(task, "metadata.uid")
    project = get_in(task, "metadata.project")
    notifications = task.get("spec", {}).get("notifications", [])
    masked_notifications = []
    if notifications:
        for notification in notifications:
            notification_object = mlrun.model.Notification.from_dict(notification)
            masked_notifications.append(
                mask_op(project, run_uid, notification_object).to_dict()
            )
    task.setdefault("spec", {})["notifications"] = masked_notifications


def _notification_params_mask_op(
    action,
) -> typing.Callable[[str, str, mlrun.model.Notification], mlrun.model.Notification]:
    return {
        server.api.constants.MaskOperations.CONCEAL: _conceal_notification_params_with_secret,
        server.api.constants.MaskOperations.REDACT: _redact_notification_params,
    }[action]


def _conceal_notification_params_with_secret(
    project: str, parent: str, notification_object: mlrun.model.Notification
) -> mlrun.model.Notification:
    if (
        notification_object.secret_params
        and "secret" not in notification_object.secret_params
    ):

        # create secret key from a hash of the secret params. this will allow multiple notifications with the same
        # params to share the same secret (saving secret storage space).
        # TODO: add holders to the secret content, so we can monitor when all runs that use the secret are deleted.
        #       as we currently don't delete runs unless the project is deleted (in which case, the entire secret is
        #       deleted), we don't need the mechanism yet.
        secret_key = server.api.crud.Secrets().generate_client_project_secret_key(
            server.api.crud.SecretsClientType.notifications,
            _generate_notification_secret_key(notification_object),
        )
        server.api.crud.Secrets().store_project_secrets(
            project,
            mlrun.common.schemas.SecretsData(
                provider=mlrun.common.schemas.SecretProviderName.kubernetes,
                secrets={secret_key: json.dumps(notification_object.secret_params)},
            ),
            allow_internal_secrets=True,
        )
        notification_object.secret_params = {"secret": secret_key}

    return notification_object


def _redact_notification_params(
    project: str, parent: str, notification_object: mlrun.model.Notification
) -> mlrun.model.Notification:
    if not notification_object.secret_params:
        return notification_object

    # If the notification params contain a secret key, we consider them concealed and don't redact them
    if "secret" in notification_object.secret_params:
        return notification_object

    for param in notification_object.secret_params:
        notification_object.secret_params[param] = "REDACTED"

    return notification_object


def _generate_notification_secret_key(
    notification_object: mlrun.model.Notification,
) -> str:
    # hash notification params to generate a unique secret key
    return sha224(
        json.dumps(notification_object.secret_params, sort_keys=True).encode("utf-8")
    ).hexdigest()


def unmask_notification_params_secret_on_task(
    db: server.api.db.base.DBInterface,
    db_session: Session,
    run: typing.Union[dict, mlrun.model.RunObject],
):
    if isinstance(run, dict):
        run = mlrun.model.RunObject.from_dict(run)

    notifications = []
    for notification in run.spec.notifications:
        invalid_notifications = []
        try:
            notifications.append(
                unmask_notification_params_secret(run.metadata.project, notification)
            )
        except Exception as exc:
            logger.warning(
                "Failed to unmask notification params, notification will not be sent",
                project=run.metadata.project,
                run_uid=run.metadata.uid,
                notification=notification.name,
                exc=err_to_str(exc),
            )
            # set error status in order to later save the db
            notification.status = mlrun.common.schemas.NotificationStatus.ERROR
            invalid_notifications.append(notification)

        if invalid_notifications:
            db.store_run_notifications(
                db_session,
                invalid_notifications,
                run.metadata.uid,
                run.metadata.project,
            )

    run.spec.notifications = notifications

    return run


def unmask_notification_params_secret(
    project: str, notification_object: mlrun.model.Notification
) -> mlrun.model.Notification:
    secret_params = notification_object.secret_params or {}
    params_secret = secret_params.get("secret", "")
    if not params_secret:
        return notification_object

    k8s = server.api.utils.singletons.k8s.get_k8s_helper()
    if not k8s:
        raise mlrun.errors.MLRunRuntimeError(
            "Not running in k8s environment, cannot load notification params secret"
        )

    notification_object.secret_params = json.loads(
        server.api.crud.Secrets().get_project_secret(
            project,
            mlrun.common.schemas.SecretProviderName.kubernetes,
            secret_key=params_secret,
            allow_internal_secrets=True,
            allow_secrets_from_k8s=True,
        )
    )

    return notification_object


def delete_notification_params_secret(
    project: str, notification_object: mlrun.model.Notification
) -> None:
    secret_params = notification_object.secret_params or {}
    params_secret = secret_params.get("secret", "")
    if not params_secret:
        return

    k8s = server.api.utils.singletons.k8s.get_k8s_helper()
    if not k8s:
        raise mlrun.errors.MLRunRuntimeError(
            "Not running in k8s environment, cannot delete notification params secret"
        )

    server.api.crud.Secrets().delete_project_secret(
        project,
        mlrun.common.schemas.SecretProviderName.kubernetes,
        secret_key=params_secret,
        allow_internal_secrets=True,
        allow_secrets_from_k8s=True,
    )


def validate_and_mask_notification_list(
    notifications: typing.List[
        typing.Union[mlrun.model.Notification, mlrun.common.schemas.Notification, dict]
    ],
    parent: str,
    project: str,
) -> typing.List[mlrun.model.Notification]:
    """
    Validates notification schema, uniqueness and masks notification params with secret if needed.
    If at least one of the validation steps fails, the function will raise an exception and cause the API to return
    an error response.
    :param notifications: list of notification objects
    :param parent: parent identifier
    :param project: project name
    :return: list of validated and masked notification objects
    """
    notification_objects = []

    for notification in notifications:
        if isinstance(notification, dict):
            notification_object = mlrun.model.Notification.from_dict(notification)
        elif isinstance(notification, mlrun.common.schemas.Notification):
            notification_object = mlrun.model.Notification.from_dict(
                notification.dict()
            )
        elif isinstance(notification, mlrun.model.Notification):
            notification_object = notification
        else:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "notification must be a dict or a Notification object"
            )

        # validate notification schema
        mlrun.common.schemas.Notification(**notification_object.to_dict())

        notification_objects.append(notification_object)

    mlrun.model.Notification.validate_notification_uniqueness(notification_objects)

    return [
        _conceal_notification_params_with_secret(project, parent, notification_object)
        for notification_object in notification_objects
    ]


# TODO: split enrichment and validation to separate functions should be in the launcher
def apply_enrichment_and_validation_on_function(
    function,
    auth_info: mlrun.common.schemas.AuthInfo,
    ensure_auth: bool = True,
    perform_auto_mount: bool = True,
    validate_service_account: bool = True,
    mask_sensitive_data: bool = True,
    ensure_security_context: bool = True,
):
    """
    This function should be used only on server side.

    This function is utilized in several flows as a consequence of different endpoints in MLRun for deploying different
    runtimes such as dask and nuclio, depends on the flow and runtime we decide which util functions we
    want to apply on the runtime.

    When adding a new util function, go through the other flows that utilize the function
    and make sure to specify the appropriate flag for each runtime.
    """
    # if auth given in request ensure the function pod will have these auth env vars set, otherwise the job won't
    # be able to communicate with the api
    if ensure_auth:
        ensure_function_has_auth_set(function, auth_info)

    # if this was triggered by the UI, we will need to attempt auto-mount based on auto-mount config and params passed
    # in the auth_info. If this was triggered by the SDK, then auto-mount was already attempted and will be skipped.
    if perform_auto_mount:
        try_perform_auto_mount(function, auth_info)

    # Validate function's service-account, based on allowed SAs for the project, if existing in a project-secret.
    if validate_service_account:
        process_function_service_account(function)

    if mask_sensitive_data:
        mask_function_sensitive_data(function, auth_info)

    if ensure_security_context:
        ensure_function_security_context(function, auth_info)


def ensure_function_auth_and_sensitive_data_is_masked(
    function,
    auth_info: mlrun.common.schemas.AuthInfo,
    allow_empty_access_key: bool = False,
):
    ensure_function_has_auth_set(function, auth_info, allow_empty_access_key)
    mask_function_sensitive_data(function, auth_info)


def mask_function_sensitive_data(function, auth_info: mlrun.common.schemas.AuthInfo):
    if not mlrun.runtimes.RuntimeKinds.is_local_runtime(function.kind):
        _mask_v3io_access_key_env_var(function, auth_info)
        _mask_v3io_volume_credentials(function, auth_info)


def _mask_v3io_volume_credentials(
    function: mlrun.runtimes.pod.KubeResource,
    auth_info: mlrun.common.schemas.AuthInfo = None,
):
    """
    Go over all of the flex volumes with v3io/fuse driver of the function and try mask their access key to a secret
    """
    get_item_attribute = mlrun.runtimes.utils.get_item_name
    v3io_volume_indices = []
    # to prevent the code from having to deal both with the scenario of the volume as V1Volume object and both as
    # (sanitized) dict (it's also snake case vs camel case), transforming all to dicts
    new_volumes = []
    k8s_api_client = kubernetes.client.ApiClient()
    for volume in function.spec.volumes:
        if isinstance(volume, dict):
            if "flexVolume" in volume:
                # mlrun.platforms.iguazio.v3io_to_vol generates a dict with a class in the flexVolume field
                if not isinstance(volume["flexVolume"], dict):
                    # sanity
                    if isinstance(
                        volume["flexVolume"], kubernetes.client.V1FlexVolumeSource
                    ):
                        volume[
                            "flexVolume"
                        ] = k8s_api_client.sanitize_for_serialization(
                            volume["flexVolume"]
                        )
                    else:
                        raise mlrun.errors.MLRunInvalidArgumentError(
                            f"Unexpected flex volume type: {type(volume['flexVolume'])}"
                        )
            new_volumes.append(volume)
        elif isinstance(volume, kubernetes.client.V1Volume):
            new_volumes.append(k8s_api_client.sanitize_for_serialization(volume))
        else:
            raise mlrun.errors.MLRunInvalidArgumentError(
                f"Unexpected volume type: {type(volume)}"
            )
    function.spec.volumes = new_volumes

    for index, volume in enumerate(function.spec.volumes):
        if volume.get("flexVolume", {}).get("driver") == "v3io/fuse":
            v3io_volume_indices.append(index)
    if v3io_volume_indices:
        volume_name_to_volume_mounts = collections.defaultdict(list)
        for volume_mount in function.spec.volume_mounts:
            # sanity
            if not get_item_attribute(volume_mount, "name"):
                logger.warning(
                    "Found volume mount without name, skipping it for volume masking username resolution",
                    volume_mount=volume_mount,
                )
                continue
            volume_name_to_volume_mounts[
                get_item_attribute(volume_mount, "name")
            ].append(volume_mount)
        for index in v3io_volume_indices:
            volume = function.spec.volumes[index]
            flex_volume = volume["flexVolume"]
            # if it's already referencing a secret, nothing to do
            if flex_volume.get("secretRef"):
                continue
            access_key = flex_volume.get("options", {}).get("accessKey")
            # sanity
            if not access_key:
                logger.warning(
                    "Found v3io fuse volume without access key, skipping masking",
                    volume=volume,
                )
                continue
            if not volume.get("name"):
                logger.warning(
                    "Found volume without name, skipping masking", volume=volume
                )
                continue
            username = _resolve_v3io_fuse_volume_access_key_matching_username(
                function,
                volume,
                volume["name"],
                volume_name_to_volume_mounts,
                auth_info,
            )
            if not username:
                continue
            secret_name = server.api.crud.Secrets().store_auth_secret(
                mlrun.common.schemas.AuthSecretData(
                    provider=mlrun.common.schemas.SecretProviderName.kubernetes,
                    username=username,
                    access_key=access_key,
                )
            )

            del flex_volume["options"]["accessKey"]
            flex_volume["secretRef"] = {"name": secret_name}


def _resolve_v3io_fuse_volume_access_key_matching_username(
    function: mlrun.runtimes.pod.KubeResource,
    volume: dict,
    volume_name: str,
    volume_name_to_volume_mounts: dict,
    auth_info: mlrun.common.schemas.AuthInfo = None,
) -> typing.Optional[str]:
    """
    Usually v3io fuse mount is set using mlrun.mount_v3io, which by default add a volume mount to /users/<username>, try
    to resolve the username from there.
    If it's not found (user may set custom volume mounts), try to look for V3IO_USERNAME env var.
    If it's still not found, look for the username in the auth info provided from the REST call (assuming we got here
    through store-function API flow, for example).
    If it's not found, skip masking for this volume.
    :return: the resolved username (string), none if not found.
    """

    get_item_attribute = mlrun.runtimes.utils.get_item_name
    username = None
    found_more_than_one_username = False
    for volume_mount in volume_name_to_volume_mounts[volume_name]:
        # volume_mount may be an V1VolumeMount instance (object access, snake case) or sanitized dict (dict
        # access, camel case)
        sub_path = get_item_attribute(volume_mount, "subPath") or get_item_attribute(
            volume_mount, "sub_path"
        )
        if sub_path and sub_path.startswith("users/"):
            username_from_sub_path = sub_path.replace("users/", "")
            if username_from_sub_path:
                if username is not None and username != username_from_sub_path:
                    found_more_than_one_username = True
                    break
                username = username_from_sub_path
    if found_more_than_one_username:
        logger.warning(
            "Found more than one user for volume, skipping masking",
            volume=volume,
            volume_mounts=volume_name_to_volume_mounts[volume_name],
        )
        return None
    if not username:
        v3io_username = function.get_env("V3IO_USERNAME")
        if not v3io_username and auth_info:
            v3io_username = auth_info.username

        if not v3io_username or not isinstance(v3io_username, str):
            logger.warning(
                "Could not resolve username from volume mount or env vars, skipping masking",
                volume=volume,
                volume_mounts=volume_name_to_volume_mounts[volume_name],
                env=function.spec.env,
            )
            return None
        username = v3io_username
    return username


def _mask_v3io_access_key_env_var(
    function: mlrun.runtimes.pod.KubeResource, auth_info: mlrun.common.schemas.AuthInfo
):
    v3io_access_key = function.get_env("V3IO_ACCESS_KEY")
    # if it's already a V1EnvVarSource or dict instance, it's already been masked
    if (
        v3io_access_key
        and not isinstance(v3io_access_key, kubernetes.client.V1EnvVarSource)
        and not isinstance(v3io_access_key, dict)
    ):
        username = None
        v3io_username = function.get_env("V3IO_USERNAME")
        if v3io_username and isinstance(v3io_username, str):
            username = v3io_username
        if not username:
            if server.api.utils.auth.verifier.AuthVerifier().is_jobs_auth_required():
                # auth_info should always has username, sanity
                if not auth_info.username:
                    raise mlrun.errors.MLRunInvalidArgumentError(
                        "Username is missing from auth info"
                    )
                username = auth_info.username
            else:
                logger.warning(
                    "Could not find matching username for v3io access key in env or session, skipping masking",
                )
                return
        secret_name = server.api.crud.Secrets().store_auth_secret(
            mlrun.common.schemas.AuthSecretData(
                provider=mlrun.common.schemas.SecretProviderName.kubernetes,
                username=username,
                access_key=v3io_access_key,
            )
        )
        access_key_secret_key = (
            mlrun.common.schemas.AuthSecretData.get_field_secret_key("access_key")
        )
        function.set_env_from_secret(
            "V3IO_ACCESS_KEY", secret_name, access_key_secret_key
        )


def ensure_function_has_auth_set(
    function: mlrun.runtimes.BaseRuntime,
    auth_info: mlrun.common.schemas.AuthInfo,
    allow_empty_access_key: bool = False,
):
    """
    :param function:    Function object.
    :param auth_info:   The auth info of the request.
    :param allow_empty_access_key: Whether to raise an error if access key wasn't set or requested to get generated
    """
    if (
        not mlrun.runtimes.RuntimeKinds.is_local_runtime(function.kind)
        and server.api.utils.auth.verifier.AuthVerifier().is_jobs_auth_required()
    ):
        function: mlrun.runtimes.pod.KubeResource
        if (
            function.metadata.credentials.access_key
            == mlrun.model.Credentials.generate_access_key
        ):
            if not auth_info.access_key:
                auth_info.access_key = server.api.utils.auth.verifier.AuthVerifier().get_or_create_access_key(
                    auth_info.session
                )
                # created an access key with control and data session plane, so enriching auth_info with those planes
                auth_info.planes = [
                    server.api.utils.clients.iguazio.SessionPlanes.control,
                    server.api.utils.clients.iguazio.SessionPlanes.data,
                ]

            function.metadata.credentials.access_key = auth_info.access_key

        if not function.metadata.credentials.access_key:
            if allow_empty_access_key:
                # skip further enrichment as we allow empty access key
                return

            raise mlrun.errors.MLRunInvalidArgumentError(
                "Function access key must be set (function.metadata.credentials.access_key)"
            )

        # after access key was passed or enriched with the condition above, we mask it with creating auth secret
        if not function.metadata.credentials.access_key.startswith(
            mlrun.model.Credentials.secret_reference_prefix
        ):
            if not auth_info.username:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    "Username is missing from auth info"
                )
            secret_name = server.api.crud.Secrets().store_auth_secret(
                mlrun.common.schemas.AuthSecretData(
                    provider=mlrun.common.schemas.SecretProviderName.kubernetes,
                    username=auth_info.username,
                    access_key=function.metadata.credentials.access_key,
                )
            )
            function.metadata.credentials.access_key = (
                f"{mlrun.model.Credentials.secret_reference_prefix}{secret_name}"
            )
        else:
            secret_name = function.metadata.credentials.access_key.lstrip(
                mlrun.model.Credentials.secret_reference_prefix
            )

        access_key_secret_key = (
            mlrun.common.schemas.AuthSecretData.get_field_secret_key("access_key")
        )
        auth_env_vars = {
            mlrun.runtimes.constants.FunctionEnvironmentVariables.auth_session: (
                secret_name,
                access_key_secret_key,
            )
        }
        for env_key, (secret_name, secret_key) in auth_env_vars.items():
            function.set_env_from_secret(env_key, secret_name, secret_key)


def try_perform_auto_mount(function, auth_info: mlrun.common.schemas.AuthInfo):
    if (
        mlrun.runtimes.RuntimeKinds.is_local_runtime(function.kind)
        or function.spec.disable_auto_mount
    ):
        return
    # Retrieve v3io auth params from the caller auth info
    override_params = {}
    if auth_info.data_session or auth_info.access_key:
        override_params["access_key"] = auth_info.data_session or auth_info.access_key
    if auth_info.username:
        override_params["user"] = auth_info.username

    function.try_auto_mount_based_on_config(override_params)


def process_function_service_account(function):
    # If we're not running inside k8s, skip this check as it's not relevant.
    if not server.api.utils.singletons.k8s.get_k8s_helper(
        silent=True
    ).is_running_inside_kubernetes_cluster():
        return

    (
        allowed_service_accounts,
        default_service_account,
    ) = resolve_project_default_service_account(function.metadata.project)

    function.validate_and_enrich_service_account(
        allowed_service_accounts, default_service_account
    )


def resolve_project_default_service_account(project_name: str):
    allowed_service_accounts = server.api.crud.secrets.Secrets().get_project_secret(
        project_name,
        mlrun.common.schemas.SecretProviderName.kubernetes,
        server.api.crud.secrets.Secrets().generate_client_project_secret_key(
            server.api.crud.secrets.SecretsClientType.service_accounts, "allowed"
        ),
        allow_secrets_from_k8s=True,
        allow_internal_secrets=True,
    )
    if allowed_service_accounts:
        allowed_service_accounts = [
            service_account.strip()
            for service_account in allowed_service_accounts.split(",")
        ]

    default_service_account = server.api.crud.secrets.Secrets().get_project_secret(
        project_name,
        mlrun.common.schemas.SecretProviderName.kubernetes,
        server.api.crud.secrets.Secrets().generate_client_project_secret_key(
            server.api.crud.secrets.SecretsClientType.service_accounts, "default"
        ),
        allow_secrets_from_k8s=True,
        allow_internal_secrets=True,
    )

    # If default SA was not configured for the project, try to retrieve it from global config (if exists)
    default_service_account = (
        default_service_account or mlrun.mlconf.function.spec.service_account.default
    )

    # Sanity check on project configuration
    if (
        default_service_account
        and allowed_service_accounts
        and default_service_account not in allowed_service_accounts
    ):
        raise mlrun.errors.MLRunInvalidArgumentError(
            f"Default service account {default_service_account} is not in list of allowed "
            + f"service accounts {allowed_service_accounts}"
        )

    return allowed_service_accounts, default_service_account


def ensure_function_security_context(
    function, auth_info: mlrun.common.schemas.AuthInfo
):
    """
    For iguazio we enforce that pods run with user id and group id depending on
    mlrun.mlconf.function.spec.security_context.enrichment_mode
    and mlrun.mlconf.function.spec.security_context.enrichment_group_id
    """

    # if security context is not required.
    # security context is not yet supported with spark runtime since it requires spark 3.2+
    if (
        mlrun.mlconf.function.spec.security_context.enrichment_mode
        == mlrun.common.schemas.SecurityContextEnrichmentModes.disabled.value
        or mlrun.runtimes.RuntimeKinds.is_local_runtime(function.kind)
        or function.kind == mlrun.runtimes.RuntimeKinds.spark
        # remote spark image currently requires running with user 1000 or root
        # and by default it runs with user 1000 (when security context is not set)
        or function.kind == mlrun.runtimes.RuntimeKinds.remotespark
        or not mlrun.mlconf.is_running_on_iguazio()
    ):
        return

    function: mlrun.runtimes.pod.KubeResource

    # TODO: enrich old functions being triggered after upgrading mlrun with project owner uid.
    #  Enrichment with retain enrichment mode should occur on function creation only.
    if (
        mlrun.mlconf.function.spec.security_context.enrichment_mode
        == mlrun.common.schemas.SecurityContextEnrichmentModes.retain.value
        and function.spec.security_context is not None
        and function.spec.security_context.run_as_user is not None
        and function.spec.security_context.run_as_group is not None
    ):
        logger.debug(
            "Security context is already set",
            mode=mlrun.mlconf.function.spec.security_context.enrichment_mode,
            function_name=function.metadata.name,
        )
        return

    if mlrun.mlconf.function.spec.security_context.enrichment_mode in [
        mlrun.common.schemas.SecurityContextEnrichmentModes.override.value,
        mlrun.common.schemas.SecurityContextEnrichmentModes.retain.value,
    ]:
        # before iguazio 3.6 the user unix id is not passed in the session verification response headers
        # so we need to request it explicitly
        if auth_info.user_unix_id is None:
            iguazio_client = server.api.utils.clients.iguazio.Client()
            if (
                server.api.utils.clients.iguazio.SessionPlanes.control
                not in auth_info.planes
            ):
                logger.warning(
                    "Auth info doesn't contain a session tagged as a control session plane, trying to get user unix id",
                    function_name=function.metadata.name,
                )
                try:
                    auth_info.user_unix_id = iguazio_client.get_user_unix_id(
                        auth_info.session
                    )
                    # if we were able to get the user unix id it means we have a control session plane so adding that
                    # to the auth info
                    auth_info.planes.append(
                        server.api.utils.clients.iguazio.SessionPlanes.control
                    )
                except Exception as exc:
                    raise mlrun.errors.MLRunUnauthorizedError(
                        "Were unable to enrich user unix id, missing control plane session"
                    ) from exc
            else:
                auth_info.user_unix_id = iguazio_client.get_user_unix_id(
                    auth_info.session
                )

        # if enrichment group id is -1 we set group id to user unix id
        enriched_group_id = mlrun.mlconf.get_security_context_enrichment_group_id(
            auth_info.user_unix_id
        )
        logger.debug(
            "Enriching/overriding security context",
            mode=mlrun.mlconf.function.spec.security_context.enrichment_mode,
            function_name=function.metadata.name,
            enriched_group_id=enriched_group_id,
            user_unix_id=auth_info.user_unix_id,
        )
        function.spec.security_context = kubernetes.client.V1SecurityContext(
            run_as_user=auth_info.user_unix_id,
            run_as_group=enriched_group_id,
        )

    else:
        raise mlrun.errors.MLRunInvalidArgumentError(
            f"Invalid security context enrichment mode {mlrun.mlconf.function.spec.security_context.enrichment_mode}"
        )


def submit_run_sync(
    db_session: Session, auth_info: mlrun.common.schemas.AuthInfo, data
) -> typing.Tuple[str, str, str, typing.Dict]:
    """
    :return: Tuple with:
        1. str of the project of the run
        2. str of the kind of the function of the run
        3. str of the uid of the run that started execution (None when it was scheduled)
        4. dict of the response info
    """
    run_uid = None
    project = None
    response = None
    try:
        fn, task = _generate_function_and_task_from_submit_run_body(db_session, data)

        run_db = get_run_db_instance(db_session)
        fn.set_db_connection(run_db)

        task_for_logging = copy.deepcopy(task)
        for notification in task_for_logging["spec"].get("notifications", []):
            mlrun.utils.notifications.notification_pusher.sanitize_notification(
                notification
            )

        logger.info("Submitting run", function=fn.to_dict(), task=task_for_logging)
        schedule = data.get("schedule")
        if schedule:
            cron_trigger = schedule
            if isinstance(cron_trigger, dict):
                cron_trigger = mlrun.common.schemas.ScheduleCronTrigger(**cron_trigger)
            schedule_labels = task["metadata"].get("labels")

            # if the task is pointing to a remote function (hub://), we need to save it to the db
            # and update the task to point to the saved function, so that the scheduler will be able to
            # access the db version of the function, and not the remote one (which can be changed between runs)
            if "://" in task["spec"]["function"]:
                function_uri = fn.save(versioned=True)
                data.pop("function", None)
                data.pop("function_url", None)
                task["spec"]["function"] = function_uri.replace("db://", "")

            is_update = get_scheduler().store_schedule(
                db_session,
                auth_info,
                task["metadata"]["project"],
                task["metadata"]["name"],
                mlrun.common.schemas.ScheduleKinds.job,
                data,
                cron_trigger,
                schedule_labels,
            )

            project = task["metadata"]["project"]
            response = {
                "schedule": schedule,
                "project": task["metadata"]["project"],
                "name": task["metadata"]["name"],
                # indicate whether it was created or modified
                "action": "modified" if is_update else "created",
            }

        else:
            # When processing a hyper-param run, secrets may be needed to access the parameters file (which is accessed
            # locally from the mlrun service pod) - include project secrets and the caller's access key
            param_file_secrets = (
                server.api.crud.Secrets()
                .list_project_secrets(
                    task["metadata"]["project"],
                    mlrun.common.schemas.SecretProviderName.kubernetes,
                    allow_secrets_from_k8s=True,
                )
                .secrets
            )
            param_file_secrets["V3IO_ACCESS_KEY"] = (
                auth_info.data_session or auth_info.access_key
            )

            run = fn.run(
                task,
                watch=False,
                param_file_secrets=param_file_secrets,
                auth_info=auth_info,
            )
            run_uid = run.metadata.uid
            project = run.metadata.project
            if run:
                response = run.to_dict()

    except HTTPException:
        logger.error(traceback.format_exc())
        raise
    except mlrun.errors.MLRunHTTPStatusError:
        raise
    except Exception as err:
        logger.error(traceback.format_exc())
        log_and_raise(
            HTTPStatus.BAD_REQUEST.value,
            reason=f"runtime error: {err_to_str(err)}",
        )

    logger.info("Run submission succeeded", run_uid=run_uid, function=fn.metadata.name)
    return project, fn.kind, run_uid, {"data": response}


# uid is hexdigest of sha1 value, which is double the digest size due to hex encoding
hash_len = sha1().digest_size * 2
uid_regex = re.compile(f"^[0-9a-f]{{{hash_len}}}$", re.IGNORECASE)


def parse_reference(reference: str):
    tag = None
    uid = None
    regex_match = uid_regex.match(reference)
    if not regex_match:
        tag = reference
    else:
        uid = regex_match.string
    return tag, uid
