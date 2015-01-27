import logging
import re

from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.http.response import HttpResponse
from django.utils.translation import ugettext as _
from rest_framework import exceptions
from rest_framework import authentication

from oscarapi.utils import (
    get_domain,
    session_id_from_parsed_session_uri,
    get_session
)
from oscarapi import models

logger = logging.getLogger(__name__)

HTTP_SESSION_ID_REGEX = re.compile(
    r'^SID:(?P<type>(?:ANON|AUTH)):(?P<realm>.*?):(?P<session_id>.+?)(?:[-:][0-9a-fA-F]+){0,2}$')


def parse_session_id(request):
    """
    Parse a session id from the request.
    
    >>> class request:
    ...      META = {'HTTP_SESSION_ID': None}
    >>>
    >>> request.META['HTTP_SESSION_ID'] = 'SID:ANON:example.com:987171879'
    >>> sorted(parse_session_id(request).items())
    [('realm', 'example.com'), ('session_id', '987171879'), ('type', 'ANON')]
    >>>
    >>> request.META['HTTP_SESSION_ID'] = 'SID:AUTH:example.com:987171879'
    >>> sorted(parse_session_id(request).items())
    [('realm', 'example.com'), ('session_id', '987171879'), ('type', 'AUTH')]
    >>>
    >>> request.META['HTTP_SESSION_ID'] = 'SID:ANON:example.com:987171879-16EF'
    >>> sorted(parse_session_id(request).items())
    [('realm', 'example.com'), ('session_id', '987171879'), ('type', 'ANON')]
    >>>
    >>> request.META['HTTP_SESSION_ID'] = 'SID:ANON:example.com:98717-16EF:100'
    >>> sorted(parse_session_id(request).items())
    [('realm', 'example.com'), ('session_id', '98717'), ('type', 'ANON')]
    >>>
    >>> request.META['HTTP_SESSION_ID'] = 'SID:ANON::987171879'
    >>> sorted(parse_session_id(request).items())
    [('realm', ''), ('session_id', '987171879'), ('type', 'ANON')]
    >>>
    >>> request.META['HTTP_SESSION_ID'] = 'SID:ANON:example.com:923-thread1'
    >>> sorted(parse_session_id(request).items())
    [('realm', 'example.com'), ('session_id', '923-thread1'), ('type', 'ANON')]
    >>>
    >>> request.META['HTTP_SESSION_ID'] = 'SID:BULLSHIT:example.com:987171879'
    >>> parse_session_id(request)
    
    >>> request.META['HTTP_SESSION_ID'] = 'ENTIREGABRBAGE'
    >>> parse_session_id(request)
    
    >>> request.META['HTTP_SESSION_ID'] = 'SID:ANON:987171879'
    >>> parse_session_id(request)
    
    """
    unparsed_session_id = request.META.get('HTTP_SESSION_ID', None)
    if unparsed_session_id is not None:
        parsed_session_id = HTTP_SESSION_ID_REGEX.match(unparsed_session_id)
        if parsed_session_id is not None:
            return parsed_session_id.groupdict()

    return None


def start_or_resume(session_id, session_type):
    if session_type == 'ANON':
        return get_session(session_id, raise_on_create=False)

    return get_session(session_id, raise_on_create=True)


def is_api_request(request):
    path = request.path.lower()
    api_root = reverse('api-root').lower()
    return path.startswith(api_root)


class HeaderSessionMiddleware(SessionMiddleware):
    """
    Implement session through headers:

    http://www.w3.org/TR/WD-session-id

    TODO:
    Implement gateway protection, with permission options for usage of
    header sessions. With that in place the api can be used for both trusted
    and non trusted clients, see README.rst.
    """

    def process_request(self, request):
        """
        Parse the session id from the 'Session-Id: ' header when using the api.
        """
        if is_api_request(request):
            try:
                parsed_session_uri = parse_session_id(request)
                if parsed_session_uri is not None:
                    domain = get_domain(request)
                    if parsed_session_uri['realm'] != domain:
                        raise exceptions.NotAcceptable(
                            _('Can not accept cookie with realm %s on realm %s') % (
                                parsed_session_uri['realm'],
                                domain
                            )
                        )
                    session_id = session_id_from_parsed_session_uri(
                        parsed_session_uri)
                    request.session = start_or_resume(
                        session_id, session_type=parsed_session_uri['type'])
                    request.parsed_session_uri = parsed_session_uri

                    # since the session id is assigned by the CLIENT, there is
                    # no point in having csrf_protection. Session id's read
                    # from cookies, still need csrf!
                    request.csrf_processing_done = True
                    return None
            except exceptions.APIException as e:
                response = HttpResponse('{"reason": "%s"}' % e.detail,
                                        content_type='application/json')
                response.status_code = e.status_code
                return response

        return super(HeaderSessionMiddleware, self).process_request(request)

    def process_response(self, request, response):
        """
        Add the 'Session-Id: ' header when using the api.
        """
        if is_api_request(request) \
                and getattr(request, 'session', None) is not None \
                and hasattr(request, 'parsed_session_uri'):
            session_key = request.session.session_key
            parsed_session_key = session_id_from_parsed_session_uri(
                request.parsed_session_uri)
            assert(session_key == parsed_session_key), \
                    '%s is not equal to %s' % (session_key, parsed_session_key)
            response['Session-Id'] = \
                'SID:%(type)s:%(realm)s:%(session_id)s' % (
                    request.parsed_session_uri)

        return super(HeaderSessionMiddleware, self).process_response(
            request, response)


class ApiGatewayMiddleWare(object):
    """
    Protect the api gateway with token.
    """
    def process_request(self, request):
        if is_api_request(request):
            key = authentication.get_authorization_header(request)
            if models.ApiKey.objects.filter(key=key).exists():
                return None

            logger.error('No credentials provided')
            raise PermissionDenied()

        return None
