#!/usr/bin/python2 -O

# Copyright 2015, Oliver Jowett <oliver@mutability.co.uk>
# All rights reserved. Do not redistribute.

# (I plan to eventually release this under an open source license,
# but I'd like to get the selection algorithm and network protocol stable
# first)

import sys

if __name__ == '__main__':
    print >>sys.stderr, 'Hang on while I load everything (takes a few seconds on a Pi)..'

import socket, json, time, traceback, asyncore, zlib, argparse, struct
from contextlib import closing

import _modes

def TS(t):
    return t * 12e6

def log(msg, *args, **kwargs):
    print >>sys.stderr, time.ctime(), msg.format(*args,**kwargs)

def log_exc(msg, *args, **kwards):
    print >>sys.stderr, time.ctime(), msg.format(*args,**kwargs)
    traceback.print_exc(sys.stderr)

class ReconnectingConnection(asyncore.dispatcher):
    reconnect_interval = 30.0

    def __init__(self, host, port):
        asyncore.dispatcher.__init__(self)
        self.host = host
        self.port = port
        self.state = 'disconnected'
        self.reconnect_at = None

    def heartbeat(self, now):
        if self.reconnect_at is None or self.reconnect_at > now: return
        if self.state == 'ready': return
        self.reconnect_at = None
        self.reconnect()

    def close(self, manual_close=False):
        if self.state != 'disconnected':
            if not manual_close:
                log('Lost connection to {host}:{port}', host=self.host, port=self.port)
                #traceback.print_stack()

            asyncore.dispatcher.close(self)
            self.state = 'disconnected'
            self.lost_connection()
            self.reset_connection()

        if not manual_close: self.schedule_reconnect()

    def disconnect(self, reason):
        if self.state != 'disconnected':
            log('Disconnecting from {host}:{port}: {reason}', host=self.host, port=self.port, reason=reason)
            self.close(True)

    def writable(self):
        return self.connecting

    def schedule_reconnect(self):
        if self.reconnect_at is None:
            log('Reconnecting in {0} seconds', self.reconnect_interval)
            self.reconnect_at = time.time() + self.reconnect_interval

    def reconnect(self):
        if self.state != 'disconnected':
            self.disconnect('About to reconnect')

        try:
            self.reset_connection()
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            self.connect((self.host, self.port))
        except socket.error as e:
            log('Connection to {host}:{port} failed: {ex!s}', host=self.host, port=self.port, ex=e)
            self.close()

    def handle_connect(self):
        log('Connected to {host}:{port}', host=self.host, port=self.port)
        self.state = 'connected'
        self.start_connection()

    def handle_read(self):
        pass

    def handle_write(self):
        pass

    def reset_connection(self):
        pass

    def start_connection(self):
        pass

    def lost_connection(self):
        pass

class BeastConnection(ReconnectingConnection):
    def __init__(self, host, port):
        ReconnectingConnection.__init__(self, host, port)
        self.coordinator = None

    def reset_connection(self):
        self.readbuf = bytearray()

    def start_connection(self):
        log('Beast input connected to {0}:{1}', self.host, self.port)
        self.state = 'ready'
        self.coordinator.input_connected()

    def lost_connection(self):
        self.coordinator.input_disconnected()

    def handle_read(self):
        try:
            moredata = bytearray(self.recv(16384))
        except socket.error as e:
            if e.errno == errno.EAGAIN:
                return
            raise

        if not moredata:
            self.close()

        self.readbuf += moredata
            
        consumed,messages = _modes.packetize_beast_input(self.readbuf)
        if consumed: self.readbuf = self.readbuf[consumed:]        
        if len(self.readbuf) > 512:
            raise ParseError('parser broken - buffer not being consumed')

        if messages:
            self.coordinator.input_received_messages(messages)

class ServerConnection(ReconnectingConnection):
    reconnect_interval = 30.0
    heartbeat_interval = 120.0

    def __init__(self, host, port, handshake_data, offer_zlib):
        ReconnectingConnection.__init__(self, host, port)
        self.handshake_data = handshake_data
        self.offer_zlib = offer_zlib
        self.coordinator = None
        self.selective_traffic = False

    def reset_connection(self):
        self.readbuf = ''
        self.writebuf = ''
        self.linebuf = []
        self.fill_writebuf = None
        self.handle_server_line = None
        self.server_heartbeat_at = None

    def lost_connection(self):
        self.coordinator.server_disconnected()

    def readable(self):
        return self.handle_server_line is not None

    def writable(self):
        return self.connecting or self.writebuf or (self.fill_writebuf and self.linebuf)

    def handle_write(self):
        if self.fill_writebuf:
            self.fill_writebuf()

        if self.writebuf:
            sent = self.send(self.writebuf)
            self.writebuf = self.writebuf[sent:]
            if len(self.writebuf) > 65536:
                self.disconnect('Server write buffer overflow (too much unsent data)')

    def fill_uncompressed(self):
        if not self.linebuf: return
        for line in self.linebuf:
            self.writebuf += line + '\n'
        self.linebuf = []

    def fill_zlib(self):
        if not self.linebuf: return

        data = ''
        pending = False
        for line in self.linebuf:
            data += self.compressor.compress(line + '\n')
            pending = True

            if len(data) >= 32768:
                data += self.compressor.flush(zlib.Z_SYNC_FLUSH)
                assert len(data) < 65536
                assert data[-4:] == '\x00\x00\xff\xff'
                data = struct.pack('!H', len(data)-4) + data[:-4]
                self.writebuf += data
                data = ''
                pending = False

        if pending:
            data += self.compressor.flush(zlib.Z_SYNC_FLUSH)
            assert len(data) < 65536
            assert data[-4:] == '\x00\x00\xff\xff'
            data = struct.pack('!H', len(data)-4) + data[:-4]
            self.writebuf += data    

        self.linebuf = []

    def send_json(self, o):
        #log('Send: {0}', o)
        self.linebuf.append(json.dumps(o, separators=(',',':')))

    def send_mlat(self, message):
        self.linebuf.append('{{"mlat":{{"t":{0},"m":"{1}"}}}}'.format(message.timestamp, str(message)))

    def send_mlat_and_alt(self, message, altitude):
        self.linebuf.append('{{"mlat":{{"t":{0},"m":"{1}","a":{2}}}}}'.format(message.timestamp, str(message), altitude))

    def send_sync(self, em, om):
        self.linebuf.append('{{"sync":{{"et":{0},"em":"{1}","ot":{2},"om":"{3}"}}}}'.format(em.timestamp, str(em), om.timestamp, str(om)))

    def send_seen(self, aclist):
        self.send_json({'seen': ['{0:06x}'.format(icao) for icao in aclist]})

    def send_lost(self, aclist):
        self.send_json({'lost': ['{0:06x}'.format(icao) for icao in aclist]})

    def start_connection(self):
        log('Connected to server at {0}:{1}, handshaking', self.host, self.port)
        self.state = 'handshaking'

        compress_methods = ['none']
        if self.offer_zlib:
            compress_methods.append('zlib')

        handshake_msg = { 'version' : 2, 'compress' : compress_methods, 'selective_traffic' : True, 'heartbeat' : True, 'return_results' : True }
        handshake_msg.update(self.handshake_data)
        self.writebuf += json.dumps(handshake_msg) + '\n' # linebuf not used yet
        self.handle_server_line = self.handle_handshake_response

    def heartbeat(self, now):
        ReconnectingConnection.heartbeat(self,now)

        if self.server_heartbeat_at is not None and self.server_heartbeat_at < now:
            self.server_heartbeat_at = now + self.heartbeat_interval
            self.send_json({'heartbeat': round(now,1)})

    def handle_read(self):
        try:
            moredata = self.recv(16384)
        except socket.error as e:
            if e.errno == errno.EAGAIN:
                return
            raise

        if not moredata:
            self.close()
            self.schedule_reconnect()
            return

        self.readbuf += moredata
        lines = self.readbuf.split('\n')
        self.readbuf = lines[-1]
        for line in lines[:-1]:
            self.handle_server_line(json.loads(line))
    
    def handle_handshake_response(self, response):
        if 'reconnect_in' in response:
            self.reconnect_interval = response['reconnect_in']

        if 'deny' in response:
            log('Server explicitly rejected our connection, saying:')
            for reason in response['deny']:
                log('  {0}', reason)
            raise IOError('Server rejected our connection attempt')

        if 'motd' in response:
            log('Server says: {0}', response['motd'])

        compress = response.get('compress', 'none')
        if response['compress'] == 'none':
            self.fill_writebuf = self.fill_uncompressed
        elif response['compress'] == 'zlib' and self.offer_zlib:
            self.compressor = zlib.compressobj(1)
            self.fill_writebuf = self.fill_zlib
        else:
            raise IOError('Server response asked for a compression method {0}, which we do not support'.format(response['compress']))

        self.selective_traffic = response.get('selective_traffic', False)
        if response.get('heartbeat',False):
            self.server_heartbeat_at = time.time() + self.heartbeat_interval

        log('Handshake complete.')
        log('  Compression:       {0}', compress)
        log('  Selective traffic: {0}', self.selective_traffic and 'enabled' or 'disabled')
        log('  Heartbeats:        {0}', self.server_heartbeat_at and 'enabled' or 'disabled')

        self.state = 'ready'
        self.handle_server_line = self.handle_connected_request
        self.coordinator.server_connected()

    def handle_connected_request(self, request):
        #log('Receive: {0}', request)
        if 'start_sending' in request:
            self.coordinator.start_sending([int(x,16) for x in request['start_sending']])
        elif 'stop_sending' in request:
            self.coordinator.stop_sending([int(x,16) for x in request['stop_sending']])
        elif 'heartbeat' in request:
            pass
        elif 'result' in request:
            result = request['result']
            self.coordinator.received_mlat_result(addr=result['addr'],
                                                  lat=result['lat'],
                                                  lon=result['lon'],
                                                  alt=result['alt'],
                                                  callsign=result['callsign'],
                                                  squawk=result['squawk'],
                                                  hdop=result['hdop'],
                                                  vdop=result['vdop'],
                                                  tdop=result['tdop'],
                                                  gdop=result['gdop'],
                                                  nstations=result['nstations'])
        else:
            log('ignoring request from server: {0}', request)

class Aircraft:
    def __init__(self, icao):
        self.icao = icao
        self.messages = 0
        self.last_message_timestamp = 0
        self.last_position_timestamp = 0
        self.last_altitude_timestamp = 0
        self.altitude = None
        self.even_message = None
        self.odd_message = None
        self.reported = False
        self.requested = True

class Coordinator:
    report_interval = 15.0
    expiry_interval = 60.0

    def __init__(self, beast, server, random_drop):
        self.beast = beast
        self.server = server
        self.random_drop_cutoff = int(255 * random_drop)

        self.aircraft = {}
        self.requested_traffic = set()
        self.df_handlers = {
            0: self.received_df_misc_alt,
            4: self.received_df_misc_alt,
            5: self.received_df_misc_noalt,
            16: self.received_df_misc_alt,
            20: self.received_df_misc_alt,
            21: self.received_df_misc_noalt,
            11: self.received_df11,
            17: self.received_df17
        }
        self.last_rcv_timestamp = None

        self.next_report = None
        self.next_expiry = None

        beast.coordinator = self
        server.coordinator = self

    def run(self):
        try:
            self.server.reconnect()

            next_heartbeat = time.time() + 1.0
            while True:
                # maybe there are no active sockets and
                # we're just waiting on a timeout
                if asyncore.socket_map:
                    asyncore.loop(timeout=0.2, count=5)
                else:
                    time.sleep(1.0)

                now = time.time()
                if now >= next_heartbeat:
                    next_heartbeat = now + 1.0
                    self.heartbeat(now)

        finally:
            if self.beast.socket: self.beast.disconnect('Server shutting down')
            if self.server.socket: self.server.disconnect('Server shutting down')

    def input_connected(self):
        self.server.send_json({'input_connected' : 'OK'})

    def input_disconnected(self):
        self.server.send_json({'input_disconnected' : 'no longer connected'})

    def server_connected(self):
        self.requested_traffic = set()
        self.newly_seen = set()
        self.aircraft = {}
        self.next_report = time.time() + self.report_interval
        self.next_expiry = time.time() + self.expiry_interval
        if self.beast.state != 'ready':
            self.beast.reconnect()

    def server_disconnected(self):
        self.beast.disconnect('Lost connection to multilateration server, no need for input data')
        self.next_report = None
        self.next_expiry = None

    def input_received_messages(self, messages):
        for message in messages:
            if self.random_drop_cutoff and message[-1] < self.random_drop_cutoff: # last byte, part of the checksum, should be fairly randomly distributed
                continue;

            if message.timestamp < self.last_rcv_timestamp:
                return

            self.last_rcv_timestamp = message.timestamp

            if not message.valid:
                return

            handler = self.df_handlers.get(message.df)
            if handler: handler(message)

    def start_sending(self, icao_list):
        log('Server requests traffic for {0} aircraft', len(icao_list))
        for icao in icao_list:
            ac = self.aircraft.get(icao)
            if ac: ac.requested = True
        self.requested_traffic.update(icao_list)

    def stop_sending(self, icao_list):
        log('Server stops traffic for {0} aircraft', len(icao_list))
        for icao in icao_list:
            ac = self.aircraft.get(icao)
            if ac: ac.requested = False
        self.requested_traffic.difference_update(icao_list)

    def heartbeat(self, now):
        self.beast.heartbeat(now)
        self.server.heartbeat(now)

        if self.next_report and now >= self.next_report:
            self.next_report = now + self.report_interval
            self.send_aircraft_report()

        if self.next_expiry and now >= self.next_expiry:
            self.next_expiry = now + self.expiry_interval
            self.expire()

    def report_aircraft(self, ac):
        ac.reported = True
        if not self.server.selective_traffic:
            ac.requested = True
        self.newly_seen.add(ac.icao)

    def send_aircraft_report(self):
        if self.newly_seen:
            log('Telling server about {0} new aircraft', len(self.newly_seen))
            self.server.send_seen(self.newly_seen)
            self.newly_seen.clear()
            
    def expire(self):
        reported_count = requested_count = discarded_count = 0
        discarded = []
        for ac in self.aircraft.values():
            if (self.last_rcv_timestamp - ac.last_message_timestamp) > TS(60):
                discarded_count += 1
                if ac.reported:
                    discarded.append(ac.icao)
                del self.aircraft[ac.icao]
            else:
                if ac.reported:
                    reported_count += 1
                if ac.requested:
                    requested_count += 1

        if discarded:
            self.server.send_lost(discarded)

        log('Expired {0} aircraft, {1} remaining', discarded_count, len(self.aircraft))
        log('Sending traffic for {0}/{1} aircraft, server requested {2} aircraft', requested_count, reported_count, len(self.requested_traffic))

    def received_df_misc_noalt(self, message):
        ac = self.aircraft.get(message.address)
        if not ac: return False  # not a known ICAO

        ac.messages += 1
        ac.last_message_timestamp = message.timestamp

        if ac.messages < 10: return   # wait for more messages
        if ac.reported and not ac.requested: return
        if message.timestamp - ac.last_position_timestamp < TS(60): return   # reported position recently, no need for mlat
        if message.timestamp - ac.last_altitude_timestamp > TS(15): return   # too long since altitude reported
        if not ac.reported:
            self.report_aircraft(ac)
            return

        # Candidate for MLAT
        self.server.send_mlat_and_alt(message, ac.altitude)

    def received_df_misc_alt(self, message):
        if not message.altitude: return

        ac = self.aircraft.get(message.address)
        if not ac: return False  # not a known ICAO

        ac.messages += 1
        ac.last_message_timestamp = message.timestamp
        ac.last_altitude_timestamp = message.timestamp
        ac.altitude = message.altitude

        if ac.messages < 10: return   # wait for more messages
        if ac.reported and not ac.requested: return
        if message.timestamp - ac.last_position_timestamp < TS(60): return   # reported position recently, no need for mlat
        if not ac.reported:
            self.report_aircraft(ac)
            return

        # Candidate for MLAT
        self.server.send_mlat(message)

    def received_df11(self, message):
        ac = self.aircraft.get(message.address)
        if not ac:
            ac = Aircraft(message.address)
            ac.requested = (message.address in self.requested_traffic)
            ac.messages += 1
            ac.last_message_timestamp = message.timestamp
            self.aircraft[message.address] = ac
            return # will need some more messages..

        ac.messages += 1
        ac.last_message_timestamp = message.timestamp

        if ac.messages < 10: return   # wait for more messages
        if ac.reported and not ac.requested: return
        if message.timestamp - ac.last_position_timestamp < TS(60): return   # reported position recently, no need for mlat
        if message.timestamp - ac.last_altitude_timestamp > TS(15): return   # no recent altitude available
        if not ac.reported:
            self.report_aircraft(ac)
            return

        # Candidate for MLAT
        self.server.send_mlat_and_alt(message, ac.altitude)

    def received_df17(self, message):
        ac = self.aircraft.get(message.address)
        if not ac:
            ac = Aircraft(message.address)
            ac.requested = (message.address in self.requested_traffic)
            ac.messages += 1
            ac.last_message_timestamp = ac.last_position_timestamp = message.timestamp
            self.aircraft[message.address] = ac
            return # wait for more messages

        ac.messages += 1
        ac.last_message_timestamp = message.timestamp

        if ac.messages < 10: return
        if ac.reported and not ac.requested: return
        if message.altitude is None: return    # need an altitude

        if message.even_cpr:
            ac.last_position_timestamp = message.timestamp
            ac.even_message = message
        elif message.odd_cpr:
            ac.last_position_timestamp = message.timestamp
            ac.odd_message = message
        else:
            return # not a position ES message

        if not ac.even_message or not ac.odd_message: return
        if abs(ac.even_message.timestamp - ac.odd_message.timestamp) > TS(5): return

        # this is a useful reference message pair
        if not ac.reported:
            self.report_aircraft(ac)
            return

        self.server.send_sync(ac.even_message, ac.odd_message)

    def received_mlat_result(self, addr, lat, lon, alt, callsign, squawk, hdop, vdop, tdop, gdop, nstations):
        # todo: local SBS output, etc
        pass

def main():
    def latitude(s):
        lat = float(s)
        if lat < -90 or lat > 90:
            raise argparse.ArgumentTypeError('Latitude %s must be in the range -90 to 90' % s)
        return lat

    def longitude(s):
        lon = float(s)
        if lon < -180 or lon > 360:
            raise argparse.ArgumentTypeError('Longitude %s must be in the range -180 to 360' % s)
        if lon > 180:
            lon -= 360
        return lon

    def altitude(s):
        if s.endswith('m'):
            alt = float(s[:-1])
        elif s.endswith('ft'):
            alt = float(s[:-2]) * 0.3048
        else:
            alt = float(s)

        # Wikipedia to the rescue!
        # "The lowest point on dry land is the shore of the Dead Sea [...]
        # 418m below sea level". Perhaps not the best spot for a receiver?
        # La Rinconada, Peru, pop. 30,000, is at 5100m.
        if alt < -420 or alt > 5100:
            raise argparse.ArgumentTypeError('Altitude %s must be in the range -420m to 6000m' % s)
        return alt

    def port(s):
        port = int(s)
        if port < 1 or port > 65535:
            raise argparse.ArgumentTypeError('Port %s must be in the range 1 to 65535' % s)
        return port

    def percentage(s):
        p = int(s)
        if p < 0 or p > 100:
            raise argparse.ArgumentTypeError('Percentage %s must be in the range 0 to 100' % s)
        return p / 100.0

    parser = argparse.ArgumentParser(description="Client for multilateration.")
    parser.add_argument('--lat',
                        type=latitude,
                        help="Latitude of the receiver, in decimal degrees",
                        required=True)
    parser.add_argument('--lon',
                        type=longitude,
                        help="Longitude of the receiver, in decimal degrees",
                        required=True)
    parser.add_argument('--alt',
                        type=altitude,
                        help="Altitude of the receiver (AMSL). Defaults to metres, but units may specified with a 'ft' or 'm' suffix. (Except if they're negative due to option parser weirdness. Sorry!)",
                        required=True)
    parser.add_argument('--user',
                        help="User information to give to the server. Used to get in touch if there are problems.",
                        required=True)
    parser.add_argument('--input-host',
                        help="Host (IP or hostname) to connect to for Mode S traffic",
                        required=True)
    parser.add_argument('--input-port',
                        help="Port to connect to for Mode S traffic. This should be a port that provides data in the 'Beast' binary format",
                        type=port,
                        default=30005)
    parser.add_argument('--output-host',
                        help="Host (IP or hostname) of the multilateration server",
                        default="mlat.mutability.co.uk")
    parser.add_argument('--output-port',
                        help="Port of the multilateration server",
                        type=port,
                        default=40147)
    parser.add_argument('--no-compression',
                        dest='compress',
                        help="Don't offer to use zlib compression to the multilateration server",
                        action='store_false',
                        default=True)
    parser.add_argument('--random-drop',
                        type=percentage,
                        help="Drop some percentage of messages",
                        default=0)

    args = parser.parse_args()

    beast = BeastConnection(host=args.input_host, port=args.input_port)
    server = ServerConnection(host=args.output_host, port=args.output_port,
                              handshake_data={'lat':args.lat,
                                              'lon':args.lon,
                                              'alt':args.alt,
                                              'user':args.user,
                                              'random_drop':args.random_drop},
                              offer_zlib=args.compress)

    coordinator = Coordinator(beast = beast, server = server, random_drop=args.random_drop)
    coordinator.run()

if __name__ == '__main__':
    main()
