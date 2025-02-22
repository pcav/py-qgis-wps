#
# Copyright 2018 3liz
# Author: David Marteau
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#

import logging
import uuid

from .basehandler import HTTPError, BaseHandler

from pyqgiswps.exceptions import InvalidParameterValue, OperationNotSupported
from pyqgiswps.accesspolicy import new_access_policy
from pyqgiswps.app.WPSRequest import WPSRequest

from typing import Optional

LOGGER = logging.getLogger('SRVLOG')


class WPSHandler(BaseHandler):
    """ Handle WPS requests
    """
    def initialize(self, filters=None):
        super().initialize()
    
        self._filters     = filters or []
        self.accesspolicy = new_access_policy()

    async def prepare(self):
        super().prepare()
        for filt in self._filters:
            await filt.apply( self )

    async def handle_wps_request(self, method_parser):
        """ Handle a wps request
        """
        wpsrequest = method_parser(self)

        wpsrequest.map_uri  = self.get_argument('MAP', default=None)
        wpsrequest.host_url = self.proxy_url()

        service = self.application.wpsservice
        LOGGER.debug('Request: %s', wpsrequest.operation)

        if wpsrequest.operation == 'getresults':
            response = service.get_results(wpsrequest.results_uuid)

        elif wpsrequest.operation == 'getcapabilities':
            response = service.get_capabilities(wpsrequest, self.accesspolicy)

        elif wpsrequest.operation == 'describeprocess':
            if any( not self.accesspolicy.allow(ident) for ident in wpsrequest.identifiers ):
                raise HTTPError(403,reason="Unauthorized operation")
            response = service.describe(wpsrequest.identifiers, map_uri=wpsrequest.map_uri)

        elif wpsrequest.operation == 'execute':
            if not self.accesspolicy.allow(wpsrequest.identifier):
                raise HTTPError(403,reason="Unauthorized operation")
            response = await service.execute(wpsrequest.identifier, wpsrequest, uuid.uuid1(),
                                             map_uri=wpsrequest.map_uri)
        else:
            raise OperationNotSupported("Unknown operation %r" % wpsrequest.operation)

        return response
 
    async def get(self):
        """ Handle Get Method
        """
        service = self.get_argument('SERVICE')
        if service.lower() != 'wps':
            raise InvalidParameterValue('parameter SERVICE [%s] not supported' % service, 'service')

        document = await self.handle_wps_request(WPSRequest.parse_get_request)

        self.write_xml(document)

    async def post(self):
        """ Handle POST method

            XXX Do not forget to set the max_buffer_size in HTTPServer arguments
            see http://www.tornadoweb.org/en/stable/tcpserver.html?highlight=max_buffer_size
        """
        document = await self.handle_wps_request(WPSRequest.parse_post_request)
        
        self.write_xml(document)

    def options(self, endpoint: Optional[str]=None) -> None:
        """ Implement OPTION for validating CORS
        """
        self.set_option_headers('GET, POST, OPTIONS')



class StatusHandler(BaseHandler):

    def get_wps_status( self, uuid: Optional[str]=None):
        """ Return the status of the processes
        """ 
        wps_status = self.application.wpsservice.get_status(uuid)
        if uuid is not None and wps_status is None:
            self.set_status(404)
            self.write_json({ 'error': 'process %s not found' % uuid })
            return

        # Replace the status url with the proxy_url if any
        proxy_url = self.proxy_url()

        # Add additional informations
        cfg = self.application.config
        def repl( s ):
            s['status_url'] = cfg['status_url'].format(host_url=proxy_url, uuid=s['uuid'])
            s['store_url']  = cfg['store_url'].format(host_url=proxy_url, uuid=s['uuid'], file="")
            s['request']    = f"{proxy_url}status/{s['uuid']}?key=request"
            return s

        if uuid is not None:
            wps_status = repl(wps_status)
        else:
            wps_status = list(map(repl, wps_status))
        
        self.write_json({ 'status': wps_status })

    def get_wps_request( self, uuid: str ):
        """ Return request infos
        """
        if uuid is None:
            self.set_status(400)
            self.write_json({ 'error': 'Missing uuid' })
            return

        wps_request = self.application.wpsservice.get_status(uuid, key='request')
        if uuid is not None and wps_request is None:
            self.set_status(404)
            self.write_json({ 'error': 'request %s not found' % uuid })
            return

        self.write_json({'request': wps_request})        

    def get( self, uuid: Optional[str]=None):
        """ Return status infos
        """
        key = self.get_argument('KEY', default=None)
        if key == 'request':
            self.get_wps_request(uuid)
        else:
            self.get_wps_status(uuid)

    def delete( self, uuid: Optional[str]=None ):
        """ Delete results
        """
        if uuid is None:
            self.set_status(400)
            self.write_json({ 'error': 'Missing uuid' })
            return
        try:
            success = self.application.wpsservice.delete_results(uuid)
            if not success:
                self.set_status(409) # 409 == Conflict
        except FileNotFoundError:
            self.set_status(404)

    def options(self, endpoint: Optional[str]=None) -> None:
        """ Implement OPTION for validating CORS
        """
        self.set_option_headers('GET, DELETE, OPTIONS')


