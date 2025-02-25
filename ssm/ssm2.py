'''
   Copyright (C) 2012 STFC

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.

   @author: Will Rogers
'''

# It's possible for SSM to be used without SSL, and the ssl module isn't in the
# standard library until 2.6, so this makes it safe for earlier Python versions.
try:
    import ssl
except ImportError:
    # ImportError is raised later on if SSL is actually requested.
    ssl = None

from ssm import crypto
from ssm.message_directory import MessageDirectory

try:
    from dirq.QueueSimple import QueueSimple
    from dirq.queue import Queue
except ImportError:
    # ImportError is raised later on if dirq is requested but not installed.
    QueueSimple = None
    Queue = None

import stomp
from stomp.exception import ConnectFailedException

import os
import socket
import time
import logging

from argo_ams_library import ArgoMessagingService, AmsMessage

# Set up logging
log = logging.getLogger(__name__)


class Ssm2Exception(Exception):
    '''
    Exception for use by SSM2.
    '''
    pass


class Ssm2(stomp.ConnectionListener):
    '''
    Minimal SSM implementation.
    '''
    # Schema for the dirq message queue.
    QSCHEMA = {'body': 'string', 'signer':'string', 'empaid':'string?'}
    REJECT_SCHEMA = {'body': 'string', 'signer':'string?', 'empaid':'string?', 'error':'string'}
    CONNECTION_TIMEOUT = 10

    # Messaging protocols
    STOMP_MESSAGING = 'STOMP'
    AMS_MESSAGING = 'AMS'

    def __init__(self, hosts_and_ports, qpath, cert, key, dest=None, listen=None, 
                 capath=None, check_crls=False, use_ssl=False, username=None, password=None, 
                 enc_cert=None, verify_enc_cert=True, pidfile=None, path_type='dirq',
                 protocol=STOMP_MESSAGING, project=None, token=''):
        '''
        Creates an SSM2 object.  If a listen value is supplied,
        this SSM2 will be a receiver.
        '''
        self._conn = None
        self._last_msg = None

        self._brokers = hosts_and_ports
        self._cert = cert
        self._key = key
        self._enc_cert = enc_cert
        self._capath = capath
        self._check_crls = check_crls
        self._user = username
        self._pwd = password
        self._use_ssl = use_ssl
        # use pwd auth if we're supplied both user and pwd
        self._use_pwd = username is not None and password is not None
        self.connected = False

        self._listen = listen
        self._dest = dest

        self._valid_dns = []
        self._pidfile = pidfile

        # Used to differentiate between STOMP and AMS methods
        self._protocol = protocol

        # Used when interacting with an Argo Messaging Service
        self._project = project
        self._token = token

        if self._protocol == Ssm2.AMS_MESSAGING:
            self._ams = ArgoMessagingService(endpoint=self._brokers[0],
                                             token=self._token,
                                             cert=self._cert,
                                             key=self._key,
                                             project=self._project)

        # create the filesystem queues for accepted and rejected messages
        if dest is not None and listen is None:
            # Determine what sort of outgoing structure to make
            if path_type == 'dirq':
                if QueueSimple is None:
                    raise ImportError("dirq path_type requested but the dirq "
                                      "module wasn't found.")

                self._outq = QueueSimple(qpath)

            elif path_type == 'directory':
                self._outq = MessageDirectory(qpath)
            else:
                raise Ssm2Exception('Unsupported path_type variable.')

        elif listen is not None:
            inqpath = os.path.join(qpath, 'incoming')
            rejectqpath = os.path.join(qpath, 'reject')

            # Receivers must use the dirq module, so make a quick sanity check
            # that dirq is installed.
            if Queue is None:
                raise ImportError("Receiving SSMs must use dirq, but the dirq "
                                  "module wasn't found.")

            self._inq = Queue(inqpath, schema=Ssm2.QSCHEMA)
            self._rejectq = Queue(rejectqpath, schema=Ssm2.REJECT_SCHEMA)
        else:
            raise Ssm2Exception('SSM must be either producer or consumer.')
        # check that the cert and key match
        if not crypto.check_cert_key(self._cert, self._key):
            raise Ssm2Exception('Cert and key don\'t match.')

        # Check that the certificate has not expired.
        if not crypto.verify_cert_date(self._cert):
            raise Ssm2Exception('Certificate %s has expired or will expire '
                                'within a day.' % self._cert)

        # check the server certificate provided
        if enc_cert is not None:
            log.info('Messages will be encrypted using %s', enc_cert)
            if not os.path.isfile(self._enc_cert):
                raise Ssm2Exception('Specified certificate file does not exist: %s.' % self._enc_cert)
            # Check that the encyption certificate has not expired.
            if not crypto.verify_cert_date(enc_cert):
                raise Ssm2Exception(
                    'Encryption certificate %s has expired or will expire '
                    'within a day. Please obtain the new one from the final '
                    'server receiving your messages.' % enc_cert
                )
            if verify_enc_cert:
                if not crypto.verify_cert_path(self._enc_cert, self._capath, self._check_crls):
                    raise Ssm2Exception('Failed to verify server certificate %s against CA path %s.'
                                        % (self._enc_cert, self._capath))

        # If the overall SSM log level is info, we want to only
        # see log entries from stomp.py at the warning level and above.
        if logging.getLogger("ssm.ssm2").getEffectiveLevel() == logging.INFO:
            logging.getLogger("stomp.py").setLevel(logging.WARNING)
        # If the overall SSM log level is debug, we want to only
        # see log entries from stomp.py at the info level and above.
        elif logging.getLogger("ssm.ssm2").getEffectiveLevel() == logging.DEBUG:
            logging.getLogger("stomp.py").setLevel(logging.INFO)

    def set_dns(self, dn_list):
        '''
        Set the list of DNs which are allowed to sign incoming messages.
        '''
        self._valid_dns = dn_list

    ##########################################################################
    # Methods called by stomppy
    ##########################################################################

    def on_send(self, frame, unused_body=None):
        '''
        Called by stomppy when a message is sent.

        unused_body is only present to have a backward compatible
        method signature when using stomp.py v3.1.X
        '''
        try:
            # Try the stomp.py v4 way first
            empaid = frame.headers['empa-id']
        except KeyError:
            # Then you are most likely using stomp.py v4.
            # on_send is now triggered on non message frames
            # (such as 'CONNECT' frames) and as such without an empa-id.
            empaid = 'no empa-id'
        except AttributeError:
            # Then you are likely using stomp.py v3
            empaid = frame['empa-id']

        log.debug('Sent message: %s', empaid)

    def on_message(self, headers, body):
        '''
        Called by stomppy when a message is received.

        Handle the message according to its content and headers.
        '''
        try:
            empaid = headers['empa-id']
            if empaid == 'ping': # ignore ping message
                log.info('Received ping message.')
                return
        except KeyError:
            empaid = 'noid'

        log.info("Received message. ID = %s", empaid)
        extracted_msg, signer, err_msg = self._handle_msg(body)

        try:
            # If the message is empty or the error message is not empty
            # then reject the message.
            if extracted_msg is None or err_msg is not None:
                if signer is None:  # crypto failed
                    signer = 'Not available.'
                elif extracted_msg is not None:
                    # If there is a signer then it was rejected for not being
                    # in the DNs list, so we can use the extracted msg, which
                    # allows the msg to be reloaded if needed.
                    body = extracted_msg

                log.warn("Message rejected: %s", err_msg)

                name = self._rejectq.add({'body': body,
                                          'signer': signer,
                                          'empaid': empaid,
                                          'error': err_msg})
                log.info("Message saved to reject queue as %s", name)

            else:  # message verified ok
                name = self._inq.add({'body': extracted_msg,
                                      'signer': signer,
                                      'empaid': empaid})
                log.info("Message saved to incoming queue as %s", name)

        except (IOError, OSError) as e:
            log.error('Failed to read or write file: %s', e)

    def on_error(self, headers, body):
        '''
        Called by stomppy when an error frame is received.
        '''
        if 'No user for client certificate: ' in headers['message']:
            log.error('The following certificate is not authorised: %s',
                      headers['message'].split(':')[1])
        else:
            log.error('Error message received: %s', body)

    def on_connected(self, unused_headers, unused_body):
        '''
        Called by stomppy when a connection is established.

        Track the connection.
        '''
        self.connected = True
        log.info('Connected.')

    def on_disconnected(self):
        '''
        Called by stomppy when disconnected from the broker.
        '''
        log.info('Disconnected from broker.')
        self.connected = False

    def on_receipt(self, headers, unused_body):
        '''
        Called by stomppy when the broker acknowledges receipt of a message.
        '''
        log.info('Broker received message: %s', headers['receipt-id'])
        self._last_msg = headers['receipt-id']

    def on_receiver_loop_completed(self, _unused_headers, _unused_body):
        """
        Called by stompy when the receiver loop ends.

        This is usually trigger as part of a disconnect.
        """
        log.debug('on_receiver_loop_completed called.')

    ##########################################################################
    # Message handling methods
    ##########################################################################

    def _handle_msg(self, text):
        '''
        Deal with the raw message contents appropriately:
        - decrypt if necessary
        - verify signature
        Return plain-text message, signer's DN and an error/None.
        '''
        if text is None or text == '':
            warning = 'Empty text passed to _handle_msg.'
            log.warn(warning)
            return None, None, warning
#        if not text.startswith('MIME-Version: 1.0'):
#            raise Ssm2Exception('Not a valid message.')

        # encrypted - this could be nicer
        if 'application/pkcs7-mime' in text or 'application/x-pkcs7-mime' in text:
            try:
                text = crypto.decrypt(text, self._cert, self._key)
            except crypto.CryptoException, e:
                error = 'Failed to decrypt message: %s' % e
                log.error(error)
                return None, None, error

        # always signed
        try:
            message, signer = crypto.verify(text, self._capath, self._check_crls)
        except crypto.CryptoException, e:
            error = 'Failed to verify message: %s' % e
            log.error(error)
            return None, None, error

        if signer not in self._valid_dns:
            warning = 'Signer not in valid DNs list: %s' % signer
            log.warn(warning)
            return None, signer, warning
        else:
            log.info('Valid signer: %s', signer)

        return message, signer, None

    def _send_msg(self, message, msgid):
        '''
        Send one message using stomppy.  The message will be signed using
        the host cert and key.  If an encryption certificate
        has been supplied, the message will also be encrypted.
        '''
        log.info('Sending message: %s', msgid)
        headers = {'destination': self._dest, 'receipt': msgid,
                   'empa-id': msgid}

        if message is not None:
            to_send = crypto.sign(message, self._cert, self._key)
            if self._enc_cert is not None:
                to_send = crypto.encrypt(to_send, self._enc_cert)
        else:
            to_send = ''

        try:
            # Try using the v4 method signiture
            self._conn.send(self._dest, to_send, headers=headers)
        except TypeError:
            # If it fails, use the v3 metod signiture
            self._conn.send(to_send, headers=headers)

    def pull_msg_ams(self):
        """Pull 1 message from the AMS and acknowledge it."""
        if self._protocol != Ssm2.AMS_MESSAGING:
            # Then this method should not be called,
            # raise an exception if it is.
            raise Ssm2Exception('pull_msg_ams called, '
                                'but protocol not set to AMS. '
                                'Protocol: %s' % self._protocol)

        # This method is setup so that you could pull down and
        # acknowledge more than one message at a time, but
        # currently there is no use case for it.
        messages_to_pull = 1
        # ack id's will be stored in this list and then acknowledged
        ackids = []

        for msg_ack_id, msg in self._ams.pull_sub(self._listen,
                                                  messages_to_pull):
            # Get the AMS message id
            msgid = msg.get_msgid()
            # Get the SSM dirq id
            try:
                empaid = msg.get_attr().get('empaid')
            except AttributeError:
                # A message without an empaid could be received if it wasn't
                # sent via the SSM, we need to pull down that message
                # to prevent it blocking the message queue.
                log.debug("Message %s has no empaid.", msgid)
                empaid = "N/A"
            # get the message body
            body = msg.get_data()

            log.info('Received message. ID = %s, Argo ID = %s', empaid, msgid)

            extracted_msg, signer, err_msg = self._handle_msg(body)

            try:
                # If the message is empty or the error message is not empty
                # then reject the message.
                if extracted_msg is None or err_msg is not None:
                    if signer is None:  # crypto failed
                        signer = 'Not available.'
                    elif extracted_msg is not None:
                        # If there is a signer then it was rejected for not
                        # being in the DNs list, so we can use the
                        # extracted msg, which allows the msg to be
                        # reloaded if needed.
                        body = extracted_msg

                    log.warn("Message rejected: %s", err_msg)

                    name = self._rejectq.add({'body': body,
                                              'signer': signer,
                                              'empaid': empaid,
                                              'error': err_msg})
                    log.info("Message saved to reject queue as %s", name)

                else:  # message verified ok
                    name = self._inq.add({'body': extracted_msg,
                                          'signer': signer,
                                          'empaid': empaid})
                    log.info("Message saved to incoming queue as %s", name)

                # If we get here, we have saved the message, so add the
                # ack ID to the list of those to be acknowledged.
                ackids.append(msg_ack_id)

            except OSError, error:
                log.error('Failed to read or write file: %s', error)

        # pass list of extracted ackIds to AMS Service so that
        # it can move the offset for the next subscription pull
        # (basically acknowledging pulled messages)
        if ackids:
            self._ams.ack_sub(self._listen, ackids)

    def send_ping(self):
        '''
        If a STOMP connection is left open with no activity for an hour or
        so, it stops responding. Stomppy 3.1.3 has two ways of handling
        this, but stomppy 3.0.3 (EPEL 5 and 6) has neither.
        To get around this, we begin and then abort a STOMP transaction to
        keep the connection active.
        '''
        # Use time as transaction id to ensure uniqueness within each connection
        transaction_id = str(time.time())

        self._conn.begin({'transaction': transaction_id})
        self._conn.abort({'transaction': transaction_id})

    def has_msgs(self):
        '''
        Return True if there are any messages in the outgoing queue.
        '''
        return self._outq.count() > 0

    def send_all(self):
        '''
        Send all the messages in the outgoing queue.

        Either via STOMP or HTTPS (to an Argo Message Broker).
        '''
        log.info('Found %s messages.', self._outq.count())
        for msgid in self._outq:
            if not self._outq.lock(msgid):
                log.warn('Message was locked. %s will not be sent.', msgid)
                continue

            text = self._outq.get(msgid)

            if self._protocol == Ssm2.STOMP_MESSAGING:
                # Then we are sending to a STOMP message broker.
                self._send_msg(text, msgid)

                log.info('Waiting for broker to accept message.')
                while self._last_msg is None:
                    if not self.connected:
                        raise Ssm2Exception('Lost connection.')

                log_string = "Sent %s" % msgid

            elif self._protocol == Ssm2.AMS_MESSAGING:
                # Then we are sending to an Argo Messaging Service instance.
                if text is not None:
                    # First we sign the message
                    to_send = crypto.sign(text, self._cert, self._key)
                    # Possibly encrypt the message.
                    if self._enc_cert is not None:
                        to_send = crypto.encrypt(to_send, self._enc_cert)

                    # Then we need to wrap text up as an AMS Message.
                    message = AmsMessage(data=to_send,
                                         attributes={'empaid': msgid}).dict()

                    argo_response = self._ams.publish(self._dest, message)

                    argo_id = argo_response['messageIds'][0]
                    log_string = "Sent %s, Argo ID: %s" % (msgid, argo_id)

            else:
                # The SSM has been improperly configured
                raise Ssm2Exception('Unknown messaging protocol: %s' %
                                    self._protocol)

            time.sleep(0.1)
            # log that the message was sent
            log.info(log_string)

            self._last_msg = None
            self._outq.remove(msgid)

        log.info('Tidying message directory.')
        try:
            # Remove empty dirs and unlock msgs older than 5 min (default)
            self._outq.purge()
        except OSError, e:
            log.warn('OSError raised while purging message queue: %s', e)

    ############################################################################
    # Connection handling methods
    ############################################################################

    def _initialise_connection(self, host, port):
        '''
        Create the self._connection object with the appropriate properties,
        but don't try to start the connection.
        '''
        log.info("Established connection to %s, port %i", host, port)
        if self._use_ssl:
            if ssl is None:
                raise ImportError("SSL connection requested but the ssl module "
                                  "wasn't found.")
            log.info('Connecting using SSL...')
        else:
            log.warning("SSL connection not requested, your messages may be "
                        "intercepted.")

        # _conn will use the default SSL version specified by stomp.py
        self._conn = stomp.Connection([(host, port)],
                                      use_ssl=self._use_ssl,
                                      ssl_key_file=self._key,
                                      ssl_cert_file=self._cert,
                                      timeout=Ssm2.CONNECTION_TIMEOUT)

        self._conn.set_listener('SSM', self)

    def handle_connect(self):
        '''
        Assuming that the SSM has retrieved the details of the broker or
        brokers it wants to connect to, connect to one.

        If more than one is in the list self._network_brokers, try to
        connect to each in turn until successful.
        '''
        if self._protocol == Ssm2.AMS_MESSAGING:
            log.debug('handle_connect called for AMS, doing nothing.')
            return

        log.info("Using stomp.py version %s.%s.%s.", *stomp.__version__)

        for host, port in self._brokers:
            self._initialise_connection(host, port)
            try:
                self.start_connection()
                break
            except ConnectFailedException, e:
                # ConnectFailedException doesn't provide a message.
                log.warn('Failed to connect to %s:%s.', host, port)
            except Ssm2Exception, e:
                log.warn('Failed to connect to %s:%s: %s', host, port, e)

        if not self.connected:
            raise Ssm2Exception('Attempts to start the SSM failed.  The system will exit.')

    def handle_disconnect(self):
        '''
        When disconnected, attempt to reconnect using the same method as used
        when starting up.
        '''
        if self._protocol == Ssm2.AMS_MESSAGING:
            log.debug('handle_disconnect called for AMS, doing nothing.')
            return

        self.connected = False
        # Shut down properly
        self.close_connection()

        # Sometimes the SSM will reconnect to the broker before it's properly
        # shut down!  This prevents that.
        time.sleep(2)

        # Try again according to the same logic as the initial startup
        try:
            self.handle_connect()
        except Ssm2Exception:
            self.connected = False

        # If reconnection fails, admit defeat.
        if not self.connected:
            err_msg = 'Reconnection attempts failed and have been abandoned.'
            raise Ssm2Exception(err_msg)

    def start_connection(self):
        '''
        Once self._connection exists, attempt to start it and subscribe
        to the relevant topics.

        If the timeout is reached without receiving confirmation of
        connection, raise an exception.
        '''
        if self._protocol == Ssm2.AMS_MESSAGING:
            log.debug('start_connection called for AMS, doing nothing.')
            return

        if self._conn is None:
            raise Ssm2Exception('Called start_connection() before a \
                    connection object was initialised.')

        self._conn.start()
        self._conn.connect(wait=False)

        i = 0
        while not self.connected:
            time.sleep(0.1)
            if i > Ssm2.CONNECTION_TIMEOUT * 10:
                err = 'Timed out while waiting for connection. '
                err += 'Check the connection details.'
                raise Ssm2Exception(err)
            i += 1

        if self._dest is not None:
            log.info('Will send messages to: %s', self._dest)

        if self._listen is not None:
            # Use a static ID for the subscription ID because we only ever have
            # one subscription within a connection and ID is only considered
            # to differentiate subscriptions within a connection.
            self._conn.subscribe(destination=self._listen, id=1, ack='auto')
            log.info('Subscribing to: %s', self._listen)

    def close_connection(self):
        '''
        Close the connection.  This is important because it runs
        in a separate thread, so it can outlive the main process
        if it is not ended.
        '''
        if self._protocol == Ssm2.AMS_MESSAGING:
            log.debug('close_connection called for AMS, doing nothing.')
            return

        try:
            self._conn.disconnect()
        except (stomp.exception.NotConnectedException, socket.error):
            self._conn = None
        except AttributeError:
            # AttributeError if self._connection is None already
            pass

        log.info('SSM connection ended.')

    def startup(self):
        '''
        Create the pidfile then start the connection.
        '''
        if self._pidfile is not None:
            try:
                f = open(self._pidfile, 'w')
                f.write(str(os.getpid()))
                f.write('\n')
                f.close()
            except IOError, e:
                log.warn('Failed to create pidfile %s: %s', self._pidfile, e)

        self.handle_connect()

    def shutdown(self):
        '''
        Close the connection then remove the pidfile.
        '''
        self.close_connection()
        if self._pidfile is not None:
            try:
                if os.path.exists(self._pidfile):
                    os.remove(self._pidfile)
                else:
                    log.warn('pidfile %s not found.', self._pidfile)
            except IOError, e:
                log.warn('Failed to remove pidfile %s: %e', self._pidfile, e)
                log.warn('SSM may not start again until it is removed.')
