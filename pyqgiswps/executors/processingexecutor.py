#
# Copyright 2018 3liz
# Author: David Marteau
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
import asyncio
import os
import traceback
import shutil
import logging
import lxml

from typing import Iterable

try:
    # XXX On android userland, /proc/stat is not readable
    import psutil
    HAVE_PSUTIL = True
except Exception:
    print("WARNING: PSUtil is not available, memory stats will be disabled !")
    HAVE_PSUTIL = False

from contextlib import contextmanager
from datetime import datetime

from pyqgiswps.app.WPSResponse import STATUS
from pyqgiswps.logger import logfile_context
from pyqgiswps.utils.lru import lrucache
from pyqgiswps.exceptions import (NoApplicableCode, ProcessException)
from pyqgiswps.config import confservice
from pyqgiswps.app.Process import WPSProcess

from pyqgiswps.poolserver.client import (create_client, 
                                         RequestBackendError, 
                                         MaxRequestsExceeded)

from pyqgisservercontrib.core.watchfiles import watchfiles

from .processfactory import get_process_factory
from .logstore import logstore

LOGGER = logging.getLogger('SRVLOG')


class ExecutorError(Exception):
    pass

class UnknownProcessError(ExecutorError):
    pass

class StorageNotFound(ExecutorError): 
    pass

class ProcessingExecutor:
    """ Progessing executor
    """

    def __init__(self, processes: Iterable[WPSProcess]):

        maxqueuesize = confservice.getint('server','maxqueuesize')

        self._pool = create_client( maxqueuesize )
        self._context_processes = lrucache(50)
        self._factory = get_process_factory()
        self._reload_handler = None

        self.processes = { p.identifier: p for p in processes }

        # Launch the cleanup task
        self.schedule_cleanup()

        # Initialize restart handler
        self.init_restart_handler()

    def init_restart_handler(self):
        restartmon = confservice.get('server','restartmon')
        if restartmon:
            LOGGER.info(f"Reload handler monitoring: {restartmon}")
            check_time = confservice.getint('server','restartmon_check_time', 3000)
            self._reload_handler = watchfiles([restartmon],
                                              lambda *_args: self.reload_qgis_processes(),
                                              check_time)
            self._reload_handler.start()

    def get_status( self, uuid=None, **kwargs ):
        """ Return status of the stored processes

            :param uuid: the uuid of the required process. 
             If set to None, return all the stored status.

            :return: The status or the list of status.
        """
        return logstore.get_status(uuid, **kwargs)

    def delete_results( self, uuid):
        """ Delete process results and status 
 
            :param uuid: the uuid of the required process. 
             If set to None, return all the stored status.

            :return: True if the status has been deleted.
        """
        rec = logstore.get_status(uuid)
        if rec is None:
            raise FileNotFoundError(uuid)
        try:
            if STATUS[rec['status']] < STATUS.DONE_STATUS:
                return False
        except KeyError:
            # Handle legacy status
            pass

        workdir = os.path.abspath(confservice.get('server','workdir'))

        # Delete the working directory
        uuid_str = rec['uuid']
        workdir = os.path.join(workdir, uuid_str)
        LOGGER.info("Cleaning response status: %s", uuid_str)
        try:
            if os.path.isdir(workdir):
                shutil.rmtree(workdir)
        except Exception as err:
            LOGGER.error('Unable to remove directory: %s: %s', workdir, err)
        # Delete the record/response
        logstore.delete_response(uuid_str)
        return True

    def get_results(self, uuid):
        """ Return results status
        """
        return logstore.get_results(uuid) 

    def terminate(self):
        """ Execute cleanup tasks
        """
        if self._reload_handler:
            self._reload_handler.stop()

        if self._cleanup_task:
            self._cleanup_task.cancel()

        if self._pool:
            self._pool.close()

    def list_processes( self ):
        """ List all available processes

            :param ident: process identifier

            :return: A list of processes infos
        """
        return self.processes.values()

    def get_processes( self, identifiers, map_uri=None ):
        """ Override executors.get_process
        """
        try:
            processes = [self.processes[ident] for ident in identifiers]
        except KeyError as exc:
            raise UnknownProcessError(str(exc))

        # TODO Allow create contextualized processes from non-processing processes
        if self._factory.qgis_enabled and map_uri is not None:

            # Create a new instance of a process for the given context
            # Contextualized processes are stored in lru cache
            def _test(p):
                return (map_uri,p.identifier) not in self._context_processes

            if any(_test(p) for p in processes):
                processes = self._factory.create_contextualized_processes(identifiers, map_uri=map_uri)
                # Update cache
                for p in processes:
                    self._context_processes[(map_uri,p.identifier)] = p 
            else:
                # Get from cache
                processes = [self._context_processes[(map_uri,p.identifier)] for p in processes]
        return processes

    def reload_qgis_processes(self) -> None:
        """ Reload Qgis processes 
        """
        factory = self._factory
        if factory.qgis_enabled:
            try:
                LOGGER.info("Reloading Qgis providers")
                self.processes = { p.identifier: p for p in factory.create_qgis_processes() }
                self._context_processes.clear()
            except ProcessException:
                LOGGER.error("Failed to reload Qgis processes")

    def schedule_cleanup( self ) -> None:
        """ Schedule a periodic cleanup
        """
        interval = confservice.getint('server','cleanup_interval')
        async def _run_cleanup():
            while True:
                await asyncio.sleep(interval)
                try:
                    self._clean_processes()
                except Exception as e:
                    traceback.print_exc()
                    LOGGER.error("Cleanup task failed: %s", e)

        # Schedule the task
        self._cleanup_task = asyncio.ensure_future( _run_cleanup() )

    async def execute( self, wps_request, wps_response ):
        """ Execute a process

            :return: wps_response or None
        """
        process = wps_response.process
        process.uuid = wps_response.uuid

        # Start request
        logstore.log_request( process.uuid, wps_request)

        # Check if we need no store the response
        wps_response.store = (wps_response.status >= STATUS.STORE_STATUS)

        # Get request defined timeout
        timeout = wps_request.timeout

        apply_future = self._pool.apply_async(self._run_process, args=(process.handler, wps_request, wps_response),
                                              timeout=timeout)

        if wps_response.status == STATUS.STORE_AND_UPDATE_STATUS:
            # ---------------------------------
            # Run the processe asynchronously
            # ---------------------------------
            async def do_execute_async():
                # Handle errors while we are going async
                try:
                    await apply_future
                except asyncio.TimeoutError:
                    wps_response.update_status("Timeout Error", None, STATUS.ERROR_STATUS)
                except MaxRequestsExceeded:
                    wps_response.update_status("Server busy, please retry later", None, STATUS.ERROR_STATUS)
                except Exception:
                    # There is no point to let the error go outside
                    LOGGER.error(traceback.format_exc())
                    wps_response.update_status("Internal Error", None, STATUS.ERROR_STATUS)
                    pass

            # Fire and forget
            asyncio.ensure_future(do_execute_async())

            wps_response.update_status('Task accepted')
            return wps_response.document                        
        else:
            # -------------------------------
            # Run process and wait for response 
            # -------------------------------
            try:
                return await apply_future
            except asyncio.TimeoutError:
                wps_response.update_status("Timeout Error", None, STATUS.ERROR_STATUS)
                raise NoApplicableCode("Execute Timeout", code=424)
            except MaxRequestsExceeded:
                raise NoApplicableCode("Server busy, please retry later", code=509)
            except RequestBackendError as e:
                if isinstance(e.response, ProcessException):
                    raise NoApplicableCode("Process Error", code=424)
                else:
                    raise

    @staticmethod
    def _run_process(handler, wps_request, wps_response):
        """ Run WPS  process

            Note that there is nothing to return is async mode
        """
        try:
            workdir = wps_response.process.workdir
            # Change current dir to workdir
            os.chdir(workdir)

            wps_response.update_status('Task started', 0)

            with logfile_context(workdir, 'processing'), memory_logger(wps_response):
                handler(wps_request, wps_response)
                wps_response.update_status('Task finished',100, STATUS.DONE_STATUS)

                ## Return pickable response
                return lxml.etree.tostring(wps_response.document, pretty_print=True)

        except ProcessException as e:
            wps_response.update_status("%s" % e, None, STATUS.ERROR_STATUS)
            raise
        except Exception:
            wps_response.update_status("Internal error", None, STATUS.ERROR_STATUS)
            raise


    @staticmethod
    def _clean_processes() -> None:
        """ Clean up all processes
            Remove status and delete processes workdir

            Dangling tasks will be removed: these are tasks no marked finished but 
            for which the timeout has expired. Usually this is task for wich the process
            as died (memory exhaustion, segfault....)
        """
        # Iterate over done processes
        cfg = confservice['server']
        workdir_base = os.path.abspath(cfg.get('workdir'))

        # The response expiration in seconds
        expire_default = cfg.getint('response_expiration')

        now_ts = datetime.utcnow().timestamp()

        for _, rec in list(logstore.records):
            timestamp = rec.get('timestamp')
            dangling  = timestamp is None
            try:
                if not dangling and STATUS[rec['status']] < STATUS.DONE_STATUS:
                    # Check that the task is not in dangling state
                    timeout  = rec.get('timeout')
                    dangling = timeout is None or (now_ts - int(timestamp)) >= timeout
                    if not dangling:
                        continue
            except KeyError:
                # Handle legacy status
                pass
            
            expiration = rec.get('expiration', expire_default)
            notpinned  = not rec.get('pinned',False)
            if notpinned and (dangling or (now_ts - int(timestamp)) >= expiration):
                # Delete the working directory
                uuid_str = rec['uuid']
                workdir = os.path.join(workdir_base, uuid_str)
                LOGGER.info("Cleaning response status: %s", uuid_str)
                try:
                    if os.path.isdir(workdir):
                        shutil.rmtree(workdir)
                except Exception as err:
                    LOGGER.error('Unable to remove directory: %s', err)
                # Delete the record/response
                logstore.delete_response(uuid_str)

if HAVE_PSUTIL:
    @contextmanager
    def memory_logger(response) -> None:
        """ Log memory consumption
        """
        # Get the current process info
        process = psutil.Process(os.getpid())
        start_mem = process.memory_info().rss
        mb = 1024*1024.0
        try:
            yield
        finally:
            # Log memory infos
            end_mem = process.memory_info().rss
            LOGGER.info(("{4}:{0} memory: start={1:.3f}Mb end={2:.3f}Mb "
                         "delta={3:.3f}Mb").format(str(response.uuid)[:8], 
                                                   start_mem/mb, 
                                                   end_mem/mb, 
                                                   (end_mem - start_mem)/mb,
                                                   response.process.identifier))
else:
    @contextmanager
    def memory_logger(response) -> None:
        try:
            yield 
        finally:
            LOGGER.info("{4}:{0} memory stats not available")

