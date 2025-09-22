#!/usr/bin/env -S python3 -u
import asyncio
import socket
import sys
from enum import Enum, auto
from uap_header import *

class ClientState(Enum):
    """Enumeration for the client's state machine."""
    START = auto()
    HELLO_WAIT = auto()
    ESTABLISHED = auto()
    ALIVE_WAIT = auto()
    CLOSING = auto()
    CLOSED = auto()

class UAPClientProtocol(asyncio.DatagramProtocol):
    """
    An event-driven UAP client using asyncio.
    This single class handles network events (datagram_received, connection_lost)
    as callbacks from the asyncio event loop. There are no background threads.
    Concurrency between network I/O and stdin I/O is managed by the event loop.
    """
    def __init__(self, on_con_lost: asyncio.Future):
        self.on_con_lost = on_con_lost
        self.transport = None
        self.state = ClientState.START
        
        self.session_id = generate_session_id()
        self.sequence_number = 0
        self.logical_clock = 0
        
        self.timeout_handle = None
        # This event acts as a semaphore to coordinate the two concurrent tasks:
        # the network protocol handler and the stdin reader.
        self.can_send_event = asyncio.Event()

    def connection_made(self, transport: asyncio.DatagramTransport):
        """Callback from the event loop when the connection is ready."""
        self.transport = transport
        peername = transport.get_extra_info('peername')
        print(f"Connecting to {peername} with session 0x{self.session_id:08x}")
        self._send_hello()

    def datagram_received(self, data: bytes, addr):
        """Callback from the event loop when a packet is received."""
        if self.timeout_handle:
            self.timeout_handle.cancel()
        
        try:
            header = unpack_header(data)
            if header["session_id"] != self.session_id: return # Ignore
            
            self.logical_clock = update_logical_clock(self.logical_clock, header["logical_clock"])
            
            # --- FSA Logic ---
            if header["command"] == CMD_GOODBYE:
                self._close_connection("Received GOODBYE from server.")
                return

            if self.state == ClientState.HELLO_WAIT and header["command"] == CMD_HELLO:
                print("Session established.")
                self.state = ClientState.ESTABLISHED
                self.can_send_event.set() # Unblock the stdin reader
            elif self.state == ClientState.ALIVE_WAIT and header["command"] == CMD_ALIVE:
                self.state = ClientState.ESTABLISHED
                self.can_send_event.set() # Unblock the stdin reader
            else:
                self._close_connection(f"Protocol error: unexpected command in state {self.state.name}")
                
        except (ValueError, KeyError): pass

    def error_received(self, exc: Exception):
        self._close_connection(f"Socket error: {exc}")

    def connection_lost(self, exc: Exception):
        if not self.on_con_lost.done():
            self.on_con_lost.set_result(True)

    def _send_message(self, message_creator, next_state, data=None):
        """Helper to send a message, schedule a timeout, and update state."""
        self.logical_clock += 1
        if data:
            msg = message_creator(self.sequence_number, self.session_id, self.logical_clock, data)
        else:
            msg = message_creator(self.sequence_number, self.session_id, self.logical_clock)
        
        self.transport.sendto(msg)
        self.sequence_number += 1
        self.state = next_state
        self.timeout_handle = asyncio.get_running_loop().call_later(RTO_S, self._handle_timeout)
        
    def _send_hello(self):
        self._send_message(create_hello_message, ClientState.HELLO_WAIT)

    def send_data(self, line: str):
        self.can_send_event.clear() # Block stdin until ALIVE is received
        self._send_message(create_data_message, ClientState.ALIVE_WAIT, data=line)

    def send_goodbye(self):
        self.can_send_event.clear()
        self._send_message(create_goodbye_message, ClientState.CLOSING)

    def _handle_timeout(self):
        self._close_connection("Timeout: No response from server.")

    def _close_connection(self, reason: str):
        if self.state != ClientState.CLOSED:
            print(f"Closing connection: {reason}")
            self.state = ClientState.CLOSED
            if self.timeout_handle: self.timeout_handle.cancel()
            self.can_send_event.set() # Unblock stdin reader if it was waiting
            self.transport.close()

async def read_stdin_and_send(protocol: UAPClientProtocol):
    """A concurrent task that reads from stdin and sends data via the protocol."""
    loop = asyncio.get_running_loop()
    await protocol.can_send_event.wait() # Wait for initial HELLO handshake to complete

    while protocol.state not in [ClientState.CLOSED, ClientState.CLOSING]:
        # Wait until the protocol signals it's ready to send (i.e., last packet was ACKed).
        await protocol.can_send_event.wait()
        if protocol.state != ClientState.ESTABLISHED: break

        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line or (sys.stdin.isatty() and line.strip() == 'q'):
            print("eof" if not line else "'q' entered, closing.")
            break
        
        protocol.send_data(line.strip())
            
    if protocol.state not in [ClientState.CLOSED, ClientState.CLOSING]:
        protocol.send_goodbye()

async def main():
    if len(sys.argv) != 3: sys.exit(f"Usage: {sys.argv[0]} <hostname> <portnum>")
    hostname, port_str = sys.argv[1], sys.argv[2]
    try:
        port = int(port_str)
    except ValueError: sys.exit(f"Error: Port '{port_str}' is not a valid number.")

    loop = asyncio.get_running_loop()
    on_con_lost = loop.create_future()

    try:
        # Create the UDP endpoint. This sets up the protocol to listen for network events.
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: UAPClientProtocol(on_con_lost), remote_addr=(hostname, port))

        # Schedule the stdin reader to run concurrently with the network protocol listener.
        stdin_task = asyncio.create_task(read_stdin_and_send(protocol))
        
        # Wait until the connection is lost (either by protocol or error).
        await on_con_lost
        
        # Clean up the stdin task.
        stdin_task.cancel()
        await asyncio.gather(stdin_task, return_exceptions=True)
    except Exception as e:
        print(f"Client failed: {e}", file=sys.stderr)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nClient interrupted by user.")