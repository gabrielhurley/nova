# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 OpenStack LLC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import functools
import re
import urlparse
from xml.dom import minidom

import webob

from nova import exception
from nova import flags
from nova import log as logging
from nova import quota
from nova.api.openstack import wsgi
from nova.compute import power_state as compute_power_state


LOG = logging.getLogger('nova.api.openstack.common')
FLAGS = flags.FLAGS


XML_NS_V10 = 'http://docs.rackspacecloud.com/servers/api/v1.0'
XML_NS_V11 = 'http://docs.openstack.org/compute/api/v1.1'


_STATUS_MAP = {
    None: 'BUILD',
    compute_power_state.NOSTATE: 'BUILD',
    compute_power_state.RUNNING: 'ACTIVE',
    compute_power_state.BLOCKED: 'ACTIVE',
    compute_power_state.SUSPENDED: 'SUSPENDED',
    compute_power_state.PAUSED: 'PAUSED',
    compute_power_state.SHUTDOWN: 'SHUTDOWN',
    compute_power_state.SHUTOFF: 'SHUTOFF',
    compute_power_state.CRASHED: 'ERROR',
    compute_power_state.FAILED: 'ERROR',
    compute_power_state.BUILDING: 'BUILD',
}


def status_from_power_state(power_state):
    """Map the power state to the server status string"""
    return _STATUS_MAP[power_state]


def power_states_from_status(status):
    """Map the server status string to a list of power states"""
    power_states = []
    for power_state, status_map in _STATUS_MAP.iteritems():
        # Skip the 'None' state
        if power_state is None:
            continue
        if status.lower() == status_map.lower():
            power_states.append(power_state)
    return power_states


def get_pagination_params(request):
    """Return marker, limit tuple from request.

    :param request: `wsgi.Request` possibly containing 'marker' and 'limit'
                    GET variables. 'marker' is the id of the last element
                    the client has seen, and 'limit' is the maximum number
                    of items to return. If 'limit' is not specified, 0, or
                    > max_limit, we default to max_limit. Negative values
                    for either marker or limit will cause
                    exc.HTTPBadRequest() exceptions to be raised.

    """
    params = {}
    for param in ['marker', 'limit']:
        if not param in request.GET:
            continue
        try:
            params[param] = int(request.GET[param])
        except ValueError:
            msg = _('%s param must be an integer') % param
            raise webob.exc.HTTPBadRequest(explanation=msg)
        if params[param] < 0:
            msg = _('%s param must be positive') % param
            raise webob.exc.HTTPBadRequest(explanation=msg)

    return params


def limited(items, request, max_limit=FLAGS.osapi_max_limit):
    """
    Return a slice of items according to requested offset and limit.

    @param items: A sliceable entity
    @param request: `wsgi.Request` possibly containing 'offset' and 'limit'
                    GET variables. 'offset' is where to start in the list,
                    and 'limit' is the maximum number of items to return. If
                    'limit' is not specified, 0, or > max_limit, we default
                    to max_limit. Negative values for either offset or limit
                    will cause exc.HTTPBadRequest() exceptions to be raised.
    @kwarg max_limit: The maximum number of items to return from 'items'
    """
    try:
        offset = int(request.GET.get('offset', 0))
    except ValueError:
        msg = _('offset param must be an integer')
        raise webob.exc.HTTPBadRequest(explanation=msg)

    try:
        limit = int(request.GET.get('limit', max_limit))
    except ValueError:
        msg = _('limit param must be an integer')
        raise webob.exc.HTTPBadRequest(explanation=msg)

    if limit < 0:
        msg = _('limit param must be positive')
        raise webob.exc.HTTPBadRequest(explanation=msg)

    if offset < 0:
        msg = _('offset param must be positive')
        raise webob.exc.HTTPBadRequest(explanation=msg)

    limit = min(max_limit, limit or max_limit)
    range_end = offset + limit
    return items[offset:range_end]


def limited_by_marker(items, request, max_limit=FLAGS.osapi_max_limit):
    """Return a slice of items according to the requested marker and limit."""
    params = get_pagination_params(request)

    limit = params.get('limit', max_limit)
    marker = params.get('marker')

    limit = min(max_limit, limit)
    start_index = 0
    if marker:
        start_index = -1
        for i, item in enumerate(items):
            if item['id'] == marker:
                start_index = i + 1
                break
        if start_index < 0:
            msg = _('marker [%s] not found') % marker
            raise webob.exc.HTTPBadRequest(explanation=msg)
    range_end = start_index + limit
    return items[start_index:range_end]


def get_id_from_href(href):
    """Return the id portion of a url as an int.

    Given: 'http://www.foo.com/bar/123?q=4'
    Returns: 123

    In order to support local hrefs, the href argument can be just an id:
    Given: '123'
    Returns: 123

    """
    LOG.debug(_("Attempting to treat %(href)s as an integer ID.") % locals())

    try:
        return int(href)
    except ValueError:
        pass

    LOG.debug(_("Attempting to treat %(href)s as a URL.") % locals())

    try:
        return int(urlparse.urlsplit(href).path.split('/')[-1])
    except ValueError as error:
        LOG.debug(_("Failed to parse ID from %(href)s: %(error)s") % locals())
        raise


def remove_version_from_href(href):
    """Removes the first api version from the href.

    Given: 'http://www.nova.com/v1.1/123'
    Returns: 'http://www.nova.com/123'

    Given: 'http://www.nova.com/v1.1'
    Returns: 'http://www.nova.com'

    """
    parsed_url = urlparse.urlsplit(href)
    new_path = re.sub(r'^/v[0-9]+\.[0-9]+(/|$)', r'\1', parsed_url.path,
                      count=1)

    if new_path == parsed_url.path:
        msg = _('href %s does not contain version') % href
        LOG.debug(msg)
        raise ValueError(msg)

    parsed_url = list(parsed_url)
    parsed_url[2] = new_path
    return urlparse.urlunsplit(parsed_url)


def get_version_from_href(href):
    """Returns the api version in the href.

    Returns the api version in the href.
    If no version is found, 1.0 is returned

    Given: 'http://www.nova.com/123'
    Returns: '1.0'

    Given: 'http://www.nova.com/v1.1'
    Returns: '1.1'

    """
    try:
        #finds the first instance that matches /v#.#/
        version = re.findall(r'[/][v][0-9]+\.[0-9]+[/]', href)
        #if no version was found, try finding /v#.# at the end of the string
        if not version:
            version = re.findall(r'[/][v][0-9]+\.[0-9]+$', href)
        version = re.findall(r'[0-9]+\.[0-9]', version[0])[0]
    except IndexError:
        version = '1.0'
    return version


def check_img_metadata_quota_limit(context, metadata):
    if metadata is None:
        return
    num_metadata = len(metadata)
    quota_metadata = quota.allowed_metadata_items(context, num_metadata)
    if quota_metadata < num_metadata:
        expl = _("Image metadata limit exceeded")
        raise webob.exc.HTTPRequestEntityTooLarge(explanation=expl,
                                                headers={'Retry-After': 0})


class MetadataXMLDeserializer(wsgi.XMLDeserializer):

    def extract_metadata(self, metadata_node):
        """Marshal the metadata attribute of a parsed request"""
        if metadata_node is None:
            return {}
        metadata = {}
        for meta_node in self.find_children_named(metadata_node, "meta"):
            key = meta_node.getAttribute("key")
            metadata[key] = self.extract_text(meta_node)
        return metadata

    def _extract_metadata_container(self, datastring):
        dom = minidom.parseString(datastring)
        metadata_node = self.find_first_child_named(dom, "metadata")
        metadata = self.extract_metadata(metadata_node)
        return {'body': {'metadata': metadata}}

    def create(self, datastring):
        return self._extract_metadata_container(datastring)

    def update_all(self, datastring):
        return self._extract_metadata_container(datastring)

    def update(self, datastring):
        dom = minidom.parseString(datastring)
        metadata_item = self.extract_metadata(dom)
        return {'body': {'meta': metadata_item}}


class MetadataHeadersSerializer(wsgi.ResponseHeadersSerializer):

    def delete(self, response, data):
        response.status_int = 204


class MetadataXMLSerializer(wsgi.XMLDictSerializer):
    def __init__(self, xmlns=wsgi.XMLNS_V11):
        super(MetadataXMLSerializer, self).__init__(xmlns=xmlns)

    def _meta_item_to_xml(self, doc, key, value):
        node = doc.createElement('meta')
        doc.appendChild(node)
        node.setAttribute('key', '%s' % key)
        text = doc.createTextNode('%s' % value)
        node.appendChild(text)
        return node

    def meta_list_to_xml(self, xml_doc, meta_items):
        container_node = xml_doc.createElement('metadata')
        for (key, value) in meta_items:
            item_node = self._meta_item_to_xml(xml_doc, key, value)
            container_node.appendChild(item_node)
        return container_node

    def _meta_list_to_xml_string(self, metadata_dict):
        xml_doc = minidom.Document()
        items = metadata_dict['metadata'].items()
        container_node = self.meta_list_to_xml(xml_doc, items)
        xml_doc.appendChild(container_node)
        self._add_xmlns(container_node)
        return xml_doc.toxml('UTF-8')

    def index(self, metadata_dict):
        return self._meta_list_to_xml_string(metadata_dict)

    def create(self, metadata_dict):
        return self._meta_list_to_xml_string(metadata_dict)

    def update_all(self, metadata_dict):
        return self._meta_list_to_xml_string(metadata_dict)

    def _meta_item_to_xml_string(self, meta_item_dict):
        xml_doc = minidom.Document()
        item_key, item_value = meta_item_dict.items()[0]
        item_node = self._meta_item_to_xml(xml_doc, item_key, item_value)
        xml_doc.appendChild(item_node)
        self._add_xmlns(item_node)
        return xml_doc.toxml('UTF-8')

    def show(self, meta_item_dict):
        return self._meta_item_to_xml_string(meta_item_dict['meta'])

    def update(self, meta_item_dict):
        return self._meta_item_to_xml_string(meta_item_dict['meta'])

    def default(self, *args, **kwargs):
        return ''


def check_snapshots_enabled(f):
    @functools.wraps(f)
    def inner(*args, **kwargs):
        if not FLAGS.allow_instance_snapshots:
            LOG.warn(_('Rejecting snapshot request, snapshots currently'
                       ' disabled'))
            msg = _("Instance snapshots are not permitted at this time.")
            raise webob.exc.HTTPBadRequest(explanation=msg)
        return f(*args, **kwargs)
    return inner
