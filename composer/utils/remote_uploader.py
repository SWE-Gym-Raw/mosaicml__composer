# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

"""Util class to upload file."""

import logging
import multiprocessing
import os
import pathlib
import shutil
import tempfile
import time
import uuid
from concurrent.futures import Future, ProcessPoolExecutor
from typing import List, Optional

from composer.utils.dist import broadcast_object_list, get_global_rank, get_local_rank
from composer.utils.file_helpers import (
    maybe_create_object_store_from_uri,
    parse_uri,
    validate_credentials,
)
from composer.utils.object_store.mlflow_object_store import MLFLOW_DBFS_PATH_PREFIX, MLFlowObjectStore
from composer.utils.object_store.object_store import (
    ObjectStore,
    ObjectStoreTransientError,
)
from composer.utils.object_store.uc_object_store import UCObjectStore
from composer.utils.retrying import retry

log = logging.getLogger(__name__)

__all__ = ['RemoteUploader']


def _build_dbfs_backend(path: str) -> ObjectStore:
    if path.startswith(MLFLOW_DBFS_PATH_PREFIX):
        return MLFlowObjectStore(path=path)
    UCObjectStore.validate_path(path)
    return UCObjectStore(path=path)


def _upload_file_to_object_store(
    remote_folder: str,
    is_dbfs: bool,
    dbfs_path: str,
    remote_file_name: str,
    local_file_path: str,
    overwrite: bool,
    num_attempts: int,
) -> int:
    if is_dbfs:
        object_store: ObjectStore = _build_dbfs_backend(dbfs_path)
    else:
        object_store: ObjectStore = maybe_create_object_store_from_uri(
            remote_folder,
        )  # pyright: ignore[reportGeneralTypeIssues]

    @retry(ObjectStoreTransientError, num_attempts=num_attempts)
    def upload_file(retry_index: int = 0):
        if retry_index == 0 and not overwrite:
            try:
                object_store.get_object_size(remote_file_name)
            except FileNotFoundError:
                # Good! It shouldn't exist.
                pass
            else:
                raise FileExistsError(f'Object {remote_file_name} already exists, but overwrite was set to False.')
        log.info(f'Uploading file {local_file_path} to {remote_file_name}')
        object_store.upload_object(
            object_name=remote_file_name,
            filename=local_file_path,
        )
        os.remove(local_file_path)

    log.info(f'Finished uploading file {local_file_path} to {remote_file_name}')
    # When encountering issues with too much concurrency in uploads, staggering the uploads can help.
    # This stagger is intended for use when uploading model shards from every rank, and will effectively reduce
    # the concurrency by a factor of num GPUs per node.
    local_rank = get_local_rank()
    local_rank_stagger = int(os.environ.get('COMPOSER_LOCAL_RANK_STAGGER_SECONDS', 0))
    log.debug(f'Staggering uploads by {local_rank * local_rank_stagger} seconds on {local_rank} local rank.')
    time.sleep(local_rank * local_rank_stagger)
    upload_file()
    return 0


class RemoteUploader:
    """Class for uploading a file to object store asynchronously."""

    def __init__(
        self,
        remote_folder: str,
        num_concurrent_uploads: int = 2,
        num_attempts: int = 3,
    ):
        if num_concurrent_uploads < 1 or num_attempts < 1:
            raise ValueError(
                f'num_concurrent_uploads and num_attempts must be >= 1, but got {num_concurrent_uploads} and {num_attempts}',
            )

        self.remote_folder = remote_folder
        # A folder to use for staging uploads
        self._tempdir = tempfile.TemporaryDirectory()
        self._upload_staging_folder = self._tempdir.name
        backend, _, self.path = parse_uri(remote_folder)

        # Need some special handling for dbfs path
        self._is_dbfs = backend == 'dbfs'
        self.object_store: Optional[ObjectStore] = None

        self.num_attempts = num_attempts

        self.executor = ProcessPoolExecutor(
            max_workers=num_concurrent_uploads,
            mp_context=multiprocessing.get_context('spawn'),
        )

        # Used internally to track the future status.
        # If a future completed successfully, we'll remove it from this list
        # when check_workers() or wait() is called
        self.futures: List[Future] = []

    def init(self):
        # If it's dbfs path like: dbfs:/databricks/mlflow-tracking/{mlflow_experiment_id}/{mlflow_run_id}/
        # We need to fill out the experiment_id and run_id
        if not self._is_dbfs:
            if self.object_store is None:
                self.object_store = maybe_create_object_store_from_uri(self.remote_folder)
        else:
            if not self.path.startswith(MLFLOW_DBFS_PATH_PREFIX):
                if self.object_store is None:
                    self.object_store = _build_dbfs_backend(self.path)
                return
            if get_global_rank() == 0:
                if self.object_store is None:
                    self.object_store = _build_dbfs_backend(self.path)
                assert isinstance(self.object_store, MLFlowObjectStore)
                self.path = self.object_store.get_dbfs_path(self.path)
            path_list = [self.path]
            broadcast_object_list(path_list, src=0)
            self.path = path_list[0]
            if get_global_rank() != 0:
                self.object_store = _build_dbfs_backend(self.path)

        if get_global_rank() == 0:
            retry(
                ObjectStoreTransientError,
                self.num_attempts,
            )(lambda: validate_credentials(self.object_store, '.credentials_validated_successfully'))()

    def upload_file_async(
        self,
        remote_file_name: str,
        file_path: pathlib.Path,
        overwrite: bool,
    ):
        """Async call to submit a job for uploading.

        It returns a future, so users can track the status of the individual future.
        User can also call wait() to wait for all the futures.
        """
        # Copy file to staging folder
        copied_path = os.path.join(self._upload_staging_folder, str(uuid.uuid4()))
        os.makedirs(self._upload_staging_folder, exist_ok=True)
        shutil.copy2(file_path, copied_path)

        # Async upload file
        future = self.executor.submit(
            _upload_file_to_object_store,
            is_dbfs=self._is_dbfs,
            dbfs_path=self.path,
            remote_folder=self.remote_folder,
            remote_file_name=remote_file_name,
            local_file_path=copied_path,
            overwrite=overwrite,
            num_attempts=self.num_attempts,
        )
        self.futures.append(future)
        return future

    def check_workers(self):
        """Non-blocking call to check workers are either running or done.

        Traverse self.futures, and check if it's completed
        1. if it completed with exception, raise that exception
        2. if it completed without exception, remove it from self.futures
        """
        done_futures: List[Future] = []
        for future in self.futures:
            if future.done():
                # future.exception is a blocking call
                exception_or_none = future.exception()
                if exception_or_none is not None:
                    raise exception_or_none
                else:
                    done_futures.append(future)
        for future in done_futures:
            self.futures.remove(future)

    def wait(self):
        """Blocking call to wait all the futures to complete.

        If a future is done successfully, remove it from self.futures(),
        otherwise, raise the exception
        """
        for future in self.futures:
            exception_or_none = future.exception()
            if exception_or_none is not None:
                raise exception_or_none
        self.futures = []

    def wait_and_close(self):
        """Blocking call to wait all uploading to finish and close this uploader.

        After this function is called, users can not use this uploader
        to uploading anymore. So please only call wait_and_close() after submitting
        all uploading requests.
        """
        # make sure all workers are either running, or completed successfully
        self.wait()
        self.executor.shutdown(wait=True)
        log.debug('Finished all uploading tasks, closing RemoteUploader')
