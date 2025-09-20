#!/usr/bin/env python3

import socket
import sys
import time
import threading
import queue
from uap_header import *


class SessionThread(threading.Thread):
    def __init__(self, server, session):
        super().__init__(daemon=True)
        self.server = server
        self.session = session
        self.q = session['queue']

    def run(self):
        session_id = self.session['id']
        # Session thread loop: wait for packets or timeout
        while self.session.get('alive', True):
            try:
                # Wait for a packet; timeout equals inactivity timeout
                timeout_s = INACTIVITY_MS / 1000.0
                packet = self.q.get(timeout=timeout_s)
                # sentinel to stop
                if packet is None:
                    break

                # parse header and delegate to server handler
                try:
                    header = unpack_header(packet)
                    command = header['command']
                    seq_num = header['sequence_number']
                    # Update logical clock from received message
                    received_clock = header['logical_clock']
                    with self.server.lock:
                        self.server.logical_clock = update_logical_clock(self.server.logical_clock, received_clock)
                    
                    # print(f"DEBUG: Session thread processing cmd={command}, seq={seq_num}")
                    # delegate to server's handler (minimal change)
                    self.server.handle_message_in_session(self.session, command, seq_num, packet)
                except Exception as e:
                    print(f"0x{session_id:08x} Error parsing packet in session thread: {e}")

            except queue.Empty:
                # inactivity timeout -> send GOODBYE and terminate session
                if not self.session.get('alive', True):
                    break
                # send goodbye and close
                try:
                    print(f"0x{session_id:08x} Session inactivity timeout. Sending GOODBYE.")
                    self.server.send_goodbye_response(self.session)
                except Exception:
                    pass
                # close session via server method (will signal thread to stop)
                self.server.close_session(session_id)
                break


class BasicServer:
    def __init__(self, port):
        self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("", port))
        self.sessions = {}  # session_id -> session_info
        self.server_sequence = 0
        self.logical_clock = 0
        self.lock = threading.Lock()  # protect server_sequence and logical_clock and sessions
        self.running = True

    def run(self):
        print(f"Waiting on port {self.port}...")

        # Start stdin watcher thread
        stdin_thread = threading.Thread(target=self._stdin_watcher, daemon=True)
        stdin_thread.start()

        while self.running:
            try:
                packet, addr = self.socket.recvfrom(4096)
                # print(f"DEBUG: Received packet from {addr}, length={len(packet)}")
                # quick parse of header to get session id and command
                try:
                    header = unpack_header(packet)
                    # verify magic/version
                    GetMessage.check_packet(header.get('magic'), header.get('version'))
                    # print(f"DEBUG: Valid packet: session=0x{header['session_id']:08x}, cmd={header['command']}, seq={header['sequence_number']}")
                except Exception as e:
                    # print(f"DEBUG: Invalid packet, discarding: {e}")
                    # silently discard
                    continue

                session_id = header['session_id']
                command = header['command']
                seq_num = header['sequence_number']

                with self.lock:
                    if session_id not in self.sessions:
                        # New session must start with HELLO
                        if command == Constants["command_HELLO"]:
                            # print(f"DEBUG: Creating new session for 0x{session_id:08x}")
                            self._create_session_thread(session_id, addr, seq_num, packet)
                        else:
                            # print(f"DEBUG: Ignoring non-HELLO first message, cmd={command}")
                            # ignore invalid first message
                            continue
                    else:
                        # put packet into session queue for processing by that thread
                        session = self.sessions[session_id]
                        # update last_activity quickly
                        session['last_activity'] = time.time()
                        # print(f"DEBUG: Queuing packet for existing session 0x{session_id:08x}")
                        session['queue'].put(packet)

            except KeyboardInterrupt:
                print("\nServer shutting down...")
                self.running = False
                break
            except Exception as e:
                # non-fatal
                print(f"Error handling packet: {e}")

        # cleanup when exiting run loop
        self.cleanup()

    def _stdin_watcher(self):
        """Watch stdin for 'q' or EOF and trigger server shutdown."""
        try:
            # If input is a tty, read line by line
            while self.running:
                line = sys.stdin.readline()
                if line == "":
                    # EOF
                    self.running = False
                    break
                if line.rstrip('\n') == 'q':
                    self.running = False
                    break
        except Exception:
            # ignore and let main loop handle shutdown
            self.running = False

    def _create_session_thread(self, session_id, addr, seq_num, initial_packet):
        # Create session structure (minimal changes from original)
        session = {
            'id': session_id,
            'addr': addr,
            'state': 'START',
            'expected_seq': seq_num + 1,
            'last_activity': time.time(),
            'queue': queue.Queue(),
            'alive': True
        }
        # Put the initial packet into the queue so the session thread can process/create
        session['queue'].put(initial_packet)
        # create and start thread
        thread = SessionThread(self, session)
        session['thread'] = thread
        self.sessions[session_id] = session

        print(f"0x{session_id:08x} [{seq_num}] Session created")
        # start thread
        thread.start()
        
        # IMPORTANT: Don't send HELLO response here!
        # The session thread will handle the HELLO packet and send the response
        # Remove these lines:
        # self.send_hello_response(session)
        # session['state'] = 'RECEIVE'

    def handle_message_in_session(self, session, command, seq_num, packet):
        """This method was largely kept as-is from the original file; called from session threads."""
        session_id = session['id']
        session['last_activity'] = time.time()

        if command == Constants["command_HELLO"]:
            if session['state'] == 'START':
                # Expected HELLO to start session
                # print(f"DEBUG: Processing HELLO in START state")
                self.send_hello_response(session)
                session['state'] = 'RECEIVE'
            elif session['state'] == 'RECEIVE':
                # Unexpected HELLO - protocol error
                print(f"0x{session_id:08x} Protocol error: unexpected HELLO")
                self.close_session(session_id)
                return

        elif command == Constants["command_DATA"]:
            if session['state'] == 'RECEIVE':
                # Check sequence number
                if seq_num > session['expected_seq']:
                    for lost_seq in range(session['expected_seq'], seq_num):
                        print(f"0x{session_id:08x} [{lost_seq}] Lost packet!")
                elif seq_num < session['expected_seq']:
                    # Old packet - protocol error
                    print(f"0x{session_id:08x} Protocol error: old sequence number")
                    self.close_session(session_id)
                    return
                elif seq_num == session['expected_seq'] - 1:
                    # Duplicate packet
                    print(f"0x{session_id:08x} [{seq_num}] Duplicate packet!")
                    return

                # Extract and print data
                data = extract_data(packet)
                print(f"0x{session_id:08x} [{seq_num}] {data}")

                # Send ALIVE response
                self.send_alive_response(session)
                session['expected_seq'] = seq_num + 1

        elif command == Constants["command_GOODBYE"]:
            print(f"0x{session_id:08x} [{seq_num}] GOODBYE from client.")
            self.send_goodbye_response(session)
            self.close_session(session_id)

    def send_hello_response(self, session):
        """Send HELLO response to client"""
        # print(f"DEBUG: Sending HELLO response to session 0x{session['id']:08x} at {session['addr']}")
        with self.lock:
            self.logical_clock += 1
            response = create_hello_message(self.server_sequence, session['id'], self.logical_clock)
            # print(f"DEBUG: HELLO response: seq={self.server_sequence}, session=0x{session['id']:08x}")
            try:
                self.socket.sendto(response, session['addr'])
                # print(f"DEBUG: HELLO response sent successfully")
            except Exception as e:
                print(f"DEBUG: Error sending HELLO response: {e}")
            self.server_sequence += 1

    def send_alive_response(self, session):
        """Send ALIVE response to client"""
        with self.lock:
            self.logical_clock += 1
            response = create_alive_message(self.server_sequence, session['id'], self.logical_clock)
            self.socket.sendto(response, session['addr'])
            self.server_sequence += 1

    def send_goodbye_response(self, session):
        """Send GOODBYE response to client"""
        with self.lock:
            self.logical_clock += 1
            response = create_goodbye_message(self.server_sequence, session['id'], self.logical_clock)
            try:
                self.socket.sendto(response, session['addr'])
            except Exception:
                pass
            self.server_sequence += 1

    def close_session(self, session_id):
        """Close and remove a session. Signals the session thread to stop by placing a sentinel."""
        with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                return
            # mark not alive and put sentinel to wake thread
            session['alive'] = False
            try:
                session['queue'].put(None)
            except Exception:
                pass
            # print and remove
            print(f"0x{session_id:08x} Session closed")
            try:
                # join thread if alive
                thr = session.get('thread')
                if thr and thr.is_alive():
                    thr.join(timeout=0.1)
            except Exception:
                pass
            # finally remove from dict
            try:
                del self.sessions[session_id]
            except KeyError:
                pass

    def cleanup(self):
        """Send GOODBYE to all active sessions and cleanup"""
        with self.lock:
            for session_id, session in list(self.sessions.items()):
                try:
                    self.send_goodbye_response(session)
                except Exception:
                    pass
                # signal thread to stop
                session['alive'] = False
                try:
                    session['queue'].put(None)
                except Exception:
                    pass

        # give threads a moment to exit
        time.sleep(0.05)
        # clear sessions
        with self.lock:
            self.sessions.clear()
        try:
            self.socket.close()
        except Exception:
            pass


def main():
    if len(sys.argv) != 2:
        print("Usage: ./server <portnum>")
        sys.exit(1)

    try:
        port = int(sys.argv[1])
        server = BasicServer(port)
        server.run()
    except ValueError:
        print("Error: Port must be a number")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()