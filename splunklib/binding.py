# Copyright 2011-2012 Splunk, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"): you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""The **splunklib.binding** module provides a low-level binding interface to the 
`Splunk REST API <http://docs.splunk.com/Documentation/Splunk/latest/RESTAPI/RESTcontents>`_.

This module handles the wire details of calling the REST API, such as 
authentication tokens, prefix paths, URL encoding, and so on. Actual path 
segments, ``GET`` and ``POST`` arguments, and the parsing of responses is left 
to the user.

If you want a friendlier interface to the Splunk REST API, use the 
:mod:`splunklib.client` module.
"""

import httplib
import socket
import ssl
import urllib
import functools
import logging
from datetime import datetime
from functools import wraps

from contextlib import contextmanager

from xml.etree.ElementTree import XML

from data import record

__all__ = [
    "AuthenticationError",
    "connect",
    "Context",
    "handler",
    "HTTPError",
    "UrlEncoded"
]

# If you change these, update the docstring 
# on _authority as well.
DEFAULT_HOST = "localhost"
DEFAULT_PORT = "8089"
DEFAULT_SCHEME = "https"

def _log_duration(f):
    @wraps(f)
    def new_f(*args, **kwargs):
        start_time = datetime.now()
        val = f(*args, **kwargs)
        end_time = datetime.now()
        logging.debug("Operation took %s", end_time-start_time)
        return val
    return new_f

# Custom exceptions
class AuthenticationError(Exception):
    """Raised when a login request to Splunk fails.

    If your username was unknown or you provided an incorrect password
    in a call to Context.login() or Service.login(), this exception is
    raised.
    """
    pass

# Singleton values to eschew None
class NoAuthenticationToken(object):
    """The value stored in a Context or Service that is not logged in.

    If a Context or Service is created without an authentication token,
    and there has not yet been a call to the login method, the token
    field of the Context or Service is set to ``NoAuthenticationToken``.

    Likewise, after a Context or Service has been logged out, the token
    is set to this value again.
    """
    pass

class UrlEncoded(str):
    """This class creates URL-encoded strings.

    Manually tracking whether strings are URL encoded can be difficult. Avoid 
    calling ``urllib.quote`` to replace special characters with escapes. When 
    you receive a URL-encoded string, *do* use ``urllib.unquote`` to replace 
    escapes with single characters. Then, wrap any string you want to use as a 
    URL in ``UrlEncoded``. Note that because the ``UrlEncoded`` class is 
    idempotent, making multiple calls to it is OK.
    
    ``UrlEncoded`` objects are identical to ``str`` objects (including being 
    equal if their contents are equal) except when passed to ``UrlEncoded`` 
    again.
    
    ``UrlEncoded`` removes the ``str`` type support for interpolating values 
    with ``%`` (doing that raises a ``TypeError``). There is no reliable way to 
    encode values this way, so instead, interpolate into a string, quoting by 
    hand, and call ``UrlEncode`` with ``skip_encode=True``.

    **Example**::

        import urllib
        UrlEncoded('%s://%s' % (scheme, urllib.quote(host)), skip_encode=True)

    If you append ``str`` strings and ``UrlEncoded`` strings, the result is also
    URL encoded.

    **Example**::

        UrlEncoded('ab c') + 'de f' == UrlEncoded('ab cde f')
        'ab c' + UrlEncoded('de f') == UrlEncoded('ab cde f')
    """
    def __new__(self, val='', skip_encode=False):
        if isinstance(val, UrlEncoded):
            # Don't urllib.quote something already URL encoded.
            return val
        elif skip_encode:
            return str.__new__(self, val)
        else:
            # When subclassing str, just call str's __new__ method
            # with your class and the value you want to have in the
            # new string.
            return str.__new__(self, urllib.quote(val))

    def __add__(self, other):
        """self + other

        If *other* is not a ``UrlEncoded``, URL encode it before
        adding it.
        """
        if isinstance(other, UrlEncoded):
            return UrlEncoded(str.__add__(self, other), skip_encode=True)
        else:
            return UrlEncoded(str.__add__(self, urllib.quote(other)), skip_encode=True)

    def __radd__(self, other):
        """other + self

        If *other* is not a ``UrlEncoded``, URL _encode it before
        adding it.
        """
        if isinstance(other, UrlEncoded):
            return UrlEncoded(str.__radd__(self, other), skip_encode=True)
        else:
            return UrlEncoded(str.__add__(urllib.quote(other), self), skip_encode=True)

    def __mod__(self, fields):
        """Interpolation into ``UrlEncoded``s is disabled.

        If you try to write ``UrlEncoded("%s") % "abc", will get a
        ``TypeError``.
        """
        raise TypeError("Cannot interpolate into a UrlEncoded object.")
    def __repr__(self):
        return "UrlEncoded('%s')" % urllib.unquote(self)

@contextmanager
def _handle_auth_error(msg):
    """Handle reraising HTTP authentication errors as something clearer.

    If an ``HTTPError`` is raised with status 401 (access denied) in
    the body of this context manager, reraise it as an
    ``AuthenticationError`` instead, with *msg* as its message.

    This function adds no round trips to the server.

    :param msg: The message to be raised in ``AuthenticationError``.
    :type msg: ``str``

    **Example**::

        with _handle_auth_error("Your login failed."):
             ... # make an HTTP request
    """
    try:
        yield
    except HTTPError as he:
        if he.status == 401:
            raise AuthenticationError(msg)
        else:
            raise

def _authentication(request_fun):
    """Decorator to handle autologin and authentication errors.

    *request_fun* is a function taking no arguments that needs to
    be run with this ``Context`` logged into Splunk.

    ``_authentication``'s behavior depends on whether the
    ``autologin`` field of ``Context`` is set to ``True`` or
    ``False``. If it's ``False``, then ``_authentication``
    aborts if the ``Context`` is not logged in, and raises an
    ``AuthenticationError`` if an ``HTTPError`` of status 401 is
    raised in *request_fun*. If it's ``True``, then
    ``_authentication`` will try at all sensible places to
    log in before issuing the request.

    If ``autologin`` is ``False``, ``_authentication`` makes
    one roundtrip to the server if the ``Context`` is logged in,
    or zero if it is not. If ``autologin`` is ``True``, it's less
    deterministic, and may make at most three roundtrips (though
    that would be a truly pathological case).

    :param request_fun: A function of no arguments encapsulating
                        the request to make to the server.

    **Example**::

        import splunklib.binding as binding
        c = binding.connect(..., autologin=True)
        c.logout()
        def f():
            c.get("/services")
            return 42
        print _authentication(f)
    """
    @wraps(request_fun)
    def wrapper(self, *args, **kwargs):
        if self.token is NoAuthenticationToken:
            # Not yet logged in.
            if self.autologin and self.username and self.password:
                # This will throw an uncaught
                # AuthenticationError if it fails.
                self.login()
            else:
                raise AuthenticationError("Request aborted: not logged in.")
        try:
            # Issue the request
            return request_fun(self, *args, **kwargs)
        except HTTPError as he:
            if he.status == 401 and self.autologin:
                # Authentication failed. Try logging in, and then
                # rerunning the request. If either step fails, throw
                # an AuthenticationError and give up.
                with _handle_auth_error("Autologin failed."):
                    self.login()
                with _handle_auth_error("Autologin succeeded, but there was an auth error on next request. Something's very wrong."):
                    return request_fun()
            elif he.status == 401 and not self.autologin:
                raise AuthenticationError("Request failed: Session is not logged in.")
            else:
                raise
    return wrapper

def _authority(scheme=DEFAULT_SCHEME, host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Construct a URL authority from the given *scheme*, *host*, and *port*.

    Named in accordance with RFC2396_, which defines URLs as::

        <scheme>://<authority><path>?<query>

    .. _RFC2396: http://www.ietf.org/rfc/rfc2396.txt

    So ``https://localhost:8000/a/b/b?boris=hilda`` would be parsed as::

        scheme := https
        authority := localhost:8000
        path := /a/b/c
        query := boris=hilda

    :param scheme: URL scheme (the default is "https")
    :type scheme: "http" or "https"
    :param host: The host name (the default is "localhost")
    :type host: string
    :param port: The port number (the default is 8089)
    :type port: integer
    :return: The URL authority.
    :rtype: UrlEncoded (subclass of ``str``)

    **Example**::

        _authority() == "https://localhost:8089"

        _authority(host="splunk.utopia.net") == "https://splunk.utopia.net:8089"

        _authority(host="2001:0db8:85a3:0000:0000:8a2e:0370:7334") == \
            "https://[2001:0db8:85a3:0000:0000:8a2e:0370:7334]:8089"

        _authority(scheme="http", host="splunk.utopia.net", port="471") == \
            "http://splunk.utopia.net:471"
        
    """
    if ':' in host: 
        # IPv6 addresses must be enclosed in [ ] in order to be well
        # formed.
        host = '[' + host + ']'
    return UrlEncoded("%s://%s:%s" % (scheme, host, port), skip_encode=True)

# kwargs: sharing, owner, app
def namespace(sharing=None, owner=None, app=None, **kwargs):
    """This function constructs a Splunk namespace.

    Every Splunk resource belongs to a namespace. The namespace is specified by
    the pair of values ``owner`` and ``app`` and is governed by a ``sharing`` mode. 
    The possible values for ``sharing`` are: "user", "app", "global" and "system", 
    which map to the following combinations of ``owner`` and ``app`` values:
    
        "user"   => {owner}, {app}

        "app"    => nobody, {app}
        
        "global" => nobody, {app}
        
        "system" => nobody, system

    "nobody" is a special user name that basically means no user, and "system"
    is the name reserved for system resources.

    "-" is a wildcard that can be used for both ``owner`` and ``app`` values and
    refers to all users and all apps, respectively.

    In general, when you specify a namespace you can specify any combination of 
    these three values and the library will reconcile the triple, overriding the
    provided values as appropriate.

    Finally, if no namespacing is specified the library will make use of the
    ``/services`` branch of the REST API, which provides a namespaced view of
    Splunk resources equivelent to using ``owner={currentUser}`` and 
    ``app={defaultApp}``.

    The ``namespace`` function returns a representation of the namespace from
    reconciling the values you provide. It ignores any keyword arguments other 
    than ``owner``, ``app``, and ``sharing``, so you can provide ``dicts`` of 
    configuration information without first having to extract individual keys.

    :param sharing: The sharing mode (the default is "user").
    :type sharing: "system", "global", "app", or "user"
    :param owner: The owner context (the default is "None").
    :type owner: ``string``
    :param app: The app context (the default is "None").
    :type app: ``string``
    :returns: A :class:`splunklib.data.Record` containing the reconciled 
        namespace.

    **Example**::

        import splunklib.binding as binding
        n = binding.namespace(sharing="user", owner="boris", app="search")
        n = binding.namespace(sharing="global", app="search")
    """
    if sharing in ["system"]:
        return record({'sharing': sharing, 'owner': "nobody", 'app': "system" })
    if sharing in ["global", "app"]:
        return record({'sharing': sharing, 'owner': "nobody", 'app': app})
    if sharing in ["user", None]:
        return record({'sharing': sharing, 'owner': owner, 'app': app})
    raise ValueError("Invalid value for argument: 'sharing'")

class Context(object):
    """This class represents a context that encapsulates a splunkd connection.

    The ``Context`` class encapsulates the details of HTTP requests,
    authentication, a default namespace, and URL prefixes to simplify access to 
    the REST API.
            
    After creating a ``Context`` object, you must call its :meth:`login`
    method before you can issue requests to splunkd. Or, use the :func:`connect`
    function to create an already-authenticated ``Context`` object. You can 
    provide a session token explicitly (the same token can be shared by multiple
    ``Context`` objects) to provide authentication.

    :param host: The host name (the default is "localhost").
    :type host: ``string``
    :param port: The port number (the default is 8089).
    :type port: ``integer``
    :param scheme: The scheme for accessing the service (the default is "https").
    :type scheme: "https" or "http"
    :param sharing: The sharing mode for the namespace (the default is "user").
    :type sharing: "global", "system", "app", or "user"
    :param owner: The owner context of the namespace (optional, the default is "None").
    :type owner: ``string``
    :param app: The app context of the namespace (optional, the default is "None").
    :type app: ``string``
    :param token: A session token. When provided, you don't need to call :meth:`login`.
    :param username: The Splunk account username, which is used to 
        authenticate the Splunk instance.
    :type username: ``string``
    :param password: The password for the Splunk account.
    :type password: ``string``
    :param handler: The HTTP request handler (optional).
    :returns: A ``Context`` instance.

    **Example**::

        import splunklib.binding as binding
        c = binding.Context(username="boris", password="natasha", ...)
        c.login()
        # Or equivalently
        c = binding.connect(username="boris", password="natasha")
        # Of if you already have a session token
        c = binding.Context(token="atg232342aa34324a")
    """
    def __init__(self, handler=None, **kwargs):        
        self.http = HttpLib(handler)
        self.token = kwargs.get("token", NoAuthenticationToken)
        if self.token is None: # In case someone explicitly passes token=None
            self.token = NoAuthenticationToken
        self.scheme = kwargs.get("scheme", DEFAULT_SCHEME)
        self.host = kwargs.get("host", DEFAULT_HOST)
        self.port = int(kwargs.get("port", DEFAULT_PORT))
        self.authority = _authority(self.scheme, self.host, self.port)
        self.namespace = namespace(**kwargs)
        self.username = kwargs.get("username", "")
        self.password = kwargs.get("password", "")
        self.autologin = kwargs.get("autologin", False)

    # Shared per-context request headers
    @property
    def _auth_headers(self):
        """Headers required to authenticate a request.

        Assumes your ``Context`` already has a authentication token,
        either provided explicitly or obtained by logging into the
        Splunk instance.

        :returns: A list of 2-tuples containing key and value
        """
        # Ensure the token is properly formatted
        if self.token.startswith('Splunk'):
            token = self.token
        else:
            token = 'Splunk %s' % self.token
        return [("Authorization", token)]

    def connect(self):
        """Returns an open connection (socket) to the Splunk instance.

        This method is used for writing bulk events to an index or similar tasks
        where the overhead of opening a connection multiple times would be 
        prohibitive.

        :returns: A socket.

        **Example**::

            import splunklib.binding as binding
            c = binding.connect(...)
            socket = c.connect()
            socket.write("POST %s HTTP/1.1\\r\\n" % c._abspath("some/path/to/post/to"))
            socket.write("Host: %s:%s\\r\\n" % (c.host, c.port))
            socket.write("Accept-Encoding: identity\\r\\n")
            socket.write("Authorization: %s\\r\\n" % c.token)
            socket.write("X-Splunk-Input-Mode: Streaming\\r\\n")
            socket.write("\\r\\n")
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self.scheme == "https":
            sock = ssl.wrap_socket(sock)
        sock.connect((self.host, self.port))
        return sock

    @_authentication
    @_log_duration
    def delete(self, path_segment, owner=None, app=None, sharing=None, **query):
        """Performs a DELETE operation at the REST path segment with the given 
        namespace and query.

        This method is named to match the HTTP method. ``delete`` makes at least 
        one round trip to the server, one additional round trip for each 303 
        status returned, and at most two additional round trips if 
        the ``autologin`` field of :func:`connect` is set to ``True``.

        If *owner*, *app*, and *sharing* are omitted, this method uses the 
        default :class:`Context` namespace. All other keyword arguments are 
        included in the URL as query parameters.

        :raises AuthenticationError: Raised when the ``Context`` object is not 
             logged in.
        :raises HTTPError: Raised when an error occurred in a GET operation from
             *path_segment*.
        :param path_segment: A REST path segment.
        :type path_segment: ``string``
        :param owner: The owner context of the namespace (optional).
        :type owner: ``string``
        :param app: The app context of the namespace (optional).
        :type app: ``string``
        :param sharing: The sharing mode of the namespace (optional).
        :type sharing: ``string``
        :param query: All other keyword arguments, which are used as query 
            parameters.
        :type query: ``string``
        :return: The response from the server.
        :rtype: ``dict`` with keys ``body``, ``headers``, ``reason``, 
                and ``status``

        **Example**::

            c = binding.connect(...)
            c.delete('saved/searches/boris') == \\
                {'body': '...a response reader object...',
                 'headers': [('content-length', '1786'),
                             ('expires', 'Fri, 30 Oct 1998 00:00:00 GMT'),
                             ('server', 'Splunkd'),
                             ('connection', 'close'),
                             ('cache-control', 'no-store, max-age=0, must-revalidate, no-cache'),
                             ('date', 'Fri, 11 May 2012 16:53:06 GMT'),
                             ('content-type', 'text/xml; charset=utf-8')],
                 'reason': 'OK',
                 'status': 200}
            c.delete('nonexistant/path') # raises HTTPError
            c.logout()
            c.delete('apps/local') # raises AuthenticationError
        """
        path = self.authority + self._abspath(path_segment, owner=owner,
                                              app=app, sharing=sharing)
        logging.debug("DELETE request to %s (body: %s)", path, repr(query))
        response = self.http.delete(path, self._auth_headers, **query)
        return response

    @_authentication
    @_log_duration
    def get(self, path_segment, owner=None, app=None, sharing=None, **query):
        """Performs a GET operation from the REST path segment with the given 
        namespace and query.

        This method is named to match the HTTP method. ``get`` makes at least 
        one round trip to the server, one additional round trip for each 303 
        status returned, and at most two additional round trips if 
        the ``autologin`` field of :func:`connect` is set to ``True``.

        If *owner*, *app*, and *sharing* are omitted, this method uses the 
        default :class:`Context` namespace. All other keyword arguments are 
        included in the URL as query parameters.

        :raises AuthenticationError: Raised when the ``Context`` object is not 
             logged in.
        :raises HTTPError: Raised when an error occurred in a GET operation from
             *path_segment*.
        :param path_segment: A REST path segment.
        :type path_segment: ``string``
        :param owner: The owner context of the namespace (optional).
        :type owner: ``string``
        :param app: The app context of the namespace (optional).
        :type app: ``string``
        :param sharing: The sharing mode of the namespace (optional).
        :type sharing: ``string``
        :param query: All other keyword arguments, which are used as query 
            parameters.
        :type query: ``string``
        :return: The response from the server.
        :rtype: ``dict`` with keys ``body``, ``headers``, ``reason``, 
                and ``status``

        **Example**::

            c = binding.connect(...)
            c.get('apps/local') == \\
                {'body': '...a response reader object...',
                 'headers': [('content-length', '26208'),
                             ('expires', 'Fri, 30 Oct 1998 00:00:00 GMT'),
                             ('server', 'Splunkd'),
                             ('connection', 'close'),
                             ('cache-control', 'no-store, max-age=0, must-revalidate, no-cache'),
                             ('date', 'Fri, 11 May 2012 16:30:35 GMT'),
                             ('content-type', 'text/xml; charset=utf-8')],
                 'reason': 'OK',
                 'status': 200}
            c.get('nonexistant/path') # raises HTTPError
            c.logout()
            c.get('apps/local') # raises AuthenticationError
        """
        path = self.authority + self._abspath(path_segment, owner=owner,
                                              app=app, sharing=sharing)
        logging.debug("GET request to %s (body: %s)", path, repr(query))
        response = self.http.get(path, self._auth_headers, **query)
        return response

    @_authentication
    @_log_duration
    def post(self, path_segment, owner=None, app=None, sharing=None, headers=[], **query):
        """Performs a POST operation from the REST path segment with the given 
        namespace and query.

        This method is named to match the HTTP method. ``post`` makes at least 
        one round trip to the server, one additional round trip for each 303 
        status returned, and at most two additional round trips if 
        the ``autologin`` field of :func:`connect` is set to ``True``.

        If *owner*, *app*, and *sharing* are omitted, this method uses the 
        default :class:`Context` namespace. All other keyword arguments are 
        included in the URL as query parameters.

        Some of Splunk's endpoints, such as ``receivers/simple`` and
        ``receivers/stream``, require unstructured data in the POST body
        and all metadata passed as GET-style arguments. If you provide
        a ``body`` argument to ``post``, it will be used as the POST
        body, and all other keyword arguments will be passed as
        GET-style arguments in the URL.

        :raises AuthenticationError: Raised when the ``Context`` object is not 
             logged in.
        :raises HTTPError: Raised when an error occurred in a GET operation from
             *path_segment*.
        :param path_segment: A REST path segment.
        :type path_segment: ``string``
        :param owner: The owner context of the namespace (optional).
        :type owner: ``string``
        :param app: The app context of the namespace (optional).
        :type app: ``string``
        :param sharing: The sharing mode of the namespace (optional).
        :type sharing: ``string``
        :param query: All other keyword arguments, which are used as query 
            parameters.
        :type query: ``string``
        :return: The response from the server.
        :rtype: ``dict`` with keys ``body``, ``headers``, ``reason``, 
                and ``status``

        **Example**::

            c = binding.connect(...)
            c.post('saved/searches', name='boris', 
                   search='search * earliest=-1m | head 1') == \\
                {'body': '...a response reader object...',
                 'headers': [('content-length', '10455'),
                             ('expires', 'Fri, 30 Oct 1998 00:00:00 GMT'),
                             ('server', 'Splunkd'),
                             ('connection', 'close'),
                             ('cache-control', 'no-store, max-age=0, must-revalidate, no-cache'),
                             ('date', 'Fri, 11 May 2012 16:46:06 GMT'),
                             ('content-type', 'text/xml; charset=utf-8')],
                 'reason': 'Created',
                 'status': 201}
            c.post('nonexistant/path') # raises HTTPError
            c.logout()
            # raises AuthenticationError:
            c.post('saved/searches', name='boris', 
                   search='search * earliest=-1m | head 1')
        """
        path = self.authority + self._abspath(path_segment, owner=owner, 
                                              app=app, sharing=sharing)
        logging.debug("POST request to %s (body: %s)", path, repr(query))
        if isinstance(headers, dict):
            all_headers = [(k,v) for k,v in headers.iteritems()]
        elif isinstance(headers, list):
            all_headers = headers
        else:
            raise ValueError("headers must be a list or dict (found: %s)" % headers)
        all_headers += self._auth_headers
        response = self.http.post(path, all_headers, **query)
        return response

    @_authentication
    @_log_duration
    def request(self, path_segment, method="GET", headers=[], body="",
                owner=None, app=None, sharing=None):
        """Issues an arbitrary HTTP request to the REST path segment.
        
        This method is named to match ``httplib.request``. This function 
        makes a single round trip to the server.

        If *owner*, *app*, and *sharing* are omitted, this method uses the 
        default :class:`Context` namespace. All other keyword arguments are 
        included in the URL as query parameters.

        :raises AuthenticationError: Raised when the ``Context`` object is not 
             logged in.
        :raises HTTPError: Raised when an error occurred in a GET operation from
             *path_segment*.
        :param path_segment: A REST path segment.
        :type path_segment: ``string``
        :param owner: The owner context of the namespace (optional).
        :type owner: ``string``
        :param app: The app context of the namespace (optional).
        :type app: ``string``
        :param sharing: The sharing mode of the namespace (optional).
        :type sharing: ``string``
        :param query: All other keyword arguments, which are used as query 
            parameters.
        :type query: ``string``
        :return: The response from the server.
        :rtype: ``dict`` with keys ``body``, ``headers``, ``reason``, 
                and ``status``

        **Example**::

            c = binding.connect(...)
            c.request('saved/searches', method='GET') == \\
                {'body': '...a response reader object...',
                 'headers': [('content-length', '46722'),
                             ('expires', 'Fri, 30 Oct 1998 00:00:00 GMT'),
                             ('server', 'Splunkd'),
                             ('connection', 'close'),
                             ('cache-control', 'no-store, max-age=0, must-revalidate, no-cache'),
                             ('date', 'Fri, 11 May 2012 17:24:19 GMT'),
                             ('content-type', 'text/xml; charset=utf-8')],
                 'reason': 'OK',
                 'status': 200}
            c.request('nonexistant/path', method='GET') # raises HTTPError
            c.logout()
            c.get('apps/local') # raises AuthenticationError
        """
        path = self.authority \
            + self._abspath(path_segment, owner=owner, 
                            app=app, sharing=sharing)
        # all_headers can't be named headers, due to how
        # Python implements closures. In particular:
        # def f(x):
        #     def g():
        #         x = x + "a"
        #         return x
        #     return g()
        # throws UnboundLocalError, since x must be either a member of
        # f's local namespace or g's, and cannot switch between them
        # during the run of the function.
        all_headers = headers + self._auth_headers
        logging.debug("%s request to %s (headers: %s, body: %s)", 
                      method, path, str(all_headers), repr(body))
        response = self.http.request(path,
                                     {'method': method,
                                     'headers': all_headers,
                                     'body': body})
        return response

    def login(self):
        """Logs into the Splunk instance referred to by the :class:`Context`
        object.

        Unless a ``Context`` is created with an explicit authentication token 
        (probably obtained by logging in from a different ``Context`` object)
        you must call :meth:`login` before you can issue requests. 
        The authentication token obtained from the server is stored in the 
        ``token`` field of the ``Context`` object.

        :raises AuthenticationError: Raised when login fails.
        :returns: The ``Context`` object, so you can chain calls.

        **Example**::

            import splunklib.binding as binding
            c = binding.Context(...).login()
            # Then issue requests...
        """
        if self.token is not NoAuthenticationToken and \
                (not self.username and not self.password):
            # If we were passed a session token, but no username or
            # password, then login is a nop, since we're automatically
            # logged in.
            return
        try:
            response = self.http.post(
                self.authority + self._abspath("/services/auth/login"),
                username=self.username, 
                password=self.password)
            body = response.body.read()
            session = XML(body).findtext("./sessionKey")
            self.token = "Splunk %s" % session
            return self
        except HTTPError as he:
            if he.status == 401:
                raise AuthenticationError("Login failed.")
            else:
                raise

    def logout(self):
        """Forgets the current session token."""
        self.token = NoAuthenticationToken
        return self

    def _abspath(self, path_segment, 
                owner=None, app=None, sharing=None):
        """Qualifies *path_segment* into an absolute path for a URL.

        If *path_segment* is already absolute, returns it unchanged.
        If *path_segment* is relative, then qualifies it with either
        the provided namespace arguments or the ``Context``'s default
        namespace. Any forbidden characters in *path_segment* are URL
        encoded. This function has no network activity.

        Named to be consistent with RFC2396_.

        .. _RFC2396: http://www.ietf.org/rfc/rfc2396.txt

        :param path_segment: A relative or absolute URL path segment.
        :type path_segment: ``string``
        :param owner, app, sharing: Components of a namespace (defaults 
                                    to the ``Context``'s namespace if all 
                                    three are omitted)
        :type owner, app, sharing: ``string``
        :return: A ``UrlEncoded`` (a subclass of ``str``).
        :rtype: ``string``

        **Example**::

            import splunklib.binding as binding
            c = binding.connect(owner='boris', app='search', sharing='user')
            c._abspath('/a/b/c') == '/a/b/c'
            c._abspath('/a/b c/d') == '/a/b%20c/d'
            c._abspath('apps/local/search') == \
                '/servicesNS/boris/search/apps/local/search'
            c._abspath('apps/local/search', sharing='system') == \
                '/servicesNS/nobody/system/apps/local/search'
            url = c.authority + c._abspath('apps/local/sharing')
        """
        skip_encode = isinstance(path_segment, UrlEncoded)
        # If path_segment is absolute, escape all forbidden characters
        # in it and return it.
        if path_segment.startswith('/'):
            return UrlEncoded(path_segment, skip_encode=skip_encode)

        # path_segment is relative, so we need a namespace to build an
        # absolute path.
        if owner or app or sharing:
            ns = namespace(owner=owner, app=app, sharing=sharing)
        else:
            ns = self.namespace

        # If no app or owner are specified, then use the /services
        # endpoint. Otherwise, use /servicesNS with the specified
        # namespace. If only one of app and owner is specified, use
        # '-' for the other.
        if ns.app is None and ns.owner is None:
            return UrlEncoded("/services/%s" % path_segment, skip_encode=skip_encode)

        oname = "nobody" if ns.owner is None else ns.owner
        aname = "system" if ns.app is None else ns.app
        path = UrlEncoded("/servicesNS/%s/%s/%s" % (oname, aname, path_segment),
                          skip_encode=skip_encode)
        return path

def connect(**kwargs):
    """This function returns an authenticated :class:`Context` object.

    This function is a shorthand for calling :meth:`Context.login`.

    This function makes one round trip to the server.

    :param host: The host name (the default is "localhost").
    :type host: ``string``
    :param port: The port number (the default is 8089).
    :type port: ``integer``
    :param scheme: The scheme for accessing the service (the default is "https").
    :type scheme: "https" or "http"
    :param owner: The owner context of the namespace (the default is "None").
    :type owner: ``string``
    :param app: The app context of the namespace (the default is "None").
    :type app: ``string``
    :param sharing: The sharing mode for the namespace (the default is "user").
    :type sharing: "global", "system", "app", or "user"
    :param token: The current session token (optional). Session tokens can be 
        shared across multiple service instances.
    :type token: ``string``
    :param username: The Splunk account username, which is used to 
        authenticate the Splunk instance.
    :type username: ``string``
    :param password: The password for the Splunk account.
    :type password: ``string``
    :param autologin: When ``True``, automatically tries to log in again if the 
        session terminates.
    :type autologin: ``Boolean``
    :return: An initialized :class:`Context` instance.

    **Example**::

        import splunklib.binding as binding
        c = binding.connect(...)
        response = c.get("apps/local")
    """
    c = Context(**kwargs)
    c.login()
    return c

# Note: the error response schema supports multiple messages but we only
# return the first, although we do return the body so that an exception 
# handler that wants to read multiple messages can do so.
class HTTPError(Exception):
    """This exception is raised for HTTP responses that return an error."""
    def __init__(self, response):
        status = response.status
        reason = response.reason
        body = response.body.read()
        detail = XML(body).findtext("./messages/msg")
        message = "HTTP %d %s%s" % (
            status, reason, "" if detail is None else " -- %s" % detail)
        Exception.__init__(self, message) 
        self.status = status
        self.reason = reason
        self.headers = response.headers
        self.body = body

#
# The HTTP interface used by the Splunk binding layer abstracts the underlying
# HTTP library using request & response 'messages' which are implemented as
# dictionaries with the following structure:
#
#   # HTTP request message (only method required)
#   request {
#       method : str,
#       headers? : [(str, str)*],
#       body? : str,
#   }
#
#   # HTTP response message (all keys present)
#   response {
#       status : int,
#       reason : str,
#       headers : [(str, str)*],
#       body : file,
#   }
#

# Encode the given kwargs as a query string. This wrapper will also _encode
# a list value as a sequence of assignemnts to the corresponding arg name, 
# for example an argument such as 'foo=[1,2,3]' will be encoded as
# 'foo=1&foo=2&foo=3'. 
def _encode(**kwargs):
    items = []
    for key, value in kwargs.iteritems():
        if isinstance(value, list):
            items.extend([(key, item) for item in value])
        else:
            items.append((key, value))
    return urllib.urlencode(items)

# Crack the given url into (scheme, host, port, path)
def _spliturl(url):
    scheme, opaque = urllib.splittype(url)
    netloc, path = urllib.splithost(opaque)
    host, port = urllib.splitport(netloc)
    # Strip brackets if its an IPv6 address
    if host.startswith('[') and host.endswith(']'): host = host[1:-1]
    if port is None: port = DEFAULT_PORT
    return scheme, host, port, path

# Given an HTTP request handler, this wrapper objects provides a related
# family of convenience methods built using that handler.
class HttpLib(object):
    """A set of convenient methods for making HTTP calls.

    HttpLib provides a general ``request`` method, and ``delete``, ``post``,
    and ``get`` methods for the three HTTP methods that Splunk uses.

    By default, ``HttpLib`` will use Python's built-in ``httplib`` library,
    but you can replace it by passing your own handling function to
    ``HttpLib``'s constructor.

    The handling function should have the type::

        handler(url, request_dict) -> response_dict

    where ``url`` is the URL to make the request to (including any query and
    fragment sections), ``request_dict`` is a dictionary with the following keys:

    - method: the method for the request, typically ``'GET'``, ``'POST'``, or ``'DELETE'``.
    - headers: A list of pairs specifying the HTTP headers (e.g., ``[('key': value), ...]``)
    - body: A string giving the body to send with the request (should default to ``''``).

    and ``response_dict`` is a dictionary with the following keys:

    - status: An integer giving the HTTP status code (e.g., 200, 404).
    - reason: The reason phrase, if any, returned by the server
    - headers: A list of pairs giving the response headers (e.g., ``[('key': value), ...]``)
    - body: A stream-like object supporting ``read(size=None)`` and ``close()``
            methods to get the body of the response.

    The response dictionary will be returned directly by ``HttpLib``'s methods with
    no further processing. By default, ``HttpLib`` calls the function ``handler``
    to get a handler function. See it for an example.
    """
    def __init__(self, custom_handler=None):
        self.handler = handler() if custom_handler is None else custom_handler

    def delete(self, url, headers=None, **kwargs):
        """Send a DELETE request to *url*.

        *headers* should be a list of pairs specifying the headers for
        the HTTP response (e.g., [('Content-Type': 'text/cthulhu'), ('Token': 'boris')]).

        Any additional keyword arguments are interpreted as the query
        part of the URL. The order of keyword arguments is not preserved
        in the request, but the keywords and their arguments will be URL
        encoded.

        :returns: A dictionary describing the response (see ``HttpLib`` for its structure).
        """
        if headers is None: headers = []
        if kwargs: 
            # url is already a UrlEncoded. We have to manually declare
            # the query to be encoded or it will get automatically URL
            # encoded by being appended to url.
            url = url + UrlEncoded('?' + _encode(**kwargs), skip_encode=True)
        message = {
            'method': "DELETE",
            'headers': headers,
        }
        return self.request(url, message)

    def get(self, url, headers=None, **kwargs):
        """Issue a GET request to *url*

        *headers* should be a list of pairs specifying the headers for
        the HTTP response (e.g., [('Content-Type': 'text/cthulhu'), ('Token': 'boris')]).

        Any additional keyword arguments are interpreted as the query
        part of the URL. The order of keyword arguments is not preserved
        in the request, but the keywords and their arguments will be URL
        encoded.

        :returns: A dictionary describing the response (see ``HttpLib`` for its structure).
        """
        if headers is None: headers = []
        if kwargs: 
            # url is already a UrlEncoded. We have to manually declare
            # the query to be encoded or it will get automatically URL
            # encoded by being appended to url.
            url = url + UrlEncoded('?' + _encode(**kwargs), skip_encode=True)
        return self.request(url, { 'method': "GET", 'headers': headers })

    def post(self, url, headers=None, **kwargs):
        """Issue a POST request to *url*.

        *headers* should be a list of pairs specifying the headers for
        the HTTP response (e.g., [('Content-Type': 'text/cthulhu'), ('Token': 'boris')]).

        If ``post`` receives a keyword argument ``body``, it will use
        its value as the body for the request, and encode the rest of the
        keyword arguments into the URL's query as ``get`` or ``delete``
        does. If there is no ``body`` keyword argument, then all the keyword
        arguments are encoded into the body of the request in the
        ``x-www-form-urlencoded`` format.

        :returns: A dictionary describing the response (see ``HttpLib`` for its structure).
        """
        if headers is None: headers = []
        headers.append(("Content-Type", "application/x-www-form-urlencoded")),
        # We handle GET-style arguments and an unstructured body. This is here
        # to support the receivers/stream endpoint.
        if 'body' in kwargs:
            body = kwargs.pop('body')
            if len(kwargs) > 0:
                url = url + UrlEncoded('?' + _encode(**kwargs), skip_encode=True)
        else:
            body = _encode(**kwargs)
        message = {
            'method': "POST",
            'headers': headers,
            'body': body
        }
        return self.request(url, message)

    def request(self, url, message, **kwargs):
        """Issue an HTTP request to *url*.

        *message* should be a dictionary of the format understood
        by the HTTP handler (see ``HttpLib`` for a description of
        the format). Any additional keyword arguments are passed
        unchanged to the handler.
        """
        response = self.handler(url, message, **kwargs)
        response = record(response)
        if 400 <= response.status:
            raise HTTPError(response) 
        return response

# Converts an httplib response into a file-like object.
class ResponseReader(object):
    """This class provides a file-like interface for :class:`httplib` responses.

    The ``ResponseReader`` class is intended to be a layer to unify the different
    types of HTTP libraries used with this SDK. This class also provides a 
    preview of the stream and a few useful predicates.
    """
    # For testing, you can use a StringIO as the argument to
    # ``ResponseReader`` instead of an ``httplib.HTTPResponse``. It
    # will work equally well.
    def __init__(self, response):
        self._response = response
        self._buffer = ''

    def __str__(self):
        return self.read()

    @property
    def empty(self):
        """Indicates whether there is any more data in the response."""
        return self.peek(1) == ""

    def peek(self, size):
        """Nondestructively retrieves a given number of characters. 

        The next :meth:`read` operation behaves as though this method was never 
        called. 

        :param size: The number of characters to retrieve. 
        :type size: ``integer``
        """
        c = self.read(size)
        self._buffer = self._buffer + c
        return c

    def close(self):
        """Closes this response."""
        self._response.close()

    def read(self, size = None):
        """Reads a given number of characters from the response.
        
        :param size: The number of characters to read, or "None" to read the
            entire response.
        :type size: ``integer`` or "None"

        """
        r = self._buffer
        self._buffer = ''
        if size is not None:
            size -= len(r)
        r = r + self._response.read(size)
        return r

def handler(key_file=None, cert_file=None, timeout=None):
    """This class returns an instance of the default HTTP request handler using
    the values you provide.

    :param `key_file`: A path to a PEM (Privacy Enhanced Mail) formatted file containing your private key (optional).
    :type key_file: ``string``
    :param `cert_file`: A path to a PEM (Privacy Enhanced Mail) formatted file containing a certificate chain file (optional).
    :type cert_file: ``string``
    :param `timeout`: The request time-out period, in seconds (optional).
    :type timeout: ``integer`` or "None"
    """

    def connect(scheme, host, port):
        kwargs = {}
        if timeout is not None: kwargs['timeout'] = timeout
        if scheme == "http":
            return httplib.HTTPConnection(host, port, **kwargs)
        if scheme == "https":
            if key_file is not None: kwargs['key_file'] = key_file
            if cert_file is not None: kwargs['cert_file'] = cert_file
            return httplib.HTTPSConnection(host, port, **kwargs)
        raise ValueError("unsupported scheme: %s" % scheme)

    def request(url, message, **kwargs):
        scheme, host, port, path = _spliturl(url)
        body = message.get("body", "")
        head = { 
            "Content-Length": str(len(body)),
            "Host": host,
            "User-Agent": "splunk-sdk-python/0.1",
            "Accept": "*/*",
        } # defaults
        for key, value in message["headers"]: 
            head[key] = value
        method = message.get("method", "GET")

        connection = connect(scheme, host, port)
        try:
            connection.request(method, path, body, head)
            if timeout is not None: 
                connection.sock.settimeout(timeout)
            response = connection.getresponse()
        finally:
            connection.close()

        return {
            "status": response.status, 
            "reason": response.reason,
            "headers": response.getheaders(),
            "body": ResponseReader(response),
        }

    return request
