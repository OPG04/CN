#!/usr/bin/env -S python3 -u
import socket
import sys
import threading
import queue
from enum import Enum, auto
from typing import Optional 
from uap_header import *

class ClientState(Enum):
    """Enumeration for the client-side state machine."""
    HELLO_WAIT = auto()
    READY = auto()
    DATA_WAIT = auto() # Waiting for an ALIVE after sending DATA
    CLOSING = auto()
    CLOSED = auto()

class BasicClient:
    """
    A thread-based UAP client.
    - The main thread runs the Finite State Automata (FSA).
    - A background thread (_network_reader) blocks on network I/O.
    - A background thread (_stdin_reader) blocks on keyboard/file I/O.
    - Queues are used for safe, thread-to-thread communication.
    """
    def __init__(self, hostname: str, port: int):
        self.server_addr = (hostname, port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.session_id = generate_session_id()
        self.sequence_number = 0
        self.logical_clock = 0
        self.state = ClientState.HELLO_WAIT
        
        # Bounded queue applies back-pressure to the stdin reader, preventing it
        # from consuming an entire large file into memory at once.
        self.stdin_q = queue.Queue(maxsize=100)
        self.network_q = queue.Queue()
        
        # Used to ensure stdin_reader doesn't start sending data before handshake is complete.
        self.handshake_complete = threading.Event()

    def _stdin_reader(self):
        """
        Worker thread: Blocks on reading sys.stdin. Puts lines onto a queue
        for the main FSA thread to process. This isolates blocking I/O.
        """
        self.handshake_complete.wait() # Don't read until session is established
        for line in sys.stdin:
            self.stdin_q.put(line)
        self.stdin_q.put(None) # EOF sentinel

    def _network_reader(self):
        """
        Worker thread: Blocks on socket.recvfrom(). Puts received packets
        onto a queue for the main FSA thread to process. This isolates blocking network I/O.
        """
        while self.state != ClientState.CLOSED:
            try:
                packet, _ = self.socket.recvfrom(4096)
                self.network_q.put(packet)
            except (socket.error, OSError):
                # Socket was closed, likely by the main thread.
                break

    def run(self):
        """Main FSA loop. This thread acts as the controller."""
        threading.Thread(target=self._stdin_reader, daemon=True).start()
        threading.Thread(target=self._network_reader, daemon=True).start()

        self._send_hello()
        
        while self.state != ClientState.CLOSED:
            try:
                # States that primarily wait for network events
                if self.state in [ClientState.HELLO_WAIT, ClientState.DATA_WAIT, ClientState.CLOSING]:
                    packet = self.network_q.get(timeout=RTO_S)
                    self._handle_network_event(packet)
                # State that primarily waits for user input
                elif self.state == ClientState.READY:
                    # Non-blocking check for network packets first to handle server GOODBYE
                    try:
                        packet = self.network_q.get_nowait()
                        self._handle_network_event(packet)
                        continue # Re-evaluate state
                    except queue.Empty:
                        pass # No network event, proceed to wait for stdin

                    line = self.stdin_q.get() # This can block
                    self._handle_stdin_event(line)

            except queue.Empty:
                self._handle_timeout()
        
        print("Client exiting.")
        self.socket.close()

    def _handle_network_event(self, packet: bytes):
        try:
            header = unpack_header(packet)
            if header['session_id'] != self.session_id: 
                return # Ignore packets not for our session

            self.logical_clock = update_logical_clock(self.logical_clock, header['logical_clock'])
            cmd = header['command']

            if cmd == CMD_GOODBYE:
                print("Received GOODBYE from server.")
                self.state = ClientState.CLOSED
                self.handshake_complete.set() # Unblock stdin_reader if it's stuck
                return

            if self.state == ClientState.HELLO_WAIT and cmd == CMD_HELLO:
                self.state = ClientState.READY
                self.handshake_complete.set()
            elif self.state == ClientState.DATA_WAIT and cmd == CMD_ALIVE:
                self.state = ClientState.READY
            elif self.state == ClientState.CLOSING and cmd == CMD_ALIVE:
                # Acknowledged our last data packet, but we are still closing.
                pass 
        except (ValueError, KeyError):
            pass # Ignore malformed packets

    def _handle_stdin_event(self, line: Optional[str]):
        if line is None: # EOF sentinel from _stdin_reader
            print("GOODBYE from Server")
            self._send_goodbye()
            return
        
        line = line.strip()
        if sys.stdin.isatty() and line == 'q':
            self._send_goodbye()
            return
        
        self._send_data(line)

    def _handle_timeout(self):
        print("Timeout waiting for server response.")
        # If any state times out, we treat it as a fatal error and close.
        self.state = ClientState.CLOSED
        self.handshake_complete.set() # Ensure _stdin_reader can exit

    def _send_message(self, message: bytes):
        """Helper to send a message and increment sequence number."""
        self.socket.sendto(message, self.server_addr)
        self.sequence_number += 1
        # print(f"SN {self.sequence_number}")
        self.logical_clock += 1

    def _send_hello(self):
        print(f"Sending HELLO with session ID 0x{self.session_id:08x}")
        self.state = ClientState.HELLO_WAIT
        msg = create_hello_message(self.sequence_number, self.session_id, self.logical_clock)
        self._send_message(msg)
        # print("Hello")

    def _send_data(self, line: str):
        self.state = ClientState.DATA_WAIT
        msg = create_data_message(self.sequence_number, self.session_id, self.logical_clock, line)
        self._send_message(msg)
        # print("Alive")

    def _send_goodbye(self):
        self.state = ClientState.CLOSING
        msg = create_goodbye_message(self.sequence_number, self.session_id, self.logical_clock)
        self._send_message(msg)
        # print("GoodBye")

if __name__ == "__main__":
    if len(sys.argv) != 3: 
        sys.exit(f"Usage: {sys.argv[0]} <hostname> <portnum>")
    try:
        client = BasicClient(sys.argv[1], int(sys.argv[2]))
        client.run()
    except ValueError: 
        sys.exit("Error: Port must be a number")
    except KeyboardInterrupt: 
        print("\nClient interrupted by user.")
    except Exception as e: 
        sys.exit(f"Client failed: {e}")