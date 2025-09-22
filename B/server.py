#!/usr/bin/env -S python3 -u
import socket
import sys
import threading
import queue
from uap_header import * # <-- FIX: Use the unified header file

class SessionThread(threading.Thread):
    """
    A dedicated thread to manage a single client session.
    Each instance runs the FSA for one client, isolating it from all others.
    It blocks on its private queue waiting for packets from the main dispatcher thread.
    """
    def __init__(self, server, session_info: dict):
        super().__init__(daemon=True)
        self.server = server
        self.session = session_info
        self.q = self.session['queue']

    def run(self):
        session_id = self.session['id']
        while self.session.get('alive', True):
            try:
                # Block until a packet arrives or the inactivity timeout is reached.
                timeout_s = INACTIVITY_TIMEOUT_S
                packet = self.q.get(timeout=timeout_s)
                if packet is None:  # Sentinel value to signal thread termination
                    break

                header = unpack_header(packet)
                # The server's handle_message method contains the session's FSA logic.
                self.server.handle_session_message(self.session, header, packet)

            except queue.Empty:
                # Inactivity timeout occurred.
                print(f"0x{session_id:08x} Session timed out due to inactivity.")
                self.server.send_goodbye(self.session)
                self.server.close_session(session_id)
                break # Exit the thread
            except Exception as e:
                print(f"0x{session_id:08x} Error in session thread: {e}")
                self.server.close_session(session_id)
                break


class BasicServer:
    """
    A thread-based UAP server.
    The main thread runs a loop that acts as a packet dispatcher.
    Upon receiving a HELLO from a new client, it spawns a dedicated SessionThread
    to manage that client's entire lifecycle.
    """
    def __init__(self, port: int):
        self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sessions = {}  # session_id -> session_info dictionary
        self.server_sequence = 0
        self.logical_clock = 0
        self.lock = threading.Lock() # Protects shared resources: sessions, clock, sequence
        self.running = True

    def run(self):
        self.socket.bind(("", self.port))
        print(f"Thread-based server waiting on port {self.port}...")

        while self.running:
            try:
                packet, addr = self.socket.recvfrom(4096)
                header = unpack_header(packet)
                if header['magic'] != UAP_MAGIC or header['version'] != UAP_VERSION:
                    continue # Silently discard invalid packets

                session_id = header['session_id']

                with self.lock:
                    session = self.sessions.get(session_id)
                    if session:
                        # If session exists, dispatch packet to its dedicated queue.
                        session['queue'].put(packet)
                    elif header['command'] == CMD_HELLO: # <-- FIX: Use direct constant
                        # If it's a new session, create it.
                        self._create_session(session_id, addr, packet)
                    # Silently ignore non-HELLO packets for non-existent sessions
            except (ValueError, KeyError):
                continue
            except Exception as e:
                if self.running: print(f"Main server loop error: {e}")
        
        self.cleanup()

    def _create_session(self, session_id, addr, initial_packet):
        # This method is always called from within a lock.
        header = unpack_header(initial_packet)
        session = {
            'id': session_id, 'addr': addr, 'state': 'START',
            'expected_seq': 0, 'queue': queue.Queue(), 'alive': True
        }
        self.sessions[session_id] = session
        thread = SessionThread(self, session)
        session['thread'] = thread
        
        # Put the first packet (the HELLO) in the queue for the new thread to process.
        session['queue'].put(initial_packet)
        thread.start()

    def handle_session_message(self, session: dict, header: dict, packet: bytes):
        """This is the FSA logic, executed by the dedicated SessionThread."""
        session_id = session['id']
        cmd = header['command']
        seq_num = header['sequence_number']

        with self.lock:
            self.logical_clock = update_logical_clock(self.logical_clock, header['logical_clock'])

        if cmd == CMD_HELLO: # <-- FIX: Use direct constant
            if session['state'] == 'START' and seq_num == 0:
                print(f"0x{session_id:08x} [{seq_num}] Session created")
                self.send_hello_response(session)
                session['state'] = 'ESTABLISHED'
                session['expected_seq'] = 1
            else:
                self.close_session(session_id)

        elif cmd == CMD_DATA: # <-- FIX: Use direct constant
            if session['state'] != 'ESTABLISHED': return
            if seq_num > session['expected_seq']:
                for lost in range(session['expected_seq'], seq_num):
                    print(f"0x{session_id:08x} [{lost}] Lost packet!")
            elif seq_num < session['expected_seq']:
                print(f"0x{session_id:08x} [{seq_num}] Duplicate packet!")
                return # Discard and wait for correct sequence

            payload = extract_data(packet)
            print(f"0x{session_id:08x} [{seq_num}] {payload}")
            self.send_alive(session)
            session['expected_seq'] = seq_num + 1

        elif cmd == CMD_GOODBYE: # <-- FIX: Use direct constant
            print(f"0x{session_id:08x} [{seq_num}] GOODBYE from client.")
            self.close_session(session_id)

    def _send_response(self, session, message_creator):
        with self.lock:
            self.logical_clock += 1
            self.server_sequence += 1
            msg = message_creator(self.server_sequence, session['id'], self.logical_clock)
            self.socket.sendto(msg, session['addr'])

    def send_hello_response(self, session): self._send_response(session, create_hello_message)
    def send_alive(self, session): self._send_response(session, create_alive_message)
    def send_goodbye(self, session): self._send_response(session, create_goodbye_message)

    def close_session(self, session_id):
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if session:
            session['alive'] = False
            session['queue'].put(None) # Unblock the thread so it can terminate
            print(f"0x{session_id:08x} Session closed")

    def cleanup(self):
        print("\nServer shutting down...")
        with self.lock:
            active_sessions = list(self.sessions.values())
        for session in active_sessions:
            self.send_goodbye(session)
            self.close_session(session['id'])
        self.socket.close()

if __name__ == "__main__":
    if len(sys.argv) != 2: sys.exit("Usage: ./server.py <portnum>")
    server = None
    try:
        port = int(sys.argv[1])
        server = BasicServer(port)
        server.run()
    except ValueError: sys.exit("Error: Port must be a number")
    except KeyboardInterrupt:
        if server: server.running = False
    except Exception as e: sys.exit(f"Server failed: {e}")