#    MSRP Relay
#    Copyright (C) 2008 AG Projects
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with this program; if not, write to the Free Software Foundation, Inc.,
#    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

from copy import copy
from base64 import b64encode
from collections import deque

from application import log
from application.configuration import *
from application.configuration.datatypes import NetworkAddress, Boolean
from application.python.util import Singleton

from zope.interface import implements
from twisted.internet.defer import maybeDeferred
from twisted.internet import reactor
from twisted.internet.protocol import Factory, ClientFactory
from twisted.internet.interfaces import IPullProducer

from gnutls.constants import *
from gnutls.interfaces.twisted import X509Credentials
from gnutls.errors import GNUTLSError

from msrp.tls import Certificate, CertificateList, PrivateKey
from msrp.protocol import *
from msrp.digest import AuthChallenger, LoginFailed
from msrp.responses import *

rand_source = open("/dev/urandom")

def load_default_config():
    global RelayConfig
    class RelayConfig(ConfigSection):
        _datatypes = {"address": NetworkAddress, "allow_other_methods": Boolean, 
                      "default_ca_list": CertificateList, "default_crl_list": CertificateList}
        address = "0.0.0.0"
        default_hostname = ""
        port = 2855
        default_domain = "msrprelay.org"
        allow_other_methods = False
        session_expiration_time_minimum = 60
        session_expiration_time_default = 600
        session_expiration_time_maximum = 3600
        auth_challenge_expiration_time = 15
        default_ca_list = []
        default_crl_list = []
        default_backend = "database"
        max_auth_attempts = 3
        debug_notls = False
        log_failed_auth = False

load_default_config()
config = ConfigFile("config.ini")
config.read_settings("Relay", RelayConfig)

class DomainConfigBase(ConfigSection):
    _datatypes = {"certificate": Certificate, "key": PrivateKey}
    backend = RelayConfig.default_backend
    hostname = ""
    certificate = None
    key = None
    ca_list = RelayConfig.default_ca_list
    crl_list = RelayConfig.default_crl_list

class Relay(object):
    global config
    __metaclass__ = Singleton

    def __init__(self):
        self.unbound_sessions = {}
        self._do_init()

    def _do_init(self):
        self.listener = None
        if RelayConfig.debug_notls:
            self.factory = RelayFactoryNoTLS()
        else:
            self.factory = RelayFactory()
        self.domains = {}
        for section in config.parser.sections():
            if section.startswith("Domain:"):
                class DomainConfig(DomainConfigBase):
                    pass
                config.read_settings(section, DomainConfig)
                domain_name = section.split(":", 1)[1]
                self.domains[domain_name] = Domain(domain_name, DomainConfig)
        if RelayConfig.default_domain not in self.domains:
            log.fatal('Default domain "%s" is not defined in the configuration file' % RelayConfig.default_domain)
        self.auth_challenger = AuthChallenger(RelayConfig.auth_challenge_expiration_time)

    def _do_run(self):
        credentials = dict((id.name, id.credentials) for id in self.domains.itervalues())
        if RelayConfig.debug_notls:
            self.listener = reactor.listenTCP(RelayConfig.port, self.factory, interface=RelayConfig.address[0])
        else:
            self.listener = reactor.listenTLS(RelayConfig.port, self.factory, credentials[RelayConfig.default_domain], interface=RelayConfig.address[0], server_name_credentials=credentials)

    def run(self):
        self._do_run()
        reactor.run()
        log.debug("Started MSRP relay")

    def reload(self):
        log.debug("Reloading configuration file")
        load_default_config()
        del ConfigFile.instances["config.ini"]
        config = ConfigFile("config.ini")
        config.read_settings("Relay", RelayConfig)
        if not self.listener:
            self._do_init()
        else:
            result = self.listener.stopListening()
            result.addCallback(lambda x: self._do_init())
            result.addCallback(lambda x: self._do_run())

class Domain(object):

    def __init__(self, name, config):
        self.name = name
        if config.hostname:
            self.hostname = config.hostname
        elif RelayConfig.default_hostname:
            self.hostname = RelayConfig.default_hostname
        else:
            self.hostname = None
        self.credentials = X509Credentials(config.certificate, config.key, config.ca_list, config.crl_list)
        self.credentials.session_params.protocols = (PROTO_TLS1_1,)
        self.credentials.session_params.kx_algorithms = (KX_RSA,)
        self.credentials.session_params.ciphers = (CIPHER_AES_128_CBC,)
        self.credentials.session_params.mac_algorithms = (MAC_SHA1,)
        self.credentials.verify_peer = False
        self.backend = __import__("msrp.backend.%s" % config.backend.lower(), globals(), locals(), [""]).Checker()

    def generate_uri(self, addr):
        if self.hostname:
            return URI("%s" % self.hostname, port = RelayConfig.port, use_tls = not RelayConfig.debug_notls)
        else:
            return URI("%s" % addr.host, port = RelayConfig.port, use_tls = not RelayConfig.debug_notls)

    def log(self, log_func, msg):
        log_func("%s: %s" % (self.name, msg))

class RelayFactory(Factory):
    protocol = MSRPProtocol
    noisy = False

    def get_peer(self, protocol):
        tls_session = protocol.transport.socket
        # If server name extension is used, check if this is one of our domains.
        relay = Relay()
        server_name = tls_session.server_name
        if server_name is None:
            server_name = RelayConfig.default_domain
        else:
            if not server_name in relay.domains:
                log.error("Invalid server name extension received: %s" % server_name)
                protocol.transport.loseConnection()
                return
        domain = relay.domains[server_name]
        # Verify the peer certificate. If not present we are dealing with a client, not a relay.
        if tls_session.peer_certificate is None:
            is_relay = False
        else:
            try:
                tls_session.verify_peer()
            except GNUTLSError, e:
                protocol.transport.loseConnection()
                self.domain.log(log.error, "Error verifying certificate: %s" % str(e))
            is_relay = True
        peer = Peer(domain, is_relay, protocol = protocol)
        #peer.log(log.debug, "Received new connection")
        return peer

class RelayFactoryNoTLS(Factory):
    protocol = MSRPProtocol
    noisy = False

    def get_peer(self, protocol):
        relay = Relay()
        server_name = RelayConfig.default_domain
        domain = relay.domains[server_name]
        peer = Peer(domain, True, protocol = protocol)
        #peer.log(log.debug, "Received new connection")
        return peer

class ConnectingFactory(ClientFactory):
    protocol = MSRPProtocol
    noisy = False

    def __init__(self, peer, uri):
        self.peer = peer
        self.uri = uri

    def get_peer(self, protocol):
        if self.uri.use_tls:
            tls_session = protocol.transport.socket
            try:
                tls_session.verify_peer()
            except GNUTLSError, e:
                self.peer.log(log.error, "Error verifying certificate: %s" % str(e))
                protocol.transport.loseConnection()
                self.peer.connection_failed(e)
                return
            # Relax this requirement
            #if not tls_session.peer_certificate.has_hostname(self.uri.host):
            #    self.peer.log(log.error, "TLS certificate hostname (%s) does not match" % self.uri.host)
            #    protocol.transport.loseConnection()
            #    self.peer.connection_failed(GNUTLSError("TLS certificate hostname does not match"))
            #    return
        self.peer.got_protocol(protocol)
        return self.peer

    def clientConnectionFailed(self, connector, reason):
        self.peer.connection_failed(reason.value)

class ForwardingData(object):

    def __init__(self, msrpdata):
        self.msrpdata_received = msrpdata
        self.msrpdata_forward = None
        self.bytes_received = 0
        self.data_queue = deque()
        self.continuation = None

    def __str__(self):
        return str(self.msrpdata_received)

    def add_data(self, data):
        self.bytes_received += len(data)
        self.data_queue.append(data)

    def consume_data(self):
        if self.data_queue:
            return self.data_queue.popleft()
        else:
            return None

    @property
    def bytes_in_queue(self):
        return sum(len(data) for data in self.data_queue)

# A session always exists of two peer instances that are associated with
# eachother. 
#
# A Peer instance has an attribute state, which can be one of the following:
#
# NEW:
# A newly created incoming connection. If an AUTH is received and successfully
# processed this creates a new session and moves the Peer into the UNBOUND
# state. Alternatively, a SEND for an existing session can be received, which
# directly binds the Peer with the session and moves it into ESTABLISHED.
#
# UNBOUND:
# A newly created session which does not yet have another endpoint associated
# with it. This association can occur either through a new outgoing connection
# when the client that performed the AUTH sends a SEND message or when a new
# SEND message is received.
#
# CONNECTING:
# An attempt at a new outgoing connection. Messages can already be queued on it.
# When the protocol is connected the Peer will move into ESTABLISHED.
#
# ESTABLISHED:
# The two endpoints for the session are known and messages can be passed through
# in either direction. When the connection to this peer is lost, the Peer will
# move into DISCONNECTED, when the connection to the other peer is lost it will
# move into DISCONNECTING.
#
# DISCONNECTING:
# The connection to the other peer is lost and relay has to send REPORTs for
# queued messages that require it. After this it will disconnect itself.
#
# DISCONNECTED:
# When the peer is disconnected, which can happen at any time, inform the other
# associated peer. This is always the end state.
#
#          |                                 |
#          V                                 V
#       +-----+                        +------------+
# +-----| NEW |---------+     +--------| CONNECTING |-----+
# |     +-----+         |     |        +------------+     |
# |        |            |     |                           |
# |        V            V     V                           |
# |   +---------+   +-------------+   +---------------+   |
# |<--| UNBOUND |-->| ESTABLISHED |-->| DISCONNECTING |-->|
# |   +---------+   +-------------+   +---------------+   |
# |                        |                              |
# |                        V                              |
# |                 +--------------+                      |
# +---------------->| DISCONNECTED |<---------------------+
#                   +--------------+


class Peer(object):
    implements(IPullProducer)

    def __init__(self, domain, is_relay, session = None, protocol = None, path = None, other_peer = None):
        self.domain = domain
        self.is_relay = is_relay
        self.session = session
        self.protocol = protocol
        self.other_peer = other_peer
        self.path = path
        if self.protocol is not None:
            self.state = "NEW"
            self.auth_attempts = 0
            self.invalid_timer = reactor.callLater(30, self._cb_invalid)
        else:
            self.invalid_timer = None
            self.state = "CONNECTING"
        self.failure_reports = {}
        self.receiving = None
        # transmission attributes
        self.registered = False
        self.sending_data = False
        self.hp_queue = deque()
        self.lp_queue = deque()
        self.quenched = False
        self.relay = Relay()

    def __str__(self):
        if self.is_relay:
            name = "Relay"
        else:
            name = "Client"
        if self.session is None:
            address = self.protocol.transport.getPeer()
            return "%s %s:%d (%s)" % (name, address.host, address.port, self.state)
        else:
            return "%s session %s (%s)" % (name, self.session.session_id, self.state)

    def log(self, log_func, msg):
        self.domain.log(log_func, "%s: %s" % (str(self), msg))

    # called by MSRPProtocol

    def data_start(self, msrpdata):
        #self.log(log.debug, "Received headers for %s" % str(msrpdata))
        if self.state == "NEW":
            result = maybeDeferred(self._unbound_peer_data, msrpdata)
            result.addErrback(self._cb_catch_response, msrpdata)
        else:
            if msrpdata.method == "SEND" or (msrpdata.method != "REPORT" and RelayConfig.allow_other_methods):
                # NB: REPORT messages will not have their body forwarded, we do not support this.
                self.receiving = ForwardingData(msrpdata)
            self._bound_peer_data(msrpdata)

    def write_chunk(self, chunk):
        #self.log(log.debug, "Received %d bytes of MSRP body" % len(chunk))
        if self.state == "ESTABLISHED" and self.receiving:
            self.receiving.add_data(chunk)
            self.other_peer._quench_check()
            self.other_peer.start_sending()

    def data_end(self, continuation):
        #self.log(log.debug, "Received termination \"%s\"" % continuation)
        if self.state == "ESTABLISHED" and self.receiving:
            msrpdata = self.receiving.msrpdata_received
            if msrpdata.method == "SEND":
                if msrpdata.failure_report != "no":
                    if msrpdata.failure_report == "yes":
                        self.enqueue(ResponseOK(msrpdata).data)
                        timer = reactor.callLater(30, self._cb_send_timeout_report, self.receiving, self.receiving.msrpdata_forward.transaction_id)
                    else:
                        timer = None
                    self.failure_reports[self.receiving.msrpdata_forward.transaction_id] = (self.receiving, timer)
            self.receiving.continuation = continuation
            self.other_peer.start_sending()
            self.receiving = None

    def connection_lost(self, reason):
        self.log(log.debug, "Connection lost: %s" % reason)
        if self.invalid_timer and self.invalid_timer.active():
            self.invalid_timer.cancel()
            self.invalid_timer = None
        if self.state == "ESTABLISHED":
            self.other_peer.disconnect()
        if self.state == "UNBOUND":
            del self.relay.unbound_sessions[self.session.session_id]
        self.state = "DISCONNECTED"

    # called by ConnectingFactory

    def connection_failed(self, reason):
        self.log(log.warn, "Connection failed: %s" % reason)
        if self.state == "CONNECTING":
            self.other_peer.disconnect()

    # methods for an unbound peer

    def _cb_invalid(self):
        self.log(log.warn, "No valid MSRP message received, disconnecting")
        self.invalid_timer = None
        self.disconnect()

    def _cb_catch_response(self, failure, msrpdata):
        failure.trap(ResponseException)
        if msrpdata.method is None:
            self.log(log.warn, "Caught exception to response: %s (%s)" % (failure.value.__class__.__name__, failure.value.data.comment))
            return
        response = failure.value.data
        #self.log(log.debug, "Sending response %03d (%s)" % (response.code, response.comment))
        self.enqueue(response)

    def _unbound_peer_data(self, msrpdata):
        try:
            msrpdata.verify_headers()
        except ParsingError, e:
            if isinstance(e, HeaderParsingError) and (e.header == "To-Path" or e.header == "From-Path"):
                self.log(log.warn, "Cannot send error response, path headers unreadable")
                return
            else:
                raise ResponseUnintelligible(msrpdata, e.args[0])
        # Check if To-Path is really directed to us.
        to_path = msrpdata.headers["To-Path"].decoded
        from_path = msrpdata.headers["From-Path"].decoded
        #to_uri = URI(to_path[0].host, use_tls = to_path[0].use_tls, port = to_path[0].port)
        #if to_uri != self.domain.generate_uri(self.protocol.transport.getHost()):
        #    self.log(log.warn, "Received a wrong to_path entry for me: %s" % to_path[0])
        #    self.disconnect()
        #    return
        if len(from_path) > 1 and not self.is_relay:
            self.log(log.warn, "Client acting as a relay")
            self.disconnect()
            return
        if msrpdata.method == "AUTH" and len(to_path) == 1:
            return self._handle_auth(msrpdata)
        elif (msrpdata.method == "SEND" or (msrpdata.method is not None and msrpdata.method != "REPORT" and RelayConfig.allow_other_methods)) and len(to_path) > 1:
            session_id = to_path[0].session_id
            try:
                session = self.relay.unbound_sessions.pop(session_id)
            except KeyError:
                raise ResponseNoSession(msrpdata)
            self.log(log.debug, "Found matching unbound session %s" % session.session_id)
            self.state = "ESTABLISHED"
            self.invalid_timer.cancel()
            self.invalid_timer = None
            self.session = session
            self.path = from_path
            self.other_peer = self.session.source
            self.other_peer.got_destination(self)
            self.receiving = ForwardingData(msrpdata)
            self._bound_peer_data(msrpdata)
        else:
            raise ResponseUnknownMethod(msrpdata)

    def _handle_auth(self, msrpdata):
        auth_challenger = self.relay.auth_challenger
        if not msrpdata.headers.has_key("Authorization"):
            # If the Authorization header is not present generate challenge data and respond.
            www_authenticate = auth_challenger.generate_www_authenticate(self.domain.name, self.protocol.transport.getPeer().host)
            raise ResponseUnauthenticated(msrpdata, headers = [WWWAuthenticateHeader(www_authenticate)])
        else:
            authorization = msrpdata.headers["Authorization"].decoded
            if authorization.get("realm") != self.domain.name:
                raise ResponseUnauthorized(msrpdata, "realm does not match")
            if authorization.get("qop") != "auth":
                raise ResponseUnauthorized(msrpdata, "qop != auth")
            try:
                username = authorization["username"]
                session_id = authorization["nonce"]
            except KeyError, e:
                raise ResponseUnauthorized(msrpdata, "%s field not present in Authorization header" % e.args[0])
            if self.domain.backend.cleartext_passwords:
                result = self.domain.backend.retrieve_password(username, self.domain.name)
                func = auth_challenger.process_authorization_password
            else:
                result = self.domain.backend.retrieve_ha1(username, self.domain.name)
                func = auth_challenger.process_authorization_ha1
            result.addCallback(func, "AUTH", msrpdata.headers["To-Path"].encoded.split()[-1], self.protocol.transport.getPeer().host, **authorization)
            result.addErrback(self._eb_login_failed, msrpdata)
            result.addCallback(self._cb_login_success, msrpdata, session_id)
            return result

    def _eb_login_failed(self, failure, msrpdata):
        failure.trap(LoginFailed)
        self.auth_attempts += 1
        if RelayConfig.log_failed_auth:
            try:
                username = msrpdata.headers["Authorization"].decoded["username"]
            except IndexError:
                self.log(log.warn, "AUTH failed, no username: %s" % failure.value.args[0])
            else:
                self.log(log.warn, 'AUTH failed for username "%s": %s' % (username, failure.value.args[0]))
        if self.auth_attempts == RelayConfig.max_auth_attempts:
            self.disconnect()
        else:
            raise ResponseUnauthorized(msrpdata, "Login failed: %s" % failure.value.args[0])

    def _cb_login_success(self, authentication_info, msrpdata, session_id):
        # Check the Expires header, if present.
        if msrpdata.headers.has_key("Expires"):
            expire = msrpdata.headers["Expires"].decoded
            if expire < RelayConfig.session_expiration_time_minimum:
                raise ResponseOutOfBounds(msrpdata, headers = [MinExpiresHeader(RelayConfig.session_expiration_time_minimum)])
            if expire > RelayConfig.session_expiration_time_maximum:
                raise ResponseOutOfBounds(msrpdata, headers = [MaxExpiresHeader(RelayConfig.session_expiration_time_maximum)])
        else:
            expire = RelayConfig.session_expiration_time_default
        # We got a successful AUTH request, so add a new session
        # and reply with the the Use-Path.
        self.state = "UNBOUND"
        self.invalid_timer.cancel()
        self.invalid_timer = None
        from_path = msrpdata.headers["From-Path"].decoded
        self.path = from_path
        self.relay.unbound_sessions[session_id] = self.session = Session(self, session_id, expire)
        use_path = copy(from_path)
        use_path.pop()
        use_path = list(reversed(use_path))
        uri = self.domain.generate_uri(self.protocol.transport.getHost())
        uri.session_id = session_id
        use_path.append(uri)
        headers = [UsePathHeader(use_path), ExpiresHeader(expire), AuthenticationInfoHeader(authentication_info)]
        self.log(log.debug, "AUTH succeeded, creating new session")
        raise ResponseOK(msrpdata, headers = headers)

    # methods for a unconnected peer

    def got_protocol(self, protocol):
        self.log(log.debug, "Successfully connected")
        self.state = "ESTABLISHED"
        self.protocol = protocol
        if self.hp_queue or self.lp_queue:
            self.start_sending()

    def got_destination(self, other_peer):
        self.state = "ESTABLISHED"
        self.session.destination = other_peer
        self.other_peer = other_peer

    # methods for a bound and connected peer

    def _bound_peer_data(self, msrpdata):
        try:
            try:
                msrpdata.verify_headers()
            except ParsingError, e:
                if isinstance(e, HeaderParsingError) and (e.header == "To-Path" or e.header == "From-Path"):
                    self.log(log.error, "Cannot send error response, path headers unreadable")
                    return
                else:
                    raise ResponseUnintelligible(msrpdata, e.args[0])
            to_path = copy(msrpdata.headers["To-Path"].decoded)
            from_path = copy(msrpdata.headers["From-Path"].decoded)
            #my_uri = self.domain.generate_uri(self.protocol.transport.getHost())
            #my_uri.session_id = self.session.session_id
            #if my_uri != to_path.popleft():
            #    raise ResponseNoSession(msrpdata, "This message is not directed to me")
            to_path.popleft()
            if len(to_path) == 0 and msrpdata.method is not None:
                    raise ResponseNoSession(msrpdata, "Non-response with me as endpoint, nowhere to relay to")
            for index, uri in enumerate(from_path):
                if uri != self.path[index]:
                    raise ResponseNoSession(msrpdata, "From-Path does not match session source")
            if self.state == "UNBOUND":
                if msrpdata.method != "SEND" and (msrpdata.method is None or msrpdata.method == "REPORT" or not RelayConfig.allow_other_methods):
                    raise ResponseNoSession(msrpdata, "Non-forwarding method received on unbound session")
                self.got_destination(Peer(self.domain, len(to_path) > 1, path = to_path, session = self.session, other_peer = self))
                uri = to_path[0]
                #self.log(log.debug, "Attempting to connect to %s" % str(uri))
                factory = ConnectingFactory(self.other_peer, uri)
                if uri.use_tls:
                    self.other_peer.connector = reactor.connectTLS(uri.host, uri.port, factory, self.domain.credentials)
                else:
                    self.other_peer.connector = reactor.connectTCP(uri.host, uri.port, factory)
            else:
                for index, uri in enumerate(to_path):
                    if uri != self.other_peer.path[index]:
                        raise ResponseNoSession(msrpdata, "To-Path does not match session destination")
            if msrpdata.method == "SEND" and not msrpdata.headers.has_key("Message-ID"):
                raise ResponseUnintelligible(msrpdata, "SEND received without Message-ID")
            if msrpdata.method == "SEND" and not msrpdata.headers.has_key("Byte-Range"):
                raise ResponseUnintelligible(msrpdata, "SEND received without Byte-Range")
            if msrpdata.method is None: # we got a response
                try:
                    orig_data, timer = self.other_peer.failure_reports.pop(msrpdata.transaction_id)
                except KeyError:
                    if msrpdata.transaction_id == self.other_peer.receiving.msrpdata_forward.transaction_id and self.other_peer.receiving.msrpdata_forward.failure_report != "no":
                        orig_data, timer = self.other_peer.receiving.msrpdata_received, None
                    else:
                        orig_data, timer = None, None
                if timer is not None:
                    timer.cancel()
                if orig_data is not None and msrpdata.code != ResponseOK.code:
                    report = generate_report(msrpdata.code, orig_data.msrpdata_received, self.session.generate_transaction_id(), orig_data.bytes_received, msrpdata.comment)
                    self.other_peer.enqueue(report)
            elif msrpdata.method == "SEND" or msrpdata.method == "REPORT" or RelayConfig.allow_other_methods:
                # Do the magic of appending the first To-Path URI to the From-Path.
                to_path = copy(msrpdata.headers["To-Path"].decoded)
                from_path = copy(msrpdata.headers["From-Path"].decoded)
                from_path.appendleft(to_path.popleft())
                forward = MSRPData(self.session.generate_transaction_id(), method = msrpdata.method)
                for header in msrpdata.headers.itervalues():
                    forward.add_header(MSRPHeader(header.name, header.encoded))
                forward.add_header(ToPathHeader(to_path))
                forward.add_header(FromPathHeader(from_path))
                if forward.method == "REPORT":
                    self.other_peer.enqueue(forward)
                else:
                    self.receiving.msrpdata_forward = forward
                    self.other_peer.enqueue(self.receiving)
            else:
                raise ResponseUnknownMethod(msrpdata)
        except ResponseException, e:
            if msrpdata.method is None:
                self.log(log.debug, "Caught exception to response: %s (%s)" % (e.__class__.__name__, e.data.comment))
                return
            response = e.data
            self.enqueue(response)

    def _cb_send_timeout_report(self, data, transaction_id):
        #self.log(log.debug, "Timeout for %s" % str(data))
        del self.failure_reports[data.msrpdata_forward.transaction_id]
        report = generate_report(ResponseDownstreamTimeout.code, data.msrpdata_received, self.session.generate_transaction_id(), data.bytes_received)
        self.enqueue(report)

    def enqueue(self, msrpdata):
        #self.log(log.debug, "Enqueuing %s" % str(msrpdata))
        if isinstance(msrpdata, MSRPData):
            self.hp_queue.append(msrpdata)
        else:
            self.lp_queue.append(msrpdata)
        self._quench_check()
        self.start_sending()

    def start_sending(self):
        if self.state not in ["CONNECTING", "DISCONNECTED"] and not self.registered:
            #self.log(log.debug, "Starting transmission")
            self.registered = True
            self.protocol.transport.registerProducer(self, False)

    def _stop_sending(self):
        #self.log(log.debug, "Empty queues, halting transmission")
        self.registered = False
        self.protocol.transport.unregisterProducer()
        if self.state == "DISCONNECTING":
            self.state = "DISCONNECTED"
            self.protocol.transport.loseConnection()

    def disconnect(self):
        #self.log(log.debug, "Disconnecting when possible")
        if self.invalid_timer and self.invalid_timer.active():
            self.invalid_timer.cancel()
            self.invalid_timer = None
        if self.state == "NEW":
            self.protocol.transport.loseConnection()
            self.state = "DISCONNECTED"
        elif self.state == "UNBOUND":
            del self.relay.unbound_sessions[self.session.session_id]
            self.protocol.transport.loseConnection()
            self.state = "DISCONNECTED"
        elif self.state == "CONNECTING":
            self.connector.disconnect()
            self.state = "DISCONNECTED"
        elif self.state == "ESTABLISHED":
            if self.receiving and self.receiving.msrpdata_received.failure_report != "no":
                report = generate_report(ResponseDownstreamTimeout.code, self.receiving.msrpdata_received, self.session.generate_transaction_id(), self.receiving.bytes_received, "Session got disconnected")
                self.enqueue(report)
            for orig_data, timer in self.failure_reports.itervalues():
                if timer is not None:
                    timer.cancel()
                report = generate_report(ResponseDownstreamTimeout.code, orig_data.msrpdata_received, self.session.generate_transaction_id(), orig_data.bytes_received, "Session got disconnected")
                self.enqueue(report)
            if not self.registered:
                self.protocol.transport.loseConnection()
                self.state = "DISCONNECTED"
            else:
                self.state = "DISCONNECTING"

    @property
    def _data_bytes(self):
        return sum(data.bytes_in_queue for data in self.lp_queue)

    @property
    def _message_count(self):
        return len(self.hp_queue) + len(self.lp_queue)

    def _quench_check(self):
        if self.state == "ESTABLISHED" and self.other_peer.state == "ESTABLISHED" and not self.other_peer.quenched:
            if self._data_bytes > 1024 * 1024 or self._message_count > 50:
                self.other_peer.quench()

    def _unquench_check(self):
        if self.state == "ESTABLISHED" and self.other_peer.state == "ESTABLISHED" and self.other_peer.quenched:
            if self._data_bytes <= 1024 * 1024 and self._message_count <= 50:
                self.other_peer.unquench()

    def quench(self):
        #self.log(log.debug, "Quenching")
        self.quenched = True
        self.protocol.transport.stopReading()

    def unquench(self):
        #self.log(log.debug, "Unquenching")
        self.quenched = False
        self.protocol.transport.startReading()

    # methods implemented for IPullProducer

    def resumeProducing(self):
        if self.hp_queue:
            if self.sending_data:
                #self.log(log.debug, "Sending high priorty message, aborting SEND")
                data = self.lp_queue[0]
                self.protocol.transport.write(data.msrpdata_forward.encode_end("+"))
                data.msrpdata_forward.transaction_id = self.session.generate_transaction_id()
                # TODO: add an entry to failure_report for the new transaction-id
                byterange = data.msrpdata_forward.headers["Byte-Range"].decoded
                byterange[0] = data.pos
                data.msrpdata_forward.headers["Byte-Range"].decoded = byterange
                self.sending_data = False
            else:
                #self.log(log.debug, "Sending high priority message")
                self.protocol.transport.write(self.hp_queue.popleft().encode())
            self._unquench_check()
        elif self.sending_data:
            data = self.lp_queue[0]
            if data.continuation is not None and len(data.data_queue) <= 1:
                #self.log(log.debug, "Sending end of SEND message")
                self.lp_queue.popleft()
                if data.data_queue:
                    self.protocol.transport.write(data.data_queue.popleft())
                self.protocol.transport.write(data.msrpdata_forward.encode_end(data.continuation))
                self.sending_data = False
                self._unquench_check()
            else:
                try:
                    chunk = data.data_queue.popleft()
                except IndexError:
                    self._stop_sending()
                else:
                    #self.log(log.debug, "Sending data for SEND message")
                    self.protocol.transport.write(chunk)
                    data.pos += len(chunk)
                    self._unquench_check()
        elif self.lp_queue:
            #self.log(log.debug, "Sending headers for SEND message")
            data = self.lp_queue[0]
            self.sending_data = True
            data.pos = data.msrpdata_forward.headers["Byte-Range"].decoded[0]
            self.protocol.transport.write(data.msrpdata_forward.encode_start())
        else:
            self._stop_sending()

    def stopProducing(self):
        pass

class Session(object):

    def __init__(self, source, session_id, expire):
        self.source = source
        self.destination = None
        self.session_id = session_id
        self.expire = expire

    def generate_transaction_id(self):
        while True:
            transaction_id = b64encode(rand_source.read(18), "+-")
            if transaction_id not in self.source.failure_reports and not (self.destination and transaction_id in self.destination.failure_reports):
                return transaction_id